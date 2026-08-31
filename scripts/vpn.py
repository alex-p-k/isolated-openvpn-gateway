#!/usr/bin/python3
"""Container-only OpenVPN, firewall, DNS hook and sanitized lifecycle log."""
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time

STATE = Path('/state')
STATUS = STATE / 'status.json'
BINARIES = {'ip': '/sbin/ip', 'iptables': '/usr/sbin/iptables', 'ip6tables': '/usr/sbin/ip6tables'}

def command(*args, check=True):
    # OpenVPN intentionally strips the inherited PATH from hook environments.
    # /sbin is absent from Python's default search path; never depend on it.
    executable = BINARIES.get(args[0], args[0])
    return subprocess.run((executable, *args[1:]), check=check, capture_output=True, text=True)

def write_state(data):
    with tempfile.NamedTemporaryFile(mode='w', dir=STATE, prefix='.status-', delete=False) as handle:
        json.dump(data, handle, indent=2)
        tmp = Path(handle.name)
    try:
        tmp.chmod(0o644)
        tmp.replace(STATUS)
    finally:
        tmp.unlink(missing_ok=True)

def read_state():
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return {}

def resolver(dns=(), domains=()):
    # Update IN PLACE: the SOCKS container bind-mounts this inode.
    text = ''.join('nameserver ' + x + '\n' for x in dns) or 'nameserver 127.0.0.1\n'
    if domains:
        text += 'search ' + ' '.join(domains[:6]) + '\n'
    text += 'options timeout:2 attempts:2\n'
    (STATE / 'resolv.conf').write_text(text)
    (STATE / 'resolv.conf').chmod(0o644)

def pushed_dns(env):
    dns, domains = [], []
    for key, value in sorted(env.items()):
        if not key.startswith('foreign_option_'):
            continue
        p = value.split()
        if len(p) < 3 or p[0] != 'dhcp-option':
            continue
        if p[1] == 'DNS':
            try:
                address = ipaddress.ip_address(p[2])
                if address.version == 4 and not address.is_loopback and not address.is_unspecified:
                    dns.append(str(address))
            except ValueError:
                pass
        elif p[1] in ('DOMAIN', 'DOMAIN-SEARCH'):
            domains.extend(x for x in p[2:] if re.fullmatch(r'[a-zA-Z0-9_.-]{1,253}', x))
    return list(dict.fromkeys(dns)), list(dict.fromkeys(domains))

def hook():
    data = read_state()
    data['ready'] = False
    if os.environ.get('script_type') == 'route-up':
        if os.environ.get('dev') != 'tun0':
            raise RuntimeError('Expected tun0')
        command('ip', 'route', 'replace', 'table', '100', 'default', 'dev', 'tun0', 'metric', '10')
        dns, domains = pushed_dns(os.environ)
        resolver(dns, domains)
        data.update(dns=dns, domains=domains, dns_pushed=bool(dns), routes_ready=True,
                    hook_error=None, hook_event='route-up', phase='network-configured')
    else:
        command('ip', 'route', 'del', 'table', '100', 'default', 'dev', 'tun0', 'metric', '10', check=False)
        resolver()
        data.update(routes_ready=False, dns=[], domains=[], dns_pushed=False,
                    initialization_completed=False, hook_event=os.environ.get('script_type'), phase='disconnected')
    write_state(data)
    print('GATEWAY_HOOK '+str(data['hook_event'])+' completed; DNS pushed='+str(data['dns_pushed']), flush=True)

def run_hook():
    """Surface controlled error details instead of discarding a Python traceback."""
    try:
        hook()
        return 0
    except Exception as exc:
        event=os.environ.get('script_type','unknown')
        if event not in ('route-up','route-pre-down','down'):
            event='unknown'
        detail=type(exc).__name__
        if isinstance(exc, subprocess.CalledProcessError):
            detail += ' exit='+str(exc.returncode)+' '+(exc.stderr or '').strip()[:180]
        elif isinstance(exc, FileNotFoundError):
            detail += ' missing executable/file'
        data=read_state()
        data.update(ready=False, routes_ready=False, phase='hook-failed',
                    hook_event=event, hook_error=detail)
        write_state(data)
        print('GATEWAY_HOOK_ERROR '+event+': '+detail, flush=True)
        return 1

