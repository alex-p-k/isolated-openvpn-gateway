"""Extract an unambiguous endpoint, without executing or exposing a profile."""
import json
import shlex

from gateway_config import ALLOWED_PROTOCOLS, ALLOWED_INLINE_BLOCKS, _host, _port
from product import ProductError


def normalize(source):
    lines = source.lstrip('\ufeff').splitlines()
    remotes, protocols, proxies, blocks = [], [], [], []
    block = None
    for index, line in enumerate(lines):
        value = line.strip()
        if block:
            if value == '</' + block + '>':
                block = None
            continue
        if value.startswith('<'):
            name = value[1:-1]
            if name not in ALLOWED_INLINE_BLOCKS or name in blocks:
                raise ProductError('PROFILE_BLOCK', 'Unsupported or duplicate inline block.', 'Ask IT for a compatible inline .ovpn profile.')
            blocks.append(name)
            block = name
            continue
        if not value or value.startswith(('#', ';')):
            continue
        try:
            tokens = shlex.split(value, comments=True)
        except ValueError:
            raise ProductError('PROFILE_SYNTAX', 'Malformed profile directive.', 'Ask IT to review the profile syntax.') from None
        if not tokens:
            continue
        if tokens[0] == 'remote': remotes.append((index, tokens))
        if tokens[0] == 'proto': protocols.append(tokens)
        if tokens[0] == 'http-proxy': proxies.append(tokens)
    if block or len(remotes) != 1 or len(protocols) > 1 or len(proxies) > 1:
        raise ProductError('PROFILE_AMBIGUOUS', 'One remote, at most one proto/http-proxy and closed inline blocks are required.',
                           'Ask IT for a single-endpoint profile or a prepared gateway.toml kit.')
    index, remote = remotes[0]
    protocol = protocols[0][1] if protocols and len(protocols[0]) == 2 else None
    if protocols and protocol is None:
        raise ProductError('PROFILE_PROTOCOL', 'proto must contain one explicit protocol.')
    if len(remote) == 4:
        if protocol and protocol != remote[3]:
            raise ProductError('PROFILE_PROTOCOL', 'remote and proto disagree; no protocol was guessed.')
        protocol = remote[3]
    if len(remote) not in (3, 4) or protocol not in ALLOWED_PROTOCOLS:
        raise ProductError('PROFILE_PROTOCOL', 'remote needs an explicit port and IPv4 UDP/TCP-client protocol.',
                           'Ask IT to specify remote HOST PORT PROTOCOL, or a separate proto directive.')
    try:
        hostname = _host(remote[1], 'remote')
        port = _port(int(remote[2]), 'remote port')
        proxy_host, proxy_port = None, None
        if proxies:
            if len(proxies[0]) != 3:
                raise ValueError
            proxy_host = _host(proxies[0][1], 'http-proxy')
            proxy_port = _port(int(proxies[0][2]), 'http-proxy port')
    except ValueError:
        raise ProductError('PROFILE_ENDPOINT', 'Invalid remote/http-proxy address, port or authentication arguments.',
                           'Ask IT for a compatible profile; endpoint values were not printed.') from None
    lines[index] = f'remote {hostname} {port} {protocol}'
    return '\n'.join(lines) + '\n', {
        'profile_file': 'primary.ovpn', 'remote_host': hostname, 'remote_port': port,
        'remote_protocol': protocol, 'http_proxy_host': proxy_host, 'http_proxy_port': proxy_port,
        'required_inline_blocks': sorted(set(blocks) | {'ca'}),
    }


def deployment(metadata, adapters, display_name='Corporate VPN'):
    # JSON string quoting is a safe subset of TOML basic strings here.
    quote = lambda value: json.dumps(value, ensure_ascii=True)
    lines = ['[gateway]', 'id = "corporate-vpn"', 'display_name = ' + quote(display_name),
             'default_transport = "primary"', 'require_outer_vpn = true',
             'windows_outer_adapter_contains = ' + quote(adapters), '', '[transports.primary]']
    for name, value in metadata.items():
        if value is not None:
            lines.append(name + ' = ' + quote(value))
    return ('\n'.join(lines) + '\n').encode('utf-8')
