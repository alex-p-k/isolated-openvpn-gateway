#!/usr/bin/env python3
"""Host lifecycle. This program never starts OpenVPN on the host OS."""
import contextlib
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from urllib.parse import urlsplit

from gateway_config import (BROWSER_CLI, CLI, PROJECT, SOCKS_IMAGE, VPN_IMAGE,
                            ConfigError, load_config)
import host
import wsl_backend

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / 'runtime'
STATE = RUNTIME / 'state'
AUTH = RUNTIME / 'auth'
PREFS = ROOT / 'settings.json'
DOCKER = host.docker_executable()
CURL = host.curl_executable()
POWERSHELL = host.powershell_executable()
WSL = host.wsl_executable()
IP_URL = 'https://checkip.amazonaws.com'
IMAGE = VPN_IMAGE
ENV = {k:v for k,v in os.environ.items() if k not in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')}
WSL_DISTRO = wsl_backend.DISTRO
FORWARDER_PID = RUNTIME / 'wsl-forwarder.pid'

class GatewayError(Exception):
    pass

def configuration():
    path = ROOT / 'gateway.toml'
    if not path.is_file():
        path = ROOT / 'gateway.example.toml'
    try:
        return load_config(path)
    except ConfigError as exc:
        raise GatewayError(str(exc)) from None

def run(args, *, check=True, timeout=40, capture=True, env=None, **kwargs):
    child_env = ENV if env is None else env
    if str(args[0]).replace('\\', '/').rsplit('/', 1)[-1].casefold() == 'powershell.exe':
        # Python launched by PS7 inherits PS7 module paths, which Windows
        # PowerShell 5.1 cannot load. Let that child rebuild its default paths;
        # never mutate parent/system environment or execution policy.
        child_env = {key: value for key, value in child_env.items() if key.upper() != 'PSMODULEPATH'}
    if str(args[0]).casefold() == str(POWERSHELL).casefold() and '-Command' in args:
        # powershell() explicitly writes UTF-8; do not decode it using the
        # current Windows ANSI locale (which may differ from the console).
        kwargs.setdefault('encoding', 'utf-8')
    if capture and str(args[0]).lower().endswith(('wsl.exe', '/wsl')) and '--exec' not in args:
        result = subprocess.run([str(x) for x in args], env=child_env,
                                capture_output=True, timeout=timeout, **kwargs)
        result.stdout = wsl_backend.normalize_output(result.stdout)
        result.stderr = wsl_backend.normalize_output(result.stderr)
        if check and result.returncode:
            raise GatewayError((result.stderr or result.stdout or 'WSL command failed')[-1400:])
        return result
    # Text-mode stdin on Windows silently translates LF to CRLF. That breaks
    # Linux executable shebangs and can alter auth input. Send exact UTF-8 bytes.
    binary_input = kwargs.get('input') is not None
    if binary_input:
        encoding = kwargs.pop('encoding', 'utf-8')
        errors = kwargs.pop('errors', 'replace')
        if isinstance(kwargs['input'], str):
            kwargs['input'] = kwargs['input'].encode(encoding, errors=errors)
    result = subprocess.run([str(x) for x in args], env=child_env,
                            text=not binary_input, capture_output=capture, timeout=timeout, **kwargs)
    if binary_input and capture:
        result.stdout = result.stdout.decode(encoding, errors=errors)
        result.stderr = result.stderr.decode(encoding, errors=errors)
    if check and result.returncode:
        # Commands never include credentials. Do not dump config/source files.
        raise GatewayError((result.stderr or result.stdout or 'Command failed')[-1400:] if capture else 'Command failed')
    return result

def docker(*args, **kwargs):
    return run([DOCKER, '--context', 'desktop-linux', *args], **kwargs)

def compose(*args, **kwargs):
    cfg = configuration()
    env = dict(ENV, GATEWAY_SOCKS_PORT=str(cfg['socks_port']))
    return run([DOCKER, '--context', 'desktop-linux', 'compose', '--project-name', PROJECT,
                '--project-directory', ROOT, '-f', ROOT/'compose.yaml', *args], env=env, **kwargs)

def private_write(path, text, mode=0o600):
    try:
        if not path.parent.exists():
            host.private_directory(path.parent, parents=True)
        host.private_write(path, text, mode=mode)
    except OSError as exc:
        raise GatewayError(str(exc)) from None

def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {} if default is None else default

def preferences():
    return read_json(PREFS, {'default_transport':'udp', 'backend':'docker'})

def save_preferences(data):
    private_write(PREFS, json.dumps(data, indent=2)+'\n')

def backend_name(explicit=None):
    value = explicit or preferences().get('backend', 'docker')
    if value not in ('docker', 'wsl'):
        raise GatewayError('Backend must be docker or wsl.')
    if value == 'wsl' and not host.IS_WINDOWS:
        raise GatewayError('The native WSL2 backend is available only on Windows; use docker on macOS.')
    return value

def active_backend(explicit=None):
    if explicit:
        return backend_name(explicit)
    path = RUNTIME/'active-backend'
    if path.is_file():
        value = path.read_text().strip()
        if value in ('docker','wsl'):
            return backend_name(value)
    return backend_name()

def parse_cli(argv):
    values = list(argv)
    backend = None
    positionals = []
    index = 0
    while index < len(values):
        value = values[index]
        if value == '--backend':
            if backend is not None or index + 1 >= len(values):
                raise GatewayError('--backend requires one value: docker or wsl.')
            backend = values[index + 1]
            index += 2
            continue
        if value.startswith('--backend='):
            if backend is not None:
                raise GatewayError('--backend may be supplied only once.')
            backend = value.split('=', 1)[1]
            index += 1
            continue
        if value.startswith('-') and value not in ('-h','--help'):
            raise GatewayError('Unknown option: ' + value)
        positionals.append(value)
        index += 1
    action = positionals.pop(0) if positionals else 'status'
    return action, positionals, backend

def wsl(*args, input=None, **kwargs):
    kwargs.setdefault('encoding', 'utf-8')
    kwargs.setdefault('errors', 'replace')
    return run(wsl_backend.wsl_command(*args, executable=WSL), input=input, **kwargs)

def wsl_network(*args, **kwargs):
    """Inspect the gateway's network, never the shared outer WSL namespace."""
    return wsl('/usr/sbin/ip', 'netns', 'exec', PROJECT, *args, **kwargs)

def wsl_names():
    result = run([WSL, '--list', '--quiet'], check=False, timeout=20)
    return wsl_backend.distro_names(result.stdout) if result.returncode == 0 else []

def wsl_owned():
    marker = read_json(ROOT/'installation.json')
    if marker.get('wsl_distro') != WSL_DISTRO or not marker.get('wsl_created_by_project'):
        return False
    result = wsl('/usr/bin/cat', '/etc/isolated-openvpn-gateway/ownership.json',
                 check=False, timeout=20)
    try:
        data = json.loads(result.stdout)
        return result.returncode == 0 and data.get('project') == PROJECT and data.get('managed') is True
    except (TypeError, ValueError):
        return False

def update_installation(**values):
    marker = read_json(ROOT/'installation.json')
    if marker.get('project') != PROJECT:
        raise GatewayError('Installation ownership marker is absent.')
    marker.update(values)
    private_write(ROOT/'installation.json', json.dumps(marker, indent=2)+'\n')

def socks_available():
    try:
        cfg = configuration()
        with socket.create_connection((cfg['socks_host'], cfg['socks_port']), timeout=2) as s:
            # A new WSL stdio bridge may take longer than the TCP accept. TCP
            # may also fragment the two-byte greeting; neither is a rejection.
            s.settimeout(10)
            s.sendall(b'\x05\x01\x00')
            reply = b''
            while len(reply) < 2:
                data = s.recv(2 - len(reply))
                if not data:
                    return False
                reply += data
            return reply == b'\x05\x00'
    except OSError:
        return False

def socks_dns_query(address):
    """Verify a real TCP DNS exchange through SOCKS with a pushed DNS server.

    Unlike a greeting, this proves that Dante can open and carry a connection
    into the private network. It does not require public Internet access.
    """
    def receive(sock, length):
        data = b''
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise OSError('SOCKS stream closed')
            data += chunk
        return data
    try:
        destination = ipaddress.IPv4Address(address).packed
        cfg = configuration()
        with socket.create_connection((cfg['socks_host'], cfg['socks_port']), timeout=4) as sock:
            sock.settimeout(5)
            sock.sendall(b'\x05\x01\x00')
            if receive(sock, 2) != b'\x05\x00':
                return False
            sock.sendall(b'\x05\x01\x00\x01' + destination + struct.pack('!H', 53))
            header = receive(sock, 4)
            if header[:3] != b'\x05\x00\x00':
                return False
            length = {1:4, 4:16}.get(header[3])
            if header[3] == 3:
                length = receive(sock, 1)[0]
            if length is None:
                return False
            receive(sock, length + 2)
            qname = b''.join(bytes([len(x)]) + x.encode('ascii')
                             for x in cfg['dns_canary'].split('.')) + b'\x00'
            transaction = 0x4356
            query = struct.pack('!6H', transaction, 0x100, 1, 0, 0, 0) + qname + struct.pack('!2H', 1, 1)
            sock.sendall(struct.pack('!H', len(query)) + query)
            reply = receive(sock, struct.unpack('!H', receive(sock, 2))[0])
            ident, flags, questions, answers, _, _ = struct.unpack('!6H', reply[:12])
            return (ident == transaction and bool(flags & 0x8000) and flags & 15 == 0
                    and questions == 1 and answers > 0 and len(reply) > 12)
    except (OSError, ValueError, struct.error):
        return False

def public_ip(proxy=False):
    cfg = configuration()
    args = [CURL, '-4', '--noproxy', '' if proxy else '*', '--fail', '--silent', '--show-error',
            '--connect-timeout', '6', '--max-time', '12']
    if not proxy:
        args += ['--retry', '1', '--retry-delay', '1', '--retry-all-errors']
    if proxy:
        args += ['--proxy', 'socks5h://%s:%d' % (cfg['socks_host'], cfg['socks_port'])]
    try:
        result = run([*args, IP_URL], check=False, timeout=30 if not proxy else 20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return str(ipaddress.ip_address(result.stdout.strip())) if result.returncode == 0 else None
    except ValueError:
        return None

def powershell(command):
    # A detached child has no console code page to inherit. Keep JSON names
    # identical in interactive launchers, background checks, and native locales.
    prefix = '[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); '
    return [POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', prefix + command]

def windows_network_commands():
    """Return read-only PowerShell diagnostics with stable, machine-readable output."""
    convert = '; ConvertTo-Json -Compress -Depth 6 -InputObject @($x)'
    def query(command):
        return powershell("$ErrorActionPreference='Stop'; " + command)
    return {
        'dns': query("$x=Get-DnsClientServerAddress -AddressFamily IPv4 | "
                          "Sort-Object InterfaceIndex | Select-Object InterfaceIndex,InterfaceAlias,ServerAddresses" + convert),
        'dns_ipv6': query("$x=Get-DnsClientServerAddress -AddressFamily IPv6 | "
                          "Sort-Object InterfaceIndex | Select-Object InterfaceIndex,InterfaceAlias,ServerAddresses" + convert),
        'routes': query("$x=Get-NetRoute -AddressFamily IPv4 | Where-Object {$_.Protocol -ne 'Local'} | "
                             "Sort-Object DestinationPrefix,InterfaceIndex,NextHop | "
                             "Select-Object DestinationPrefix,NextHop,InterfaceIndex,InterfaceAlias,RouteMetric,Protocol" + convert),
        'routes_ipv6': query("$x=Get-NetRoute -AddressFamily IPv6 | Where-Object {$_.Protocol -ne 'Local'} | "
                             "Sort-Object DestinationPrefix,InterfaceIndex,NextHop | "
                             "Select-Object DestinationPrefix,NextHop,InterfaceIndex,InterfaceAlias,RouteMetric,Protocol" + convert),
        'default': query("$x=Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' | "
                              "Sort-Object RouteMetric,InterfaceMetric | "
                              "Select-Object InterfaceIndex,InterfaceAlias,NextHop,RouteMetric,InterfaceMetric" + convert),
        'default_ipv6': query("$x=Get-NetRoute -AddressFamily IPv6 | Where-Object {$_.DestinationPrefix -eq '::/0'} | "
                              "Sort-Object RouteMetric,InterfaceMetric | "
                              "Select-Object InterfaceIndex,InterfaceAlias,NextHop,RouteMetric,InterfaceMetric" + convert),
        'public_route': query("$x=Find-NetRoute -RemoteIPAddress 1.1.1.1 | "
                                   "Select-Object InterfaceIndex,InterfaceAlias,NextHop,RouteMetric" + convert),
        'adapters': query("$x=Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | "
                               "Sort-Object ifIndex | Select-Object Name,InterfaceDescription,ifIndex,Status" + convert),
        'git_global_proxy': ['git','config','--global','--get-regexp',
                             r'^(http\..*proxy|http\.proxy|core\.sshCommand|url\..*\.insteadOf)$'],
    }

def network_snapshot():
    commands = windows_network_commands() if host.IS_WINDOWS else {
        'dns':['scutil','--dns'], 'routes':['netstat','-rn','-f','inet'],
        'default':['route','-n','get','default'], 'public_route':['route','-n','get','1.1.1.1'],
        'git_global_proxy':['git','config','--global','--get-regexp',
                            r'^(http\..*proxy|http\.proxy|core\.sshCommand|url\..*\.insteadOf)$']}
    data = {'captured_at': time.time(), 'public_ip':public_ip()}
    for key, cmd in commands.items():
        r = run(cmd, check=False)
        data[key] = {'code':r.returncode, 'stdout':r.stdout, 'stderr':r.stderr}
    required = ('dns','routes','default','public_route')
    if host.IS_WINDOWS:
        required += ('dns_ipv6','routes_ipv6','default_ipv6','adapters')
    data['diagnostics_ok'] = all(data[key]['code'] == 0 for key in required)
    if host.IS_WINDOWS:
        try:
            data['diagnostics_ok'] = data['diagnostics_ok'] and all(
                isinstance(json.loads(data[key]['stdout']), list) for key in required)
        except (TypeError, ValueError):
            data['diagnostics_ok'] = False
    if host.IS_WINDOWS:
        data['static_routes'] = data['routes']['stdout'].strip()
        data['public_route_signature'] = data['public_route']['stdout'].strip()
        data['outer_adapters'] = data['adapters']['stdout'].strip()
    else:
        data['static_routes'] = [' '.join(line.split()[:4]) for line in data['routes']['stdout'].splitlines()
                                 if len(line.split()) >= 4 and 'S' in line.split()[2]]
        data['public_route_signature'] = '\n'.join(line.strip() for line in data['public_route']['stdout'].splitlines()
                                                   if re.match(r'\s*(destination|mask|gateway|interface):',line))
    return data

def windows_outer_route_matches(snapshot, matchers):
    """Require the selected public route to use a configured, connected adapter."""
    try:
        adapters = json.loads(snapshot['adapters']['stdout'])
        routes = json.loads(snapshot['public_route']['stdout'])
    except (KeyError, TypeError, ValueError):
        return False
    if not isinstance(adapters, list):
        adapters = [adapters]
    if not isinstance(routes, list):
        routes = [routes]
    wanted = []
    for adapter in adapters:
        if not isinstance(adapter, dict) or str(adapter.get('Status', '')).casefold() != 'up':
            continue
        identity = (str(adapter.get('Name', '')) + '\n' +
                    str(adapter.get('InterfaceDescription', ''))).casefold()
        if any(value.casefold() in identity for value in matchers):
            try:
                wanted.append(int(adapter.get('ifIndex', adapter.get('InterfaceIndex'))))
            except (TypeError, ValueError):
                pass
    for route in routes:
        if isinstance(route, dict):
            try:
                if int(route.get('InterfaceIndex', route.get('ifIndex'))) in wanted:
                    return True
            except (TypeError, ValueError):
                pass
    return False

def compare_host(before, after):
    result = {'host_diagnostics_available':before.get('diagnostics_ok') is True and after.get('diagnostics_ok') is True,
            'host_dns_preserved': before['dns'] == after['dns'],
            'host_default_route_preserved':before['default'] == after['default'],
            'host_static_routes_preserved':before['static_routes'] == after['static_routes'],
            'host_public_route_preserved':before['public_route_signature'] == after['public_route_signature'],
            'host_outer_ip_preserved':bool(before['public_ip']) and before['public_ip'] == after['public_ip'],
            'global_git_proxy_preserved':before['git_global_proxy'] == after['git_global_proxy']}
    if host.IS_WINDOWS:
        for name in ('dns','routes','default'):
            old, new = before.get(name+'_ipv6'), after.get(name+'_ipv6')
            result['host_ipv6_'+name+'_preserved'] = (
                isinstance(old, dict) and old.get('code') == 0 and old == new)
    return result

def engine():
    result = docker('info','--format','{{.OSType}}',check=False)
    if result.returncode:
        raise GatewayError('Docker Desktop Engine is stopped. Start Docker Desktop, then retry.')
    if result.stdout.strip().lower() != 'linux':
        raise GatewayError('Docker backend requires Docker Desktop Linux containers; Windows containers are unsupported.')

def images():
    for image in (IMAGE, SOCKS_IMAGE):
        if docker('image','inspect', image, check=False).returncode:
            print('Building private gateway images (no configuration or credentials in build context).',flush=True)
            compose('build', timeout=600, capture=False)
            return

def wsl_version():
    result = run([WSL, '--version'], check=False, timeout=20)
    return result.stdout + result.stderr

def wsl_path(path):
    result = wsl('/usr/bin/wslpath', '-a', '-u', str(Path(path).resolve()), timeout=20)
    value = wsl_backend.normalize_output(result.stdout).strip()
    if not value.startswith('/') or any(character in value for character in ('\r','\n','\x00')):
        raise GatewayError('WSL could not map an installation path safely.')
    return value

def copy_wsl_asset(source, destination, mode='0555'):
    source = Path(source)
    if not source.is_file() or source.is_symlink():
        raise GatewayError('Managed WSL source asset is missing or unsafe: ' + source.name)
    # Windows Git checkouts commonly use CRLF. A Linux executable shebang
    # must use LF, and stdin avoids quoting/DrvFS/packaged-app path surprises.
    content = source.read_text(encoding='utf-8').replace('\r\n', '\n')
    writer = ('import os,pathlib,sys; p=pathlib.Path(sys.argv[1]); '
              'p.parent.mkdir(parents=True,exist_ok=True); '
              'p.write_bytes(sys.stdin.buffer.read()); p.chmod(int(sys.argv[2],8))')
    wsl('/usr/bin/python3', '-c', writer, destination, mode, input=content, timeout=30)

def install_backend(selected):
    selected = backend_name(selected)
    if selected == 'docker':
        engine(); images()
        print('Docker backend is available. The saved backend was not changed.')
        return
    version = wsl_version()
    if not wsl_backend.modern_install_supported(version):
        raise GatewayError('WSL 2.4.4 or newer is required for a separately named managed distro. '
                           'Install/update WSL, reboot if requested, then retry; Docker is not required.')
    existing = wsl_names()
    if WSL_DISTRO in existing:
        if not wsl_owned():
            raise GatewayError('A WSL distro named '+WSL_DISTRO+' already exists but is not owned by this installation. '
                               'It was not modified or adopted.')
        marker = read_json(ROOT/'installation.json')
        if marker.get('wsl_install_phase', 'ready') == 'ready':
            print('Managed WSL backend is already installed. The saved backend was not changed.')
            return
        provision_wsl_backend(ROOT/'wsl')
        return
    location = ROOT/'wsl'
    if location.exists() and any(location.iterdir()):
        raise GatewayError('Managed WSL storage path already exists and is not empty; nothing was changed.')
    host.private_directory(location, parents=True, exist_ok=True)
    created = False
    try:
        result = run(wsl_backend.install_command(location, executable=WSL), check=False,
                     timeout=900, capture=False)
        if result.returncode or WSL_DISTRO not in wsl_names():
            raise GatewayError('WSL could not install the separate Debian distro. A reboot or one-time '
                               'Administrator WSL enablement may be required.')
        created = True
        listing = run([WSL, '--list', '--verbose'], timeout=20)
        if wsl_backend.distro_version(listing.stdout, WSL_DISTRO) != 2:
            run([WSL, '--set-version', WSL_DISTRO, '2'], timeout=300, capture=False)
        provision_wsl_backend(location)
    except BaseException:
        if created:
            run(wsl_backend.unregister_command(executable=WSL), check=False, timeout=180, capture=False)
            update_installation(wsl_distro=None, wsl_created_by_project=False, wsl_location=None)
        with contextlib.suppress(OSError):
            if location.exists() and not any(location.iterdir()):
                location.rmdir()
        raise

def provision_wsl_backend(location):
    print('Installing OpenVPN, Dante, firewall and diagnostic packages in the managed WSL distro.', flush=True)
    wsl('/usr/bin/env', 'DEBIAN_FRONTEND=noninteractive', '/usr/bin/apt-get',
        '-o', 'APT::Update::Error-Mode=any', 'update', timeout=600, capture=False)
    wsl('/usr/bin/env', 'DEBIAN_FRONTEND=noninteractive', '/usr/bin/apt-get', 'install',
        '-y', '--no-install-recommends', 'openvpn', 'iproute2', 'iptables',
        'curl', 'ca-certificates', 'python3', 'slirp4netns', 'util-linux', timeout=900, capture=False)
    wsl('/usr/bin/install', '-d', '-m', '0700', '/opt/isolated-openvpn-gateway',
        '/etc/isolated-openvpn-gateway', '/var/lib/isolated-openvpn-gateway/state', timeout=30)
    assets = {
        ROOT/'scripts'/'wsl_dependencies.py': ('/opt/isolated-openvpn-gateway/wsl_dependencies.py', '0555'),
        ROOT/'scripts'/'vpn.py': ('/opt/isolated-openvpn-gateway/vpn.py', '0555'),
        ROOT/'scripts'/'socks.py': ('/opt/isolated-openvpn-gateway/socks.py', '0555'),
        ROOT/'scripts'/'wsl_bridge.py': ('/opt/isolated-openvpn-gateway/wsl_bridge.py', '0555'),
        ROOT/'scripts'/'wsl_manager.py': ('/opt/isolated-openvpn-gateway/wsl_manager.py', '0555'),
        ROOT/'scripts'/'wsl_sockd.conf': ('/etc/isolated-openvpn-gateway/sockd.conf', '0444'),
    }
    for source, (destination, mode) in assets.items():
        copy_wsl_asset(source, destination, mode)
    wsl('/usr/bin/env', 'DEBIAN_FRONTEND=noninteractive', '/usr/bin/python3',
        '/opt/isolated-openvpn-gateway/wsl_dependencies.py', timeout=900, capture=False)
    wsl('/usr/bin/python3', '/opt/isolated-openvpn-gateway/wsl_manager.py', 'provision', timeout=60)
    update_installation(wsl_distro=WSL_DISTRO, wsl_created_by_project=True,
                        wsl_location=str(location), wsl_install_phase='ready')
    run(wsl_backend.terminate_command(executable=WSL), check=False, timeout=30)
    result = wsl('/usr/bin/python3', '/opt/isolated-openvpn-gateway/wsl_manager.py', 'status',
                 check=False, timeout=30)
    if result.returncode:
        raise GatewayError('Managed WSL distro was provisioned but did not restart cleanly.')
    print('Managed WSL2 backend installed without Docker Desktop. The saved backend was not changed.')

def uninstall_wsl_backend(confirm=True):
    marker = read_json(ROOT/'installation.json')
    if marker.get('wsl_distro') != WSL_DISTRO or not marker.get('wsl_created_by_project'):
        raise GatewayError('This installation does not own a managed WSL distro; nothing was removed.')
    if confirm and input('Unregister only the project-owned '+WSL_DISTRO+' distro? Type REMOVE WSL: ') != 'REMOVE WSL':
        return False
    stop_internal(backend='wsl')
    if not wsl_owned():
        raise GatewayError('Managed WSL ownership marker is absent; refusing to unregister the distro.')
    result = run(wsl_backend.unregister_command(executable=WSL), check=False, timeout=180, capture=False)
    if result.returncode:
        raise GatewayError('WSL could not unregister the project-owned distro.')
    location = Path(marker.get('wsl_location') or ROOT/'wsl')
    if location.name == 'wsl' and location.resolve().parent == ROOT.resolve() and location.exists() and not location.is_symlink():
        shutil.rmtree(location)
    if (RUNTIME/'active-backend').is_file() and (RUNTIME/'active-backend').read_text().strip() == 'wsl':
        (RUNTIME/'active-backend').unlink()
    update_installation(wsl_distro=None, wsl_created_by_project=False, wsl_location=None)
    print('Project-owned WSL distro removed. WSL itself and all other distros were preserved.')
    return True

def verify_outer_host(before, cfg):
    if not before.get('diagnostics_ok'):
        raise GatewayError('Host route/DNS diagnostics are unavailable. Inner OpenVPN was not started.')
    if not cfg['require_outer_vpn']:
        return
    if host.IS_WINDOWS:
        needles = cfg['windows_outer_adapter_contains']
        if not needles:
            raise GatewayError('Windows deployment must configure windows_outer_adapter_contains before startup.')
        if not windows_outer_route_matches(before, needles):
            raise GatewayError('The selected Windows public route does not use a configured outer VPN adapter. '
                               'Inner OpenVPN was not started.')
    else:
        prefix = re.escape(cfg['outer_interface_prefix'])
        if not re.search(r'interface:\s+' + prefix + r'\d+', before['public_route']['stdout']):
            raise GatewayError('No active outer VPN route on ' + cfg['outer_interface_prefix'] +
                               '*. Connect the required outer VPN before starting.')

def docker_preflight():
    engine(); images()
    cfg = configuration()
    tun = docker('run','--rm','--cap-drop','ALL','--device','/dev/net/tun:/dev/net/tun',
                 '--security-opt','no-new-privileges','--entrypoint','test',IMAGE,'-c','/dev/net/tun',
                 check=False,timeout=20)
    if tun.returncode:
        raise GatewayError('Docker Desktop cannot expose /dev/net/tun to a Linux container. Inner OpenVPN was not started.')
    before = network_snapshot()
    verify_outer_host(before, cfg)
    result = docker('run','--rm','--cap-drop','ALL','--read-only','--security-opt','no-new-privileges',
                    '--entrypoint','curl',IMAGE,'-4','--noproxy','*','-fsS','--max-time','15',IP_URL, timeout=25)
    container_ip = result.stdout.strip()
    if not before['public_ip'] or container_ip != before['public_ip']:
        raise GatewayError('Outer path check failed: host/container egress differs or is unavailable. Inner VPN was not started.')
    before['docker_public_ip'] = container_ip
    before['backend'] = 'docker'
    private_write(ROOT/'validation'/'before.json', json.dumps(before,indent=2))
    print('Outer VPN preflight passed: host = Docker = '+container_ip,flush=True)
    return before

def wsl_preflight():
    if not host.IS_WINDOWS:
        raise GatewayError('The WSL backend is Windows-only.')
    if WSL_DISTRO not in wsl_names() or not wsl_owned():
        raise GatewayError('Managed WSL backend is not installed. Run '+CLI+' install --backend wsl first.')
    listing = run([WSL, '--list', '--verbose'], check=False, timeout=20)
    if wsl_backend.distro_version(listing.stdout, WSL_DISTRO) != 2:
        raise GatewayError('The managed distro must use WSL2. Inner OpenVPN was not started.')
    tun = wsl('/usr/bin/test', '-c', '/dev/net/tun', check=False, timeout=20)
    if tun.returncode:
        raise GatewayError('/dev/net/tun is unavailable in the managed WSL2 distro. Inner OpenVPN was not started.')
    before = network_snapshot()
    verify_outer_host(before, configuration())
    result = wsl_manager('network-egress', check=False, timeout=35)
    distro_ip = wsl_backend.parse_public_ip(result.stdout)
    if not before['public_ip'] or distro_ip != before['public_ip']:
        wsl_manager('network-stop', check=False, timeout=20)
        reason = ('Windows public egress is unavailable' if not before['public_ip'] else
                  'WSL public egress is unavailable' if not distro_ip else 'Windows/WSL egress differs')
        raise GatewayError('Outer path check failed: ' + reason + '. '
                           'Inner VPN was not started; review NAT/mirrored compatibility with the outer VPN.')
    before.update(wsl_public_ip=distro_ip, backend='wsl',
                  wsl_networking_mode=wsl_backend.networking_mode(Path.home()/'.wslconfig'))
    private_write(ROOT/'validation'/'before.json', json.dumps(before, indent=2))
    print('Outer VPN preflight passed: Windows = WSL = '+distro_ip+'; networking mode = '+
          before['wsl_networking_mode'], flush=True)
    return before

def preflight(backend='docker'):
    return wsl_preflight() if backend_name(backend) == 'wsl' else docker_preflight()

def container_id(service):
    return compose('ps','--all','--quiet',service,check=False).stdout.strip()

def wsl_manager(action, *, input=None, check=True, timeout=40):
    return wsl('/usr/bin/python3', '/opt/isolated-openvpn-gateway/wsl_manager.py', action,
               input=input, check=check, timeout=timeout)

def wsl_status_data():
    if WSL_DISTRO not in wsl_names():
        return {}
    result = wsl_manager('status', check=False, timeout=20)
    try:
        return json.loads(result.stdout) if result.returncode == 0 else {}
    except (TypeError, ValueError):
        return {}

def windows_listener_records(port):
    command = ("$x=Get-NetTCPConnection -State Listen -LocalPort " + str(int(port)) +
               " -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,OwningProcess; "
               "ConvertTo-Json -Compress -Depth 4 -InputObject @($x)")
    result = run(powershell(command), check=False, timeout=20)
    try:
        value = json.loads(result.stdout) if result.returncode == 0 else []
        return value if isinstance(value, list) else [value]
    except (TypeError, ValueError):
        return []

def listener_result():
    cfg = configuration()
    if host.IS_WINDOWS:
        return wsl_backend.listener_validation(windows_listener_records(cfg['socks_port']), cfg['socks_port'])
    return {'listener_present': socks_available(), 'loopback_only': True,
            'ipv4_wildcard_absent': True, 'ipv6_wildcard_absent': True, 'addresses':['127.0.0.1']}

def listener_pid(record):
    try:
        return int(record.get('OwningProcess', -1)) if isinstance(record, dict) else -1
    except (TypeError, ValueError):
        return -1

def stop_forwarder():
    if not FORWARDER_PID.is_file():
        return
    try:
        pid = int(FORWARDER_PID.read_text())
    except (OSError, ValueError):
        FORWARDER_PID.unlink(missing_ok=True)
        return
    records = windows_listener_records(configuration()['socks_port']) if host.IS_WINDOWS else []
    owns_listener = any(str(item.get('LocalAddress')) == '127.0.0.1' and listener_pid(item) == pid
                        for item in records if isinstance(item, dict))
    if owns_listener:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(listener_pid(item) == pid for item in
                windows_listener_records(configuration()['socks_port'])):
            time.sleep(0.2)
    FORWARDER_PID.unlink(missing_ok=True)

def start_forwarder():
    if not host.IS_WINDOWS:
        raise GatewayError('The WSL loopback forwarder requires Windows.')
    stop_forwarder()
    cfg = configuration()
    if windows_listener_records(cfg['socks_port']):
        raise GatewayError('SOCKS port is already in use on Windows; no listener was changed.')
    creation = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | getattr(subprocess, 'DETACHED_PROCESS', 0)
    command = [sys.executable, ROOT/'scripts'/'loopback_forwarder.py', '--port', str(cfg['socks_port']),
               '--distro', WSL_DISTRO, '--pid-file', FORWARDER_PID]
    with open(os.devnull, 'r+b') as null:
        subprocess.Popen([str(value) for value in command], stdin=null, stdout=null, stderr=null,
                         close_fds=True, creationflags=creation)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        validation = listener_result()
        if validation['listener_present'] and validation['loopback_only']:
            return
        time.sleep(0.25)
    stop_forwarder()
    raise GatewayError('Windows loopback-only SOCKS forwarder did not start.')

def ready(backend='docker'):
    selected = backend_name(backend)
    state = wsl_status_data() if selected == 'wsl' else read_json(STATE/'status.json')
    return bool(state.get('ready')) and socks_available()

def cleanup_auth(backend='docker'):
    # SSD/APFS cannot promise forensic secure erasure; permissions and short
    # lifetime are the protection. No secret is intentionally backed up.
    if AUTH.is_symlink() or AUTH.exists():
        AUTH.unlink()
    if backend == 'wsl' and WSL_DISTRO in wsl_names():
        if not wsl_owned():
            raise GatewayError('Refusing credential cleanup in an unowned WSL distro.')
        wsl('/usr/bin/rm', '-f', '/run/isolated-openvpn-gateway/auth', check=False, timeout=20)

def stop_internal(keep_auth=False, backend='docker'):
    selected = backend_name(backend)
    if selected == 'wsl':
        stop_forwarder()
        action = 'stop-keep-auth' if keep_auth else 'stop'
        result = None
        if WSL_DISTRO in wsl_names():
            if not wsl_owned():
                raise GatewayError('Refusing to stop services in an unowned WSL distro.')
            available = wsl('/usr/bin/test', '-f', '/opt/isolated-openvpn-gateway/wsl_manager.py',
                            check=False, timeout=20).returncode == 0
            if available:
                result = wsl_manager(action, check=False, timeout=35)
        if not keep_auth:
            cleanup_auth('wsl')
        if result is not None and result.returncode:
            raise GatewayError('Managed WSL services could not be stopped; credentials cleanup was still attempted.')
        return
    result = compose('down','--timeout','8','--remove-orphans',check=False,timeout=35)
    if not keep_auth:
        cleanup_auth('docker')
    if result.returncode:
        raise GatewayError('Docker could not remove gateway containers. Credentials were removed unless this is a controlled transport comparison.')

def prompt_credentials(backend='docker'):
    cleanup_auth(backend)
    if not sys.stdin.isatty():
        raise GatewayError('Credentials require an interactive terminal; run ' + CLI +
                           ' start there. Do not send credentials in chat.')
    print('VPN credentials: both inputs are hidden; nothing is saved in shell history.',flush=True)
    username = getpass.getpass('VPN username: ')
    password = getpass.getpass('VPN password: ')
    if not username or not password or any(c in username+password for c in '\r\n\x00'):
        raise GatewayError('Empty or multiline credentials are not supported.')
    secret = username+'\n'+password+'\n'
    del username, password
    if backend == 'wsl':
        result = wsl_manager('put-auth', input=secret, check=False, timeout=20)
        secret = ''
        if result.returncode:
            raise GatewayError('Credentials could not be transferred to the protected WSL runtime file.')
    else:
        private_write(AUTH, secret)

def derive_config(source, expected=None):
    """Only known profile directives are accepted; embedded keys are opaque data."""
    allowed = {'dev','persist-tun','persist-key','data-ciphers','data-ciphers-fallback','ncp-ciphers',
               'cipher','auth','tls-client','client','resolv-retry','remote','nobind','auth-user-pass',
               'remote-cert-tls','explicit-exit-notify','setenv','key-direction','http-proxy',
               'proto','remote-random','connect-retry','connect-timeout','verb','mute-replay-warnings',
               'tls-version-min','tls-cipher','tls-ciphersuites','verify-x509-name','reneg-sec',
               'auth-nocache','auth-retry','remote-cert-ku','remote-cert-eku','x509-username-field',
               'fast-io','sndbuf','rcvbuf','mssfix','tun-mtu'}
    allowed_blocks = {'ca','cert','key','tls-auth','tls-crypt','tls-crypt-v2'}
    output, block, blocks, modern, cipher, remotes, proxies = [], None, {}, False, None, [], []
    auth_user_pass = []
    remote_cert_tls = False
    auth_valid = False
    for line in source.splitlines():
        s = line.strip()
        if block:
            output.append(line)
            if s == '</'+block+'>':
                block = None
            elif s:
                blocks[block] += 1
            continue
        if s.startswith('<'):
            name = s[1:-1] if s.endswith('>') and not s.startswith('</') else ''
            if name not in allowed_blocks or name in blocks:
                raise GatewayError('Unexpected or duplicate inline profile block.')
            block = name; blocks[name] = 0; output.append(line); continue
        if not s or s.startswith(('#',';')):
            output.append(line); continue
        try:
            p = shlex.split(s)
        except ValueError:
            raise GatewayError('Malformed profile directive.') from None
        key = p[0]
        if key not in allowed:
            raise GatewayError('Unexpected profile directive; manual review required: '+key)
        if key == 'remote':
            remotes.append(p)
        if key == 'http-proxy':
            proxies.append(p)
        if key == 'auth-user-pass':
            auth_user_pass.append(p)
        if key == 'remote-cert-tls' and p == ['remote-cert-tls', 'server']:
            remote_cert_tls = True
        if key == 'auth' and len(p) == 2 and p[1].lower() != 'none':
            auth_valid = True
        if key in ('persist-tun','auth-user-pass','setenv'):
            continue
        if key == 'dev':
            output.append('dev tun0'); continue
        if key == 'ncp-ciphers':
            # OpenVPN 2.6 alias normalized without broadening the cipher list.
            output.append('data-ciphers '+' '.join(p[1:])); modern=True; continue
        if key == 'cipher':
            cipher = p[1]
        if key == 'data-ciphers-fallback':
            cipher = None
        output.append(line)
    if block or len(remotes) != 1 or auth_user_pass != [['auth-user-pass']] or not remote_cert_tls or not auth_valid:
        raise GatewayError('Malformed profile or required authentication/TLS directives are absent.')
    if expected:
        expected_remote = ['remote', expected['remote_host'], str(expected['remote_port']),
                           expected['remote_protocol']]
        expected_proxy = ([['http-proxy', expected['http_proxy_host'], str(expected['http_proxy_port'])]]
                          if expected['http_proxy_host'] else [])
        if remotes != [expected_remote]:
            raise GatewayError('Profile remote does not match gateway.toml.')
        if proxies != expected_proxy:
            raise GatewayError('Profile HTTP proxy does not match gateway.toml.')
        if any(blocks.get(name, 0) == 0 for name in expected['required_inline_blocks']):
            raise GatewayError('Profile is missing a required inline security block.')
    if modern and cipher:
        output.append('data-ciphers-fallback '+cipher)
    # IPv4-only proxy; do not install unusable IPv6 tunnel routes.
    output += ['pull-filter ignore "route-ipv6"', 'pull-filter ignore "ifconfig-ipv6"',
               'pull-filter ignore "block-outside-dns"', 'pull-filter ignore "register-dns"']
    return '\n'.join(output)+'\n'

def prepare(transport, backend='docker'):
    cfg = configuration()
    if transport not in cfg['transports']:
        raise GatewayError('Unknown transport: ' + transport)
    if not STATE.exists():
        host.private_directory(STATE, parents=True)
    if not host.IS_WINDOWS:
        STATE.chmod(0o755)
    for name in ('status.json','status.new','safe.log'):
        p=STATE/name
        if p.exists() or p.is_symlink():
            p.unlink()
    private_write(STATE/'resolv.conf','nameserver 127.0.0.1\noptions timeout:2 attempts:2\n',0o644)
    profile = cfg['transports'][transport]
    private_write(RUNTIME/'client.ovpn', derive_config(
        (ROOT/'config'/profile['profile_file']).read_text(), profile))
    private_write(RUNTIME/'selected-transport',transport+'\n')
    if backend_name(backend) == 'wsl':
        result = wsl_manager('put-profile', input=(RUNTIME/'client.ovpn').read_text(),
                             check=False, timeout=20)
        if result.returncode:
            raise GatewayError('Derived OpenVPN profile could not be transferred to the managed WSL distro.')

def start_transport(transport, timeout=100, backend='docker'):
    selected = backend_name(backend)
    prepare(transport, selected)
    if selected == 'wsl':
        wsl_manager('start', timeout=40)
        print('Waiting for '+transport.upper()+' OpenVPN initialization and tun0 in managed WSL2...',flush=True)
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            data = wsl_status_data()
            if data.get('hook_error') or (data.get('initialization_completed') and not data.get('routes_ready')):
                print('OpenVPN initialized, but the managed WSL network hook failed.', flush=True)
                logs('wsl')
                return False
            if data.get('ready') and data.get('tun_exists'):
                start_forwarder()
                for _ in range(24):
                    if socks_available() and listener_result()['loopback_only']:
                        print('Connected: Initialization Sequence Completed; WSL tun0 verified; '
                              'Windows loopback-only SOCKS5 handshake passed.', flush=True)
                        return True
                    time.sleep(0.5)
                print('WSL VPN initialized, but loopback-only SOCKS did not become available.', flush=True)
                return False
            time.sleep(2)
        print('WSL transport did not become ready. Sanitized events:', flush=True)
        logs('wsl')
        return False
    compose('up','-d','--no-build','vpn',timeout=40)
    print('Waiting for '+transport.upper()+' OpenVPN initialization and tun0...',flush=True)
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        data = read_json(STATE/'status.json')
        if data.get('hook_error') or (data.get('initialization_completed') and not data.get('routes_ready')):
            print('OpenVPN initialized, but gateway network hook failed: '+str(data.get('hook_error','route-up did not complete')),flush=True)
            logs()
            return False
        if data.get('ready'):
            cid=container_id('vpn')
            if cid and docker('exec',cid,'python3','/opt/gateway/vpn.py','health',check=False).returncode == 0:
                compose('up','-d','--no-build','socks',timeout=35)
                for _ in range(20):
                    if socks_available():
                        print('Connected: Initialization Sequence Completed; tun0 verified; SOCKS5 handshake passed.',flush=True)
                        return True
                    time.sleep(0.5)
                print('VPN initialized, but Dante did not become available.',flush=True)
                return False
        cid=container_id('vpn')
        if cid and docker('inspect','--format','{{.State.Running}}',cid,check=False).stdout.strip() == 'false':
            break
        time.sleep(2)
    print('Transport did not become ready. Sanitized events:',flush=True)
    logs()
    return False

def logs(backend='docker'):
    if backend_name(backend) == 'wsl':
        result = wsl('/usr/bin/tail', '-n', '45', '/var/lib/isolated-openvpn-gateway/state/safe.log',
                     check=False, timeout=20)
        print(result.stdout.strip() if result.returncode == 0 and result.stdout.strip()
              else 'No sanitized WSL VPN events yet.')
        return
    path = STATE/'safe.log'
    print('\n'.join(path.read_text().splitlines()[-45:]) if path.exists() else 'No sanitized VPN events yet.')

def status(backend='docker'):
    selected_backend = backend_name(backend)
    if selected_backend == 'wsl':
        data = wsl_status_data()
        transport = (RUNTIME/'selected-transport').read_text().strip() if (RUNTIME/'selected-transport').exists() else 'none'
        print('Backend: wsl')
        print('Managed distro:', WSL_DISTRO, 'owned' if data.get('owned') else 'unavailable')
        print('WSL networking mode:', wsl_backend.networking_mode(Path.home()/'.wslconfig'))
        print('Service manager:', 'systemd' if data.get('systemd') else 'safe fallback')
        print('Selected transport:',transport)
        print('OpenVPN initialization completed:',bool(data.get('initialization_completed')))
        print('Gateway ready:',bool(data.get('ready')) and socks_available())
        print('tun0 exists:',bool(data.get('tun_exists')))
        print('Pushed DNS present:',bool(data.get('dns_pushed')))
        if not data.get('dns_pushed'):
            print('Corporate DNS: server did not push DNS; no address was invented.')
        print('Windows SOCKS5 handshake:',socks_available())
        print('Windows listener:', json.dumps(listener_result(), ensure_ascii=False))
        if data.get('hook_error'):
            print('Network hook error:',data['hook_error'])
        return
    engine()
    data = read_json(STATE/'status.json')
    print('Backend: docker')
    transport = (RUNTIME/'selected-transport').read_text().strip() if (RUNTIME/'selected-transport').exists() else 'none'
    print('Selected transport:',transport)
    print(compose('ps','--all',check=False).stdout.strip())
    cid=container_id('vpn')
    if cid:
        for args in (['ip','-4','addr','show','dev','tun0'],['ip','-4','route'],['ip','rule'],['ip','route','show','table','100']):
            r=docker('exec',cid,*args,check=False)
            if r.returncode == 0:
                print(r.stdout.strip())
    print('OpenVPN initialization completed:',bool(data.get('initialization_completed')))
    print('Gateway ready:',bool(data.get('ready')))
    if data.get('hook_error'):
        print('Network hook error:',data['hook_error'])
    print('Pushed DNS present:',bool(data.get('dns_pushed')))
    print('SOCKS5 handshake:',socks_available())
    if data.get('ready') and data.get('connected_at'):
        print('Connected for: %d seconds' % (time.time()-data['connected_at']))

def safe_target(value):
    u=urlsplit(value if '://' in value else 'https://'+value)
    if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise GatewayError('Supply a private http(s) URL without credentials, query or fragment.')
    return u

def validation(target=None, backend='docker'):
    selected = backend_name(backend)
    before=read_json(ROOT/'validation'/'before.json')
    after=network_snapshot()
    result=compare_host(before,after) if before else {}
    state=wsl_status_data() if selected == 'wsl' else read_json(STATE/'status.json')
    result['backend']=selected
    result['initialization_completed']=bool(state.get('ready'))
    result['socks_handshake']=socks_available()
    if selected == 'wsl':
        listeners = listener_result()
        result['loopback_publish_only'] = all(listeners.get(key) is True for key in (
            'listener_present','loopback_only','ipv4_wildcard_absent','ipv6_wildcard_absent'))
        route=wsl_network('/sbin/ip','route','get','1.1.1.1','uid','10000',check=False,timeout=15)
        result['proxy_route_tun0']=route.returncode == 0 and 'dev tun0' in route.stdout
        result['tun0_exists']=wsl_network('/sbin/ip','link','show','tun0',check=False,timeout=15).returncode == 0
        chain=wsl_network('/usr/sbin/iptables','-C','OUTPUT','-m','owner','--uid-owner','10000',
                  '-j','IOVG_WSL_PROXY',check=False,timeout=15)
        result['fail_closed_firewall_installed']=chain.returncode == 0
        outer=state.get('outer_interface')
        if outer:
            forced=wsl_network('/usr/sbin/runuser','-u','proxyuser','--','/usr/bin/curl','--interface',outer,
                       '--noproxy','*','-fsS','--connect-timeout','3','--max-time','4',
                       'http://1.1.1.1/',check=False,timeout=8)
            result['forced_eth0_blocked']=forced.returncode != 0
        else:
            result['forced_eth0_blocked']=False
    else:
        cid=container_id('vpn')
        if not cid:
            cid=None
    if selected == 'docker' and cid:
        details=json.loads(docker('inspect',cid).stdout)[0]
        cfg = configuration()
        bindings=details['HostConfig']['PortBindings'].get('1080/tcp',[])
        docker_loopback=bindings == [{'HostIp':cfg['socks_host'],'HostPort':str(cfg['socks_port'])}]
        if host.IS_WINDOWS:
            listeners=listener_result()
            docker_loopback = docker_loopback and all(listeners.get(key) is True for key in (
                'listener_present','loopback_only','ipv4_wildcard_absent','ipv6_wildcard_absent'))
        result['loopback_publish_only']=docker_loopback
        r=docker('exec',cid,'ip','route','get','1.1.1.1','uid','10000',check=False)
        result['proxy_route_tun0']='dev tun0' in r.stdout
        result['tun0_exists']=docker('exec',cid,'ip','link','show','tun0',check=False).returncode == 0
        # Try forcing UID 10000 to eth0, even while the tunnel is up. This must
        # fail; only UID 0 can send the exact outer OpenVPN endpoint traffic.
        r=docker('exec','--user','10000:10000',cid,'curl','--interface','eth0','--noproxy','*',
                 '-fsS','--connect-timeout','3','--max-time','4','http://1.1.1.1/',check=False,timeout=8)
        result['forced_eth0_blocked']=r.returncode != 0
    result['private_dns_pushed']=bool(state.get('dns_pushed'))
    if state.get('dns'):
        result['private_dns_via_socks']=any(socks_dns_query(address) for address in state['dns'])
    if target:
        u=safe_target(target)
        cfg = configuration()
        r=run([CURL,'--proxy','socks5h://%s:%d' % (cfg['socks_host'], cfg['socks_port']),
               '--noproxy','','--connect-timeout','8','--max-time','20',
               '--silent','--show-error','--output',os.devnull,'--write-out','%{http_code}',u.geturl()],check=False,timeout=25)
        result['private_http_status']=r.stdout
        result['private_target_connected']=r.returncode == 0
    else:
        result['private_target_connected']='NOT TESTED: no private hostname supplied'
    critical = ('host_diagnostics_available','host_dns_preserved','host_default_route_preserved',
                'host_static_routes_preserved','host_public_route_preserved',
                'host_outer_ip_preserved','global_git_proxy_preserved','initialization_completed',
                'socks_handshake','loopback_publish_only','proxy_route_tun0','tun0_exists','forced_eth0_blocked')
    if selected == 'wsl':
        critical += ('fail_closed_firewall_installed',)
    if host.IS_WINDOWS:
        critical += ('host_ipv6_dns_preserved','host_ipv6_routes_preserved','host_ipv6_default_preserved')
    result['passed'] = all(result.get(key) is True for key in critical)
    if state.get('dns'):
        result['passed'] = result['passed'] and result['private_dns_via_socks']
    if target:
        result['passed'] = result['passed'] and result.get('private_target_connected') is True
    result['private_application_tested'] = bool(target)
    private_write(ROOT/'validation'/'after.json',json.dumps(after,indent=2))
    private_write(ROOT/'validation'/'latest.json',json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
    return result

def failure_test(backend='docker'):
    selected = backend_name(backend)
    cid=container_id('vpn') if selected == 'docker' else None
    if (selected == 'docker' and not cid) or not ready(selected):
        raise GatewayError('Failure test needs a live verified connection first.')
    state=wsl_status_data() if selected == 'wsl' else read_json(STATE/'status.json')
    dns=state.get('dns',[])
    if dns:
        server=next((address for address in dns if socks_dns_query(address)),None)
        if not server:
            raise GatewayError('Failure test aborted: no successful private DNS request through SOCKS before stopping VPN.')
        canary=lambda: socks_dns_query(server)
    else:
        canary=lambda: public_ip(proxy=True) is not None
        if not canary():
            raise GatewayError('Failure test aborted: a successful SOCKS request is required as a positive control.')
    result = {}
    try:
        if selected == 'wsl':
            wsl_manager('stop-vpn', timeout=25)
        else:
            docker('exec',cid,'python3','-c',"import os,signal,pathlib; os.kill(int(pathlib.Path('/run/openvpn.pid').read_text()),signal.SIGTERM)")
        time.sleep(3)
        blocked=not canary()
        outer=public_ip()
        if selected == 'wsl':
            stopped_state = wsl_status_data()
            diagnostic_valid = stopped_state.get('network_namespace') is True
            stopped = diagnostic_valid and stopped_state.get('openvpn_running') is False
            tun_absent = diagnostic_valid and stopped_state.get('tun_exists') is False
            forced=wsl_manager('proxy-outer-test', check=False, timeout=20)
            try:
                evidence = json.loads(forced.stdout) if forced.returncode == 0 else {}
            except (ValueError, TypeError):
                evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
            firewall_blocked = all(evidence.get(key) is True for key in (
                'outer_control_succeeded','proxy_outer_blocked','firewall_reject_observed','probe_route_removed'))
        else:
            stopped=docker('inspect','--format','{{.State.Running}}',cid,check=False).stdout.strip() == 'false'
            socks=container_id('socks')
            tun_absent=bool(socks) and docker('exec',socks,'ip','link','show','tun0',check=False).returncode != 0
            firewall_blocked=True
        result.update(openvpn_terminated=stopped, tun0_absent=tun_absent,
                      proxy_request_verified_before_stop=True, socks_after_vpn_stop_blocked=blocked,
                      firewall_forced_outer_blocked=firewall_blocked, host_still_online=bool(outer))
    finally:
        if selected == 'wsl':
            result['state_restored'] = restore_wsl_after_failure_test(canary)
    print(json.dumps(result,indent=2),flush=True)
    return result

def restore_wsl_after_failure_test(canary):
    wsl_manager('start', timeout=40)
    deadline = time.monotonic()+100
    while time.monotonic() < deadline:
        state = wsl_status_data()
        if state.get('ready') and state.get('tun_exists'):
            if not listener_result()['listener_present']:
                start_forwarder()
            if socks_available() and canary():
                return True
        time.sleep(2)
    return False

def failure_evidence_passed(evidence, backend):
    required = ('openvpn_terminated', 'tun0_absent', 'proxy_request_verified_before_stop',
                'socks_after_vpn_stop_blocked', 'firewall_forced_outer_blocked', 'host_still_online')
    if backend == 'wsl':
        required += ('state_restored',)
    return isinstance(evidence, dict) and all(evidence.get(key) is True for key in required)


def comparison_error(exc, phase):
    # Exception text, subprocess argv/stdout/stderr and paths can contain private
    # deployment data. Persist only an allowlisted category and our own stage.
    kind = ('cancelled' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else
            'timeout' if isinstance(exc, subprocess.TimeoutExpired) else
            'gateway_check_failed' if isinstance(exc, GatewayError) else
            'os_error' if isinstance(exc, OSError) else 'unexpected_error')
    return {'phase': phase, 'kind': kind}


def compare_transports(backend='docker'):
    selected_backend = backend_name(backend)
    results = {}
    report = {'backend': selected_backend, 'phase': 'initial_stop', 'transport': None,
              'completed': False, 'session_ready': False, 'cleanup_attempted': False}
    completed = False

    def save():
        private_write(ROOT/'validation'/'transports.json', json.dumps(results, indent=2))
        private_write(ROOT/'validation'/'comparison.json', json.dumps(report, indent=2))

    def checkpoint(phase, transport=None):
        report.update(phase=phase, transport=transport)
        save()

    try:
        checkpoint('initial_stop')
        stop_internal(backend=selected_backend)
        checkpoint('initial_preflight')
        before = preflight(selected_backend)
        checkpoint('credentials')
        prompt_credentials(selected_backend)
        transports = list(configuration()['transports'])
        for transport in transports:
            row = {'connected': False, 'usable': False}
            results[transport] = row
            # The initial check precedes an unbounded interactive prompt.
            # Revalidate even the first transport: the outer VPN may have
            # changed while credentials were being entered. Every subsequent
            # teardown also requires a fresh verified outer path.
            checkpoint('transport_preflight', transport)
            preflight(selected_backend)
            checkpoint('transport_start', transport)
            row['connected'] = start_transport(transport, backend=selected_backend)
            current_state = wsl_status_data() if selected_backend == 'wsl' else read_json(STATE/'status.json')
            row.update(openvpn_initialized=bool(current_state.get('initialization_completed')),
                       gateway_state=current_state)
            if row['connected'] is True:
                checkpoint('transport_validation', transport)
                row['validation'] = validation(preferences().get('test_url'), selected_backend)
                if row['validation'].get('passed') is True:
                    checkpoint('transport_negative_test', transport)
                    row['failure_test'] = failure_test(selected_backend)
                    row['usable'] = failure_evidence_passed(row['failure_test'], selected_backend)
            row['events'] = (STATE/'safe.log').read_text() if (STATE/'safe.log').exists() else ''
            checkpoint('transport_stop', transport)
            stop_internal(keep_auth=True, backend=selected_backend)
        checkpoint('selection')
        selected = next((name for name in transports if results[name]['usable']), None)
        if not selected:
            raise GatewayError('No transport passed all gateway checks; private comparison evidence saved.')
        checkpoint('final_preflight', selected)
        preflight(selected_backend)
        checkpoint('final_start', selected)
        if not start_transport(selected, backend=selected_backend):
            raise GatewayError('Chosen transport failed its repeat connection test.')
        checkpoint('final_validation', selected)
        prefs = dict(preferences())
        report['final_validation'] = validation(prefs.get('test_url'), selected_backend)
        if report['final_validation'].get('passed') is not True or not ready(selected_backend):
            raise GatewayError('Chosen transport failed final validation; selection was not saved.')
        checkpoint('host_preservation', selected)
        report['host_preservation'] = compare_host(before, network_snapshot())
        if not report['host_preservation'] or not all(value is True for value in report['host_preservation'].values()):
            raise GatewayError('Host state changed during comparison; selection was not saved.')
        checkpoint('save_selection', selected)
        prefs['default_transport'] = selected
        save_preferences(prefs)
        report.update(completed=True, session_ready=True)
        checkpoint('complete', selected)
        completed = True
        print('Selected default: '+selected+'; other configured profiles remain available.', flush=True)
    except BaseException as exc:
        report.update(completed=False, session_ready=False,
                      failure=comparison_error(exc, report['phase']))
        raise
    finally:
        if not completed:
            report['cleanup_attempted'] = True
            try:
                stop_internal(backend=selected_backend)
            except BaseException as exc:
                report['cleanup_failure'] = comparison_error(exc, 'cleanup')
                raise
            finally:
                save()

def repository_ssh_config(host, user_config='~/.ssh/config', system_config='/etc/ssh/ssh_config',
                          proxy_host=None, proxy_port=None, proxy_command=None):
    # Reset Host scope before Include. Otherwise unrelated hosts would lose
    # the user's existing settings. Disable reuse of a possible direct master.
    cfg = configuration()
    proxy_host = proxy_host or cfg['socks_host']
    proxy_port = proxy_port or cfg['socks_port']
    if proxy_command is None:
        proxy_command = '/usr/bin/nc -X 5 -x '+proxy_host+':'+str(proxy_port)+' %h %p'
    user_config = str(user_config).replace('\\','/')
    system_config = str(system_config).replace('\\','/')
    return ('Host '+host+'\n'
            '    ProxyCommand '+proxy_command+'\n'
            '    CanonicalizeHostname no\n'
            '    ControlMaster no\n'
            '    ControlPath none\n'
            '    ControlPersist no\n\n'
            'Host *\n    Include '+json.dumps(str(user_config),ensure_ascii=False)+'\n'
            'Host *\n    Include '+json.dumps(str(system_config),ensure_ascii=False)+'\n')

def repository_ssh_settings(remote_host, config_path):
    if host.IS_WINDOWS:
        user_ssh, system_ssh = host.ssh_config_paths()
        python_path = str(Path(sys.executable).resolve()).replace('\\','/')
        helper_path = str((ROOT/'scripts'/'socks_connect.py').resolve()).replace('\\','/')
        try:
            host.ensure_windows_command_paths(python_path, helper_path, user_ssh, system_ssh, config_path)
        except OSError as exc:
            raise GatewayError(str(exc)) from None
        cfg = configuration()
        proxy_command = ('"'+python_path+'" "'+helper_path+'" '+cfg['socks_host']+' '+
                         str(cfg['socks_port'])+' %h %p')
        ssh_path = host.ssh_executable().replace('\\','/')
        private_config = str(config_path).replace('\\','/')
        value='"'+ssh_path+'" -F "'+private_config+'"'
        text=repository_ssh_config(remote_host, user_ssh, system_ssh, proxy_command=proxy_command)
        return value, text
    return ('/usr/bin/ssh -F '+shlex.quote(str(config_path)),
            repository_ssh_config(remote_host))

def configure_git(repo):
    repo=Path(repo).expanduser().resolve()
    remotes=run(['git','-C',repo,'remote']).stdout.strip()
    if not remotes:
        raise GatewayError('Repository has no remotes. No Git settings changed.')
    print('Repository remotes:', ', '.join(remotes.splitlines()))
    remote=input('Private remote NAME to configure (e.g. origin): ').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',remote):
        raise GatewayError('Invalid remote name')
    fetch_urls=run(['git','-C',repo,'remote','get-url','--all',remote]).stdout.splitlines()
    push_urls=run(['git','-C',repo,'remote','get-url','--push','--all',remote]).stdout.splitlines()
    if len(fetch_urls) != 1 or push_urls != fetch_urls:
        raise GatewayError('Separate/multiple fetch or push URLs need manual scoped review; nothing changed.')
    url=fetch_urls[0]
    u=urlsplit(url)
    config, config_text = None, None
    if u.scheme == 'https':
        safe_target(url)
        cfg = configuration()
        key='http.'+url+'.proxy'; value='socks5h://%s:%d' % (cfg['socks_host'],cfg['socks_port'])
    else:
        remote_host=u.hostname if u.scheme == 'ssh' else (re.fullmatch(r'(?:[^@/:]+@)?([^/:]+):.+',url).group(1)
                if re.fullmatch(r'(?:[^@/:]+@)?([^/:]+):.+',url) else None)
        if not remote_host or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*',remote_host) or u.password:
            raise GatewayError('Unsupported remote URL')
        if os.environ.get('GIT_SSH_COMMAND') or os.environ.get('GIT_SSH'):
            raise GatewayError('An environment SSH override exists; review it before configuring the proxy.')
        key='core.sshCommand'
        config=ROOT/'config'/('ssh-repo-'+hashlib.sha256(str(repo).encode()).hexdigest()[:16]+'.conf')
        if os.path.lexists(config):
            raise GatewayError('A private SSH configuration already exists; refusing to overwrite it.')
        value, config_text = repository_ssh_settings(remote_host, config)
    scope=['--local'] if key != 'core.sshCommand' else []
    previous=run(['git','-C',repo,'config',*scope,'--get-all',key],check=False)
    if previous.returncode == 0:
        raise GatewayError('Existing proxy/SSH setting found. Review it manually; nothing overwritten.')
    git_config=Path(run(['git','-C',repo,'rev-parse','--path-format=absolute','--git-path','config']).stdout.strip())
    backup=ROOT/'backups'/('git-config-'+str(time.time_ns())+'.backup')
    private_write(backup,git_config.read_text())
    if config:
        private_write(config,config_text)
    run(['git','-C',repo,'config','--local',key,value])
    records=read_json(ROOT/'git-changes.json',[])
    records.append({'repo':str(repo),'key':key,'value':value,'backup':str(backup)})
    private_write(ROOT/'git-changes.json',json.dumps(records,indent=2))
    test_command=['git','-C',str(repo),'ls-remote',remote]
    rendered=subprocess.list2cmdline(test_command) if host.IS_WINDOWS else shlex.join(test_command)
    print('Configured repository only. Read-only test: '+rendered)

def uninstall():
    if input('Remove only '+PROJECT+' and its launchers? Type REMOVE: ') != 'REMOVE':
        return
    marker = read_json(ROOT/'installation.json')
    if marker.get('project') != PROJECT or not host.installation_root_matches(ROOT, marker):
        raise GatewayError('Installation ownership check failed; refusing to change anything.')
    selected = active_backend()
    stop_internal(backend=selected)
    for record in read_json(ROOT/'git-changes.json',[]):
        r=run(['git','-C',record['repo'],'config','--local','--get',record['key']],check=False)
        if r.stdout.strip() == record['value']:
            run(['git','-C',record['repo'],'config','--local','--unset-all',record['key']])
        elif r.returncode == 0:
            raise GatewayError('Repository configuration changed since installation; preserve gateway and review rollback manually.')
    marker = read_json(ROOT/'installation.json')
    if marker.get('wsl_created_by_project'):
        if input('Full uninstall must remove the project-owned managed WSL distro. Type REMOVE WSL: ') != 'REMOVE WSL':
            raise GatewayError('Full uninstall cancelled; the managed distro and host installation were preserved.')
        uninstall_wsl_backend(confirm=False)
    if Path(DOCKER).is_file():
        for image in (IMAGE,SOCKS_IMAGE):
            docker('image','rm',image,check=False)
    profile=host.browser_root()
    delete_profile=profile.exists() and input('Also delete the separate private browser profile? Type DELETE PROFILE: ') == 'DELETE PROFILE'
    names = [CLI, BROWSER_CLI]
    if marker.get('legacy_aliases'):
        names += ['corp-vpn','corp-browser']
    if not host.IS_WINDOWS:
        for name in names:
            p=Path.home()/'.local/bin'/name
            if p.is_symlink() and p.resolve().is_relative_to(ROOT):
                p.unlink()
    marker=read_json(ROOT/'installation.json')
    if marker.get('project') != PROJECT or not host.installation_root_matches(ROOT, marker):
        raise GatewayError('Installation ownership check failed; refusing deletion.')
    if delete_profile:
        if not (profile/'.isolated-openvpn-gateway-owned').is_file():
            raise GatewayError('Browser profile ownership marker is absent; refusing deletion.')
        shutil.rmtree(profile)
    if host.IS_WINDOWS:
        # main() removes ROOT after the Windows lock handle is closed.
        return True
    shutil.rmtree(ROOT)
    print('Gateway removed. Docker Desktop, outer VPN, repositories and global network/Git settings were preserved.')
    return False

def usage():
    try:
        names = '|'.join(configuration()['transports'])
    except GatewayError:
        names = 'TRANSPORT'
    print(CLI+' install --backend wsl|docker\n'
          '            start [TRANSPORT] [--backend docker|wsl] | stop | restart [TRANSPORT]\n'
          '            status | logs | test [URL] | compare-transports [--backend docker|wsl]\n'
          '            backend set docker|wsl | git-configure [repo] | build | uninstall [--backend wsl]\n'
          'Configured transports: '+names)

def main():
    os.umask(0o077)
    action, positionals, explicit_backend = parse_cli(sys.argv[1:])
    if action in ('--help','-h','help'):
        usage()
        return 0
    marker = read_json(ROOT/'installation.json')
    if marker.get('project') != PROJECT or not host.installation_root_matches(ROOT, marker):
        command = str(host.command_directory(ROOT)/((CLI+'.cmd') if host.IS_WINDOWS else CLI))
        raise GatewayError('Run install.py first, then use '+command+
                           '; do not run lifecycle commands from the source archive.')
    RUNTIME.mkdir(mode=0o700,exist_ok=True)
    delete_after_unlock = False
    with host.maybe_lifecycle_lock(ROOT/'.lock', action not in ('status','logs','test')):
        if action in ('start','restart'):
            cfg = configuration()
            if len(positionals) > 1:
                raise GatewayError('Usage: '+CLI+' '+action+' [TRANSPORT] [--backend docker|wsl]')
            selected_backend=active_backend(explicit_backend) if action == 'restart' else backend_name(explicit_backend)
            selected=positionals[0] if positionals else preferences().get('default_transport',cfg['default_transport'])
            if selected not in cfg['transports']:
                raise GatewayError('Usage: '+CLI+' start ['+'|'.join(cfg['transports'])+']')
            if action == 'start' and ready(selected_backend):
                print('Already connected. Use '+CLI+' restart to switch sessions.'); status(selected_backend); return
            stop_internal(backend=selected_backend)
            completed = False
            try:
                preflight(selected_backend)
                prompt_credentials(selected_backend)
                # Input can take arbitrarily long; never start on the basis
                # of the pre-prompt outer path alone.
                preflight(selected_backend)
                if not start_transport(selected, backend=selected_backend):
                    raise GatewayError('VPN not ready. See '+CLI+' logs; try another configured transport.')
                private_write(RUNTIME/'active-backend', selected_backend+'\n')
                if not ready(selected_backend):
                    raise GatewayError('VPN readiness was lost before startup completed.')
                completed = True
            finally:
                if not completed:
                    stop_internal(backend=selected_backend)
        elif action == 'stop':
            if positionals:
                raise GatewayError('Usage: '+CLI+' stop')
            selected_backend=active_backend(explicit_backend)
            stop_internal(backend=selected_backend)
            for name in ('client.ovpn','selected-transport','active-backend'):
                (RUNTIME/name).unlink(missing_ok=True)
            print('Gateway stopped; ephemeral credentials removed.')
        elif action == 'status': status(active_backend(explicit_backend))
        elif action == 'logs': logs(active_backend(explicit_backend))
        elif action == 'test':
            if len(positionals) > 1:
                raise GatewayError('Usage: '+CLI+' test [URL]')
            return 0 if validation(positionals[0] if positionals else preferences().get('test_url'),
                                   active_backend(explicit_backend)).get('passed') else 1
        elif action == 'compare-transports':
            if positionals: raise GatewayError('Usage: '+CLI+' compare-transports [--backend docker|wsl]')
            selected_backend=backend_name(explicit_backend)
            compare_transports(selected_backend)
            private_write(RUNTIME/'active-backend', selected_backend+'\n')
        elif action == 'git-configure':
            if len(positionals) > 1: raise GatewayError('Usage: '+CLI+' git-configure [repo]')
            configure_git(positionals[0] if positionals else os.getcwd())
        elif action == 'backend':
            if len(positionals) != 2 or positionals[0] != 'set':
                raise GatewayError('Usage: '+CLI+' backend set docker|wsl')
            selected_backend=backend_name(positionals[1])
            prefs=preferences(); prefs['backend']=selected_backend; save_preferences(prefs)
            print('Saved backend:', selected_backend, '(running sessions were not changed).')
        elif action == 'install':
            if positionals or explicit_backend is None:
                raise GatewayError('Usage: '+CLI+' install --backend docker|wsl')
            install_backend(explicit_backend)
        elif action == 'build':
            if explicit_backend not in (None,'docker') or positionals:
                raise GatewayError('Image build is available only for the docker backend.')
            engine(); compose('build',capture=False,timeout=600)
        elif action == 'uninstall':
            if positionals:
                raise GatewayError('Usage: '+CLI+' uninstall [--backend wsl]')
            if explicit_backend:
                if explicit_backend != 'wsl':
                    raise GatewayError('Backend-only uninstall is supported for the project-owned WSL distro only.')
                uninstall_wsl_backend()
            else:
                delete_after_unlock = uninstall()
        else:
            usage()
            return 2
    if delete_after_unlock:
        with contextlib.suppress(ValueError):
            if Path.cwd().resolve().is_relative_to(ROOT):
                os.chdir(Path.home())
        shutil.rmtree(ROOT)
        print('Gateway removed. Docker Desktop, outer VPN, repositories and global network/Git settings were preserved.')
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        if len(sys.argv)>1 and sys.argv[1] in ('start','restart','compare-transports'):
            with contextlib.suppress(Exception):
                _action, _values, _explicit = parse_cli(sys.argv[1:])
                stop_internal(backend=backend_name(_explicit) if _explicit else active_backend())
            print('\nCancelled; credentials removed.',file=sys.stderr)
        else:
            print('\nCancelled.',file=sys.stderr)
        sys.exit(130)
    except (GatewayError, subprocess.TimeoutExpired, BlockingIOError, OSError) as exc:
        print('ERROR:',str(exc),file=sys.stderr)
        sys.exit(1)