def initialization_event(protocol):
    data=read_state()
    data.update(ready=bool(data.get('routes_ready')) and not data.get('hook_error'),
                initialization_completed=True, connected_at=time.time(), transport=protocol)
    data['phase']='ready' if data['ready'] else 'hook-failed'
    write_state(data)

def firewall(endpoint, port, protocol):
    # Each rule is inside this container namespace, never macOS.
    for tool in ('iptables', 'ip6tables'):
        for chain in ('INPUT', 'OUTPUT', 'FORWARD'):
            command(tool, '-P', chain, 'DROP')
            command(tool, '-F', chain)
    def rule(chain, *args):
        command('iptables', '-A', chain, *args)
    rule('INPUT', '-i', 'lo', '-j', 'ACCEPT')
    rule('INPUT', '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '-j', 'ACCEPT')
    rule('INPUT', '-i', 'eth0', '-p', 'tcp', '--dport', '1080', '-j', 'ACCEPT')
    # No general ESTABLISHED allow on OUTPUT: existing proxy connections must
    # also be blocked if their route ever changes to eth0.
    rule('OUTPUT', '-o', 'tun0', '-j', 'ACCEPT')
    rule('OUTPUT', '-p', 'tcp', '--sport', '1080', '-m', 'conntrack',
         '--ctstate', 'ESTABLISHED', '--ctdir', 'REPLY', '-j', 'ACCEPT')
    rule('OUTPUT', '-o', 'lo', '-m', 'owner', '--uid-owner', '0', '-j', 'ACCEPT')
    rule('OUTPUT', '-o', 'eth0', '-d', endpoint, '-p', protocol, '--dport', str(port),
         '-m', 'owner', '--uid-owner', '0', '-j', 'ACCEPT')
    rule('OUTPUT', '-j', 'REJECT', '--reject-with', 'icmp-admin-prohibited')

    # Proxy target sockets (UID 10000) have a dedicated routing table. An
    # unreachable route prevents fall-through when tun0 disappears.
    routes = json.loads(command('ip', '-j', '-4', 'route', 'show', 'default').stdout)
    gateway = next(x['gateway'] for x in routes if x.get('dev') == 'eth0')
    command('ip', 'route', 'replace', endpoint + '/32', 'via', gateway, 'dev', 'eth0')
    command('ip', 'route', 'replace', 'table', '101', 'default', 'via', gateway, 'dev', 'eth0', 'onlink')
    command('ip', 'route', 'replace', 'unreachable', 'default', 'table', '100', 'metric', '32767')
    command('ip', 'rule', 'add', 'priority', '90', 'ipproto', 'tcp', 'sport', '1080', 'lookup', '101')
    command('ip', 'rule', 'add', 'priority', '100', 'uidrange', '10000-10000', 'lookup', '100')

def endpoint_config(text):
    remote, proxy = None, None
    block = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith('<'):
            block = not s.startswith('</')
            continue
        if block or not s or s.startswith(('#', ';')):
            continue
        p = shlex.split(s)
        if p[0] == 'remote':
            remote = p
        if p[0] == 'http-proxy':
            proxy = p
    if not remote:
        raise ValueError('Missing remote')
    endpoint_name = proxy[1] if proxy else remote[1]
    endpoint = socket.getaddrinfo(endpoint_name, None, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
    if not proxy:
        text = re.sub(r'(?m)^remote\s+[^\r\n]+', f'remote {endpoint} {remote[2]} {remote[3]}', text)
    protocol = 'tcp' if proxy or 'tcp' in remote[3].lower() else 'udp'
    return text, endpoint, int(proxy[2] if proxy else remote[2]), protocol

def healthy():
    data = read_state()
    result = command('ip', '-4', '-o', 'addr', 'show', 'dev', 'tun0', check=False)
    route = command('ip', '-4', 'route', 'get', '1.1.1.1', 'uid', '10000', check=False)
    return bool(data.get('ready') and result.returncode == 0 and 'inet ' in result.stdout
                and route.returncode == 0 and 'dev tun0' in route.stdout)

def safe_line(line, secrets):
    # The original OpenVPN log stream is NEVER saved. TLS certificate DNs,
    # PUSH_REPLY, peer strings and arbitrary server messages are not emitted.
    if 'AUTH_FAILED' in line:
        return 'AUTH_FAILED (server rejected authentication; details omitted)'
    keep = ('OpenVPN 2.', 'library versions:', 'Initialization Sequence Completed',
            'TLS Error:', 'TLS key negotiation failed', 'TCP/UDP:', 'UDPv4 link',
            'TCPv4 link', 'TCP connection established', 'Attempting to establish TCP',
            'HTTP proxy returned', 'HTTP proxy requires', 'HTTP proxy status',
            'SIGUSR1[', 'SIGTERM[', 'Inactivity timeout', 'Connection reset',
            'Connection refused', 'Network is unreachable', 'Exiting due to fatal error',
            'Options error:', 'ERROR:', 'GATEWAY_HOOK', 'TUN/TAP device', 'RTNETLINK answers:',
            'WARNING: Failed running command', 'Cannot resolve host address:',
            'HTTP proxy returned bad status', 'HTTP proxy returned bad version',
            'WARNING: this configuration may cache passwords')
    if not any(s in line for s in keep):
        return None
    for secret in secrets:
        if secret:
            line = line.replace(secret, '[REDACTED]')
    return line.strip()[:600]

def supervise():
    os.umask(0o077)
    STATE.mkdir(exist_ok=True)
    resolver()
    write_state({'ready': False, 'initialization_completed':False, 'phase':'starting',
                 'dns_pushed': False, 'started_at': time.time()})
    text, endpoint, port, protocol = endpoint_config(Path('/config/client.ovpn').read_text())
    firewall(endpoint, port, protocol)
    Path('/run/client.ovpn').write_text(text)
    secrets = Path('/run/credentials/auth').read_text().splitlines()
    args = ['/usr/sbin/openvpn', '--config', '/run/client.ovpn', '--auth-user-pass', '/run/credentials/auth',
            '--auth-nocache', '--auth-retry', 'none', '--dev', 'tun0', '--disable-dco',
            # Only UID 10000's table 100 sends proxy traffic through tun0.
            # Keep OpenVPN's automatic routes away from our pinned endpoint
            # and reply routes. Unlike route-nopull, this preserves pushed DNS.
            '--route-noexec', '--script-security', '2', '--route-up', '/opt/gateway/vpn.py hook',
            '--route-pre-down', '/opt/gateway/vpn.py hook', '--down', '/opt/gateway/vpn.py hook',
            '--down-pre', '--up-restart', '--verb', '3', '--connect-retry-max', '2',
            '--connect-timeout', '12', '--ping', '10', '--ping-restart', '60']
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    Path('/run/openvpn.pid').write_text(str(proc.pid))
    def terminate(*_):
        if proc.poll() is None:
            proc.terminate()
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        with (STATE / 'safe.log').open('a', buffering=1) as log:
            for line in proc.stdout:
                if 'Initialization Sequence Completed' in line:
                    initialization_event(protocol)
                elif any(s in line for s in ('SIGUSR1[', 'SIGTERM[', 'AUTH_FAILED', 'Exiting due to fatal')):
                    data = read_state(); data['ready'] = False; write_state(data)
                cleaned = safe_line(line, secrets)
                if cleaned:
                    log.write(cleaned + '\n')
                    print(cleaned, flush=True)
        return proc.wait()
    finally:
        terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
        data = read_state(); data.update(ready=False, stopped_at=time.time()); write_state(data)
        resolver()

if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else 'run'
    if action == 'hook':
        sys.exit(run_hook())
    elif action == 'health':
        sys.exit(0 if healthy() else 1)
    else:
        try:
            sys.exit(supervise())
        except Exception as exc:
            # Exception text could contain an untrusted configuration line.
            print('Gateway setup failed:', type(exc).__name__, file=sys.stderr)
            sys.exit(1)
