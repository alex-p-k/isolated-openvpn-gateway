#!/usr/bin/env python3
"""Strict, dependency-free deployment configuration for the gateway."""
import ipaddress
from pathlib import Path
import re
import tomllib


PROJECT = 'isolated-openvpn-gateway'
CLI = 'vpn-gateway'
BROWSER_CLI = 'vpn-browser'
INSTALL_DIR = '.local/share/isolated-openvpn-gateway'
BROWSER_DIR = '.local/share/isolated-openvpn-browser'
VPN_IMAGE = 'isolated-openvpn-gateway-vpn:local'
SOCKS_IMAGE = 'isolated-openvpn-gateway-socks:local'
ALLOWED_PROTOCOLS = {'udp', 'udp4', 'tcp-client', 'tcp4-client'}
ALLOWED_INLINE_BLOCKS = {'ca', 'cert', 'key', 'tls-auth', 'tls-crypt', 'tls-crypt-v2'}


class ConfigError(ValueError):
    pass


def _host(value, field):
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ConfigError(field + ' must be a hostname or IPv4 address.')
    try:
        address = ipaddress.ip_address(value)
        if address.version != 4 or address.is_unspecified:
            raise ValueError
        return str(address)
    except ValueError:
        if not re.fullmatch(r'(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value):
            raise ConfigError(field + ' must be a hostname or IPv4 address.') from None
        if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
               for label in value.split('.')):
            raise ConfigError(field + ' is not a valid hostname.')
        return value


def _port(value, field):
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise ConfigError(field + ' must be an integer from 1 to 65535.')
    return value


def _only(table, allowed, field):
    extra = set(table) - set(allowed)
    if extra:
        raise ConfigError(field + ' has unsupported keys: ' + ', '.join(sorted(extra)))


def load_config(path):
    """Load a deployment file without executing it or resolving profile paths."""
    path = Path(path)
    try:
        raw = path.read_bytes()
        if len(raw) > 128 * 1024:
            raise ConfigError('gateway.toml is unexpectedly large.')
        data = tomllib.loads(raw.decode('utf-8'))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError('Cannot read a valid gateway.toml deployment file.') from exc
    _only(data, {'gateway', 'transports'}, 'gateway.toml')
    gateway = data.get('gateway')
    transports = data.get('transports')
    if not isinstance(gateway, dict) or not isinstance(transports, dict) or not transports:
        raise ConfigError('gateway.toml requires [gateway] and at least one [transports.NAME] table.')
    _only(gateway, {'id', 'display_name', 'default_transport', 'dns_canary', 'socks_port',
                    'require_outer_vpn', 'outer_interface_prefix'}, '[gateway]')
    identifier = gateway.get('id')
    if not isinstance(identifier, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', identifier):
        raise ConfigError('gateway.id must use lowercase letters, digits and hyphens (max 32).')
    display_name = gateway.get('display_name', identifier)
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 80 or \
            any(ord(ch) < 32 for ch in display_name):
        raise ConfigError('gateway.display_name must be 1-80 printable characters.')
    default = gateway.get('default_transport')
    if not isinstance(default, str) or default not in transports:
        raise ConfigError('gateway.default_transport must name a configured transport.')
    dns_canary = gateway.get('dns_canary')
    if dns_canary is not None:
        dns_canary = _host(dns_canary, 'gateway.dns_canary')
    socks_port = _port(gateway.get('socks_port', 1080), 'gateway.socks_port')
    require_outer = gateway.get('require_outer_vpn', True)
    if not isinstance(require_outer, bool):
        raise ConfigError('gateway.require_outer_vpn must be true or false.')
    interface_prefix = gateway.get('outer_interface_prefix', 'utun')
    if not isinstance(interface_prefix, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,15}', interface_prefix):
        raise ConfigError('gateway.outer_interface_prefix must be a simple interface prefix.')

    normalized = {}
    profile_files = set()
    for name, item in transports.items():
        field = '[transports.' + str(name) + ']'
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,15}', name):
            raise ConfigError('Transport names must use lowercase letters, digits, _ or - (max 16).')
        if not isinstance(item, dict):
            raise ConfigError(field + ' must be a table.')
        _only(item, {'profile_file', 'remote_host', 'remote_port', 'remote_protocol',
                     'http_proxy_host', 'http_proxy_port', 'required_inline_blocks'}, field)
        profile_file = item.get('profile_file')
        if not isinstance(profile_file, str) or not profile_file.lower().endswith('.ovpn') or \
                Path(profile_file).name != profile_file or profile_file.startswith('.') or '\x00' in profile_file:
            raise ConfigError(field + '.profile_file must be a plain .ovpn filename.')
        if profile_file in profile_files:
            raise ConfigError('Each transport must use a distinct profile_file.')
        profile_files.add(profile_file)
        protocol = item.get('remote_protocol')
        if protocol not in ALLOWED_PROTOCOLS:
            raise ConfigError(field + '.remote_protocol must be IPv4 UDP or TCP client mode.')
        proxy_host = item.get('http_proxy_host')
        proxy_port = item.get('http_proxy_port')
        if (proxy_host is None) != (proxy_port is None):
            raise ConfigError(field + ' must set both http_proxy_host and http_proxy_port.')
        blocks = item.get('required_inline_blocks', ['ca'])
        if not isinstance(blocks, list) or not blocks or any(x not in ALLOWED_INLINE_BLOCKS for x in blocks):
            raise ConfigError(field + '.required_inline_blocks contains an unsupported block name.')
        blocks = list(dict.fromkeys(blocks))
        if 'ca' not in blocks:
            raise ConfigError(field + '.required_inline_blocks must include ca.')
        normalized[name] = {
            'profile_file': profile_file,
            'remote_host': _host(item.get('remote_host'), field + '.remote_host'),
            'remote_port': _port(item.get('remote_port'), field + '.remote_port'),
            'remote_protocol': protocol,
            'http_proxy_host': _host(proxy_host, field + '.http_proxy_host') if proxy_host else None,
            'http_proxy_port': _port(proxy_port, field + '.http_proxy_port') if proxy_port else None,
            'required_inline_blocks': blocks,
        }
    if dns_canary is None:
        dns_canary = normalized[default]['remote_host']
    return {
        'id': identifier,
        'display_name': display_name.strip(),
        'default_transport': default,
        'dns_canary': dns_canary,
        'socks_host': '127.0.0.1',
        'socks_port': socks_port,
        'require_outer_vpn': require_outer,
        'outer_interface_prefix': interface_prefix,
        'transports': normalized,
    }
