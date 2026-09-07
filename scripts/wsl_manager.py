#!/usr/bin/python3
"""Root-side lifecycle for the dedicated IsolatedOpenVPNGateway WSL distro."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import sys
import time
import ipaddress
import socket


PROJECT = 'isolated-openvpn-gateway'
BASE = Path('/opt/isolated-openvpn-gateway')
ETC = Path('/etc/isolated-openvpn-gateway')
RUN = Path('/run/isolated-openvpn-gateway')
STATE = Path('/var/lib/isolated-openvpn-gateway/state')
AUTH = RUN / 'auth'
PROFILE = ETC / 'client.ovpn'
OWNER = ETC / 'ownership.json'
VPN_PID = RUN / 'vpn.pid'
SOCKS_PID = RUN / 'socks.pid'
NETNS = 'isolated-openvpn-gateway'
NETNS_ETC = Path('/etc/netns') / NETNS
INNER_RESOLV = NETNS_ETC / 'resolv.conf'
NETNS_PATH = Path('/run/netns') / NETNS
NETNS_PID = RUN / 'netns.pid'
SLIRP_PID = RUN / 'slirp.pid'


ENVIRONMENT = {
    'GATEWAY_BACKEND': 'wsl',
    'GATEWAY_STATE': str(STATE),
    'GATEWAY_CONFIG': str(PROFILE),
    'GATEWAY_AUTH': str(AUTH),
    'GATEWAY_RUNTIME': str(RUN),
    'GATEWAY_RESOLV_CONF': '/etc/resolv.conf',
    'GATEWAY_OUTER_RESOLV': '/etc/isolated-openvpn-gateway/resolv.outer',
    'GATEWAY_SOCKD_CONFIG': '/etc/isolated-openvpn-gateway/sockd.conf',
    'GATEWAY_SOCKD_BINARY': '/usr/sbin/danted',
}


VPN_UNIT = '''[Unit]
Description=Isolated OpenVPN Gateway VPN
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
UMask=0077
NetworkNamespacePath=/run/netns/isolated-openvpn-gateway
BindPaths=/etc/netns/isolated-openvpn-gateway/resolv.conf:/etc/resolv.conf
Environment=GATEWAY_BACKEND=wsl
Environment=GATEWAY_STATE=/var/lib/isolated-openvpn-gateway/state
Environment=GATEWAY_CONFIG=/etc/isolated-openvpn-gateway/client.ovpn
Environment=GATEWAY_AUTH=/run/isolated-openvpn-gateway/auth
Environment=GATEWAY_RUNTIME=/run/isolated-openvpn-gateway
Environment=GATEWAY_RESOLV_CONF=/etc/resolv.conf
Environment=GATEWAY_OUTER_RESOLV=/etc/isolated-openvpn-gateway/resolv.outer
ExecStart=/usr/bin/python3 -u /opt/isolated-openvpn-gateway/vpn.py
Restart=no
KillMode=control-group
TimeoutStopSec=12

[Install]
WantedBy=multi-user.target
'''


SOCKS_UNIT = '''[Unit]
Description=Isolated OpenVPN Gateway loopback SOCKS
After=isolated-openvpn-gateway-vpn.service

[Service]
Type=simple
UMask=0077
NetworkNamespacePath=/run/netns/isolated-openvpn-gateway
BindReadOnlyPaths=/etc/netns/isolated-openvpn-gateway/resolv.conf:/etc/resolv.conf
Environment=GATEWAY_STATE=/var/lib/isolated-openvpn-gateway/state
Environment=GATEWAY_SOCKD_CONFIG=/etc/isolated-openvpn-gateway/sockd.conf
Environment=GATEWAY_SOCKD_BINARY=/usr/sbin/danted
User=proxyuser
Group=proxyuser
ExecStart=/usr/bin/python3 -u /opt/isolated-openvpn-gateway/socks.py
Restart=no
KillMode=control-group
TimeoutStopSec=8

[Install]
WantedBy=multi-user.target
'''


def run(args, *, check=True, **kwargs):
    result = subprocess.run([str(value) for value in args], capture_output=True, text=True, **kwargs)
    if check and result.returncode:
        raise RuntimeError('managed WSL command failed: ' + Path(str(args[0])).name)
    return result


def private_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError('refusing managed-path symlink')
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags, mode)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
    path.chmod(mode)


def require_root():
    if os.geteuid() != 0:
        raise RuntimeError('managed WSL lifecycle requires root inside its dedicated distro')


def owned():
    try:
        return json.loads(OWNER.read_text()).get('project') == PROJECT
    except (OSError, ValueError):
        return False


def systemd_available():
    try:
        return Path('/proc/1/comm').read_text().strip() == 'systemd' and \
            run(['/usr/bin/systemctl', 'is-system-running'], check=False).returncode in (0, 1)
    except OSError:
        return False


def inside(args, *, check=True, **kwargs):
    return run(['/usr/sbin/ip', 'netns', 'exec', NETNS, *args], check=check, **kwargs)


def network_ready():
    if not NETNS_PATH.exists() or not pid_alive(NETNS_PID) or not pid_alive(SLIRP_PID):
        return False
    result = inside(['/sbin/ip', '-4', 'route', 'show', 'default'], check=False)
    return result.returncode == 0 and 'dev eth0' in result.stdout


def network_stop():
    kill_pid(SLIRP_PID)
    kill_pid(NETNS_PID)
    run(['/usr/sbin/ip', 'netns', 'delete', NETNS], check=False)
    restore_resolver('host')


def network_start():
    require_root()
    if network_ready():
        return
    network_stop()
    restore_resolver('host')
    run(['/usr/sbin/ip', 'netns', 'add', NETNS])
    keeper = subprocess.Popen(['/usr/sbin/ip', 'netns', 'exec', NETNS,
                               '/usr/bin/sleep', 'infinity'], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
    private_write(NETNS_PID, (str(keeper.pid) + '\n').encode())
    slirp = subprocess.Popen(['/usr/bin/slirp4netns', '--configure', '--mtu=65520',
                              '--disable-host-loopback', '--cidr=10.0.2.0/24',
                              str(keeper.pid), 'eth0'], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    private_write(SLIRP_PID, (str(slirp.pid) + '\n').encode())
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if slirp.poll() is not None:
            break
        if network_ready():
            restore_resolver('outer')
            return
        time.sleep(0.2)
    network_stop()
    raise RuntimeError('isolated WSL network namespace did not become ready')


def restore_resolver(kind):
    source = ETC / ('resolv.' + kind)
    if source.is_file():
        private_write(INNER_RESOLV, source.read_bytes(), 0o644)


def ensure_proxy_user():
    try:
        account = pwd.getpwnam('proxyuser')
        if account.pw_uid != 10000:
            raise RuntimeError('proxyuser exists with an unexpected UID')
        return
    except KeyError:
        pass
    try:
        collision = pwd.getpwuid(10000)
    except KeyError:
        collision = None
    if collision:
        raise RuntimeError('UID 10000 is already owned by another account')
    run(['/usr/sbin/groupadd', '--gid', '10000', 'proxyuser'], check=False)
    run(['/usr/sbin/useradd', '--uid', '10000', '--gid', '10000', '--no-create-home',
         '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', 'proxyuser'])


def provision():
    require_root()
    required = ('/usr/sbin/openvpn', '/usr/sbin/danted', '/usr/sbin/iptables',
                '/usr/sbin/ip6tables', '/usr/sbin/ip', '/usr/bin/curl', '/usr/bin/python3',
                '/usr/bin/slirp4netns', '/usr/bin/sleep', '/usr/sbin/runuser')
    if any(not Path(item).is_file() for item in required):
        raise RuntimeError('required Debian packages are missing')
    if not Path('/dev/net/tun').exists():
        raise RuntimeError('/dev/net/tun is unavailable in WSL2')
    ensure_proxy_user()
    original_resolver = Path('/etc/resolv.conf').read_bytes()
    if not original_resolver or len(original_resolver) > 65536 or b'\x00' in original_resolver:
        raise RuntimeError('WSL host resolver is missing or unsafe')
    for directory in (BASE, ETC, RUN, STATE):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    for directory in (BASE, ETC, STATE):
        os.chown(directory, 0, 10000)
        directory.chmod(0o750)
    run(['/usr/bin/systemctl', 'disable', '--now', 'danted.service'], check=False)
    private_write(OWNER, (json.dumps({'project': PROJECT, 'managed': True}) + '\n').encode())
    private_write(ETC/'resolv.host', original_resolver, 0o644)
    outer_text = b'nameserver 10.0.2.3\noptions timeout:2 attempts:2\n'
    private_write(ETC/'resolv.outer', outer_text, 0o644)
    NETNS_ETC.mkdir(mode=0o755, parents=True, exist_ok=True)
    private_write(INNER_RESOLV, outer_text, 0o644)
    private_write('/etc/systemd/system/isolated-openvpn-gateway-vpn.service', VPN_UNIT.encode(), 0o644)
    private_write('/etc/systemd/system/isolated-openvpn-gateway-socks.service', SOCKS_UNIT.encode(), 0o644)
    # This is per-distribution configuration. The host-wide .wslconfig is untouched.
    # Keep WSL DNS tunneling intact for the outer slirp process. Only gateway
    # processes see the bind-mounted namespace resolver (also under systemd).
    private_write('/etc/wsl.conf', b'[boot]\nsystemd=true\n[network]\ngenerateResolvConf=true\n[user]\ndefault=root\n', 0o644)
    run(['/usr/bin/systemctl', 'daemon-reload'], check=False)
    print(json.dumps({'project': PROJECT, 'tun': True, 'systemd': systemd_available()}))


def put(path, limit):
    require_root()
    if not owned():
        raise RuntimeError('managed distro ownership marker is absent')
    data = sys.stdin.buffer.read(limit + 1)
    if not data or len(data) > limit or b'\x00' in data:
        raise RuntimeError('refusing empty, oversized, or NUL-containing private input')
    private_write(path, data)


def pid_alive(path):
    try:
        value = int(path.read_text())
        os.kill(value, 0)
        return value
    except (OSError, ValueError):
        return None


def kill_pid(path, *, keep_file=False):
    value = pid_alive(path)
    if value:
        with contextlib.suppress(ProcessLookupError):
            os.kill(value, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                os.kill(value, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.kill(value, signal.SIGKILL)
    if not keep_file:
        path.unlink(missing_ok=True)


def fallback_start():
    environment = dict(os.environ, **ENVIRONMENT)
    if not pid_alive(VPN_PID):
        process = subprocess.Popen(['/usr/sbin/ip', 'netns', 'exec', NETNS,
                                    '/usr/bin/python3', '-u', str(BASE/'vpn.py')],
                                   env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        private_write(VPN_PID, (str(process.pid) + '\n').encode())
    if not pid_alive(SOCKS_PID):
        process = subprocess.Popen(['/usr/sbin/ip', 'netns', 'exec', NETNS,
                                    '/usr/sbin/runuser', '-u', 'proxyuser', '--',
                                    '/usr/bin/python3', '-u', str(BASE/'socks.py')],
                                   env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        private_write(SOCKS_PID, (str(process.pid) + '\n').encode())


def start():
    require_root()
    if not owned() or not PROFILE.is_file() or not AUTH.is_file():
        raise RuntimeError('managed WSL backend is not provisioned or private runtime input is absent')
    network_start()
    if systemd_available():
        run(['/usr/bin/systemctl', 'start', 'isolated-openvpn-gateway-vpn.service'])
        run(['/usr/bin/systemctl', 'start', 'isolated-openvpn-gateway-socks.service'])
        mode = 'systemd'
    else:
        fallback_start()
        mode = 'fallback'
    print(json.dumps({'started': True, 'mode': mode}))


def stop(*, keep_auth=False, vpn_only=False):
    require_root()
    if systemd_available():
        units = ['isolated-openvpn-gateway-vpn.service'] if vpn_only else [
            'isolated-openvpn-gateway-socks.service', 'isolated-openvpn-gateway-vpn.service']
        run(['/usr/bin/systemctl', 'stop', *units], check=False)
    else:
        if not vpn_only:
            kill_pid(SOCKS_PID)
        kill_pid(VPN_PID)
    if not vpn_only and NETNS_PATH.exists():
        inside(['/usr/bin/python3', str(BASE/'vpn.py'), 'cleanup-keep-auth' if keep_auth else 'cleanup'],
               env=dict(os.environ, **ENVIRONMENT), check=False)
        network_stop()
    if not keep_auth:
        AUTH.unlink(missing_ok=True)


def status():
    try:
        data = json.loads((STATE/'status.json').read_text())
    except (OSError, ValueError):
        data = {}
    namespace = network_ready()
    tun_exists = namespace and inside(['/usr/bin/test', '-d', '/sys/class/net/tun0'], check=False).returncode == 0
    socks = inside(['/usr/bin/ss', '-H', '-lnt', 'sport', '=', ':11080'], check=False).stdout.strip() \
        if namespace else ''
    data.update(owned=owned(), systemd=systemd_available(), network_namespace=namespace,
                tun_exists=tun_exists, auth_present=AUTH.is_file(), socks_loopback=socks,
                openvpn_running=openvpn_running())
    print(json.dumps(data))


def openvpn_running():
    try:
        pid = int((RUN/'openvpn.pid').read_text())
        info = Path('/proc', str(pid), 'stat').read_text().split(') ', 1)
        return info[0].endswith('(openvpn') and info[1].split()[0] != 'Z'
    except (OSError, ValueError, IndexError):
        return False


def bridge():
    if not network_ready():
        raise RuntimeError('isolated WSL network namespace is unavailable')
    os.execv('/usr/sbin/ip', ['/usr/sbin/ip', 'netns', 'exec', NETNS, '/usr/bin/python3',
                              str(BASE/'wsl_bridge.py')])


def network_egress():
    network_start()
    result = inside(['/usr/bin/curl', '-4', '--noproxy', '*', '-fsS', '--max-time', '10',
                     '--connect-timeout', '5', '--retry', '1', '--retry-delay', '1', '--retry-all-errors',
                     'https://checkip.amazonaws.com'], check=False)
    if result.returncode:
        raise RuntimeError('isolated network namespace egress is unavailable')
    print(result.stdout.strip())


def proxy_outer_test():
    """Prove the UID firewall, independently of the unreachable policy route."""
    if not network_ready():
        raise RuntimeError('isolated WSL network namespace is unavailable')
    # Use the same HTTPS service as preflight. A fixed HTTP canary may itself
    # be blocked by the outer VPN, which cannot prove anything about our rules.
    address = socket.getaddrinfo('checkip.amazonaws.com', 443, socket.AF_INET,
                                 socket.SOCK_STREAM)[0][4][0]
    if not ipaddress.ip_address(address).is_global:
        raise RuntimeError('public firewall canary resolved to a non-public address')
    curl = ['/usr/bin/curl', '-4', '--interface', 'eth0', '--noproxy', '*', '-fsS',
            '--resolve', 'checkip.amazonaws.com:443:' + address, '-o', '/dev/null',
            '--connect-timeout', '4', '--max-time', '6', 'https://checkip.amazonaws.com']
    control = inside(curl, check=False).returncode == 0
    evidence = {'outer_control_succeeded':control, 'proxy_outer_blocked':False,
                'firewall_reject_observed':False, 'probe_route_removed':False}
    if control:
        before = firewall_reject_count()
        # This narrowly scoped probe bypasses table 100 ONLY for this UID and
        # public canary. The firewall stays active; its counter must increase.
        rule = ['priority', '95', 'to', address + '/32', 'uidrange', '10000-10000', 'lookup', 'main']
        inside(['/usr/sbin/ip', 'rule', 'add', *rule])
        try:
            result = inside(['/usr/sbin/runuser', '-u', 'proxyuser', '--', *curl], check=False)
            evidence['proxy_outer_blocked'] = result.returncode != 0
            evidence['firewall_reject_observed'] = firewall_reject_count() > before
        finally:
            evidence['probe_route_removed'] = inside(
                ['/usr/sbin/ip', 'rule', 'del', *rule], check=False).returncode == 0
    print(json.dumps(evidence))
    return 0


def firewall_reject_count():
    output = inside(['/usr/sbin/iptables', '-n', '-v', '-x', '-L', 'IOVG_WSL_PROXY']).stdout
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2] == 'REJECT':
            return int(fields[0])
    raise RuntimeError('SOCKS firewall reject rule is missing')


def remove_state():
    require_root()
    if not owned():
        raise RuntimeError('managed distro ownership marker is absent')
    stop()
    for unit in ('isolated-openvpn-gateway-vpn.service', 'isolated-openvpn-gateway-socks.service'):
        Path('/etc/systemd/system', unit).unlink(missing_ok=True)
    run(['/usr/bin/systemctl', 'daemon-reload'], check=False)
    for path in (STATE, RUN, ETC, BASE, NETNS_ETC):
        if path.exists() and not path.is_symlink():
            shutil.rmtree(path)


def main(argv=None):
    require_root()
    action = (argv or sys.argv[1:] or ['status'])[0]
    if action == 'provision': provision()
    elif action == 'put-profile': put(PROFILE, 256 * 1024)
    elif action == 'put-auth': put(AUTH, 16 * 1024)
    elif action == 'cleanup-auth': AUTH.unlink(missing_ok=True)
    elif action == 'network-start': network_start()
    elif action == 'network-stop': network_stop()
    elif action == 'network-egress': network_egress()
    elif action == 'proxy-outer-test': return proxy_outer_test()
    elif action == 'bridge': bridge()
    elif action == 'start': start()
    elif action == 'stop': stop()
    elif action == 'stop-keep-auth': stop(keep_auth=True)
    elif action == 'stop-vpn': stop(keep_auth=True, vpn_only=True)
    elif action == 'status': status()
    elif action == 'remove-state': remove_state()
    else: return 2
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('managed WSL action failed: ' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
