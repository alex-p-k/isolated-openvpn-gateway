#!/usr/bin/env python3
"""Install the isolated OpenVPN-to-SOCKS5 gateway on macOS or Windows."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time


PACKAGE = Path(__file__).resolve().parent
PACKAGE_FILES = (
    'VERSION', 'README.md', 'QUICKSTART.md', 'HANDOFF.md', 'ROLLBACK.md', 'MIGRATION.md', 'WINDOWS.md', 'install.py',
    'gateway.example.toml', 'compose.yaml', 'Dockerfile.vpn', 'Dockerfile.socks',
    '.dockerignore', '.gitignore', '.gitattributes', 'scripts/gateway_config.py', 'scripts/host.py',
    'scripts/gateway.py', 'scripts/vpn.py', 'scripts/socks.py', 'scripts/sockd.conf',
    'scripts/browser.py', 'scripts/socks_connect.py', 'scripts/wsl_backend.py',
    'scripts/wsl_manager.py', 'scripts/wsl_dependencies.py', 'scripts/wsl_bridge.py', 'scripts/loopback_forwarder.py',
    'scripts/wsl_sockd.conf', 'tests/test_gateway.py', 'tests/test_wsl.py',
    'tests/test_install.py', 'tests/test_windows.py', 'tests/test_listener_windows.py', 'tests/test_forwarder.py', 'tools/build_release.py',
    'tools/windows_acceptance.ps1',
)
INSTALL_FILES = tuple(x for x in PACKAGE_FILES if x not in (
    'install.py', 'gateway.example.toml', 'tests/test_install.py', 'tools/build_release.py',
    'tests/test_windows.py',
))


class InstallError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_source(package, name):
    if name not in PACKAGE_FILES:
        raise InstallError('File is not in the package allowlist.')
    package = Path(package).resolve()
    path = package / name
    current = path
    while current != package:
        if current.is_symlink():
            raise InstallError('Package contains a symlink: ' + name)
        current = current.parent
    if not path.is_file():
        raise InstallError('Package file missing: ' + name)
    return path


def verify_manifest(package):
    try:
        lines = (package / 'MANIFEST.sha256').read_text().splitlines()
        entries = [line.split('  ', 1) for line in lines if line]
        if any(len(row) != 2 for row in entries):
            raise ValueError
        expected = {name: value for value, name in entries}
    except (OSError, ValueError):
        raise InstallError('Missing or malformed MANIFEST.sha256; obtain a complete release.') from None
    if len(entries) != len(PACKAGE_FILES) or set(expected) != set(PACKAGE_FILES):
        raise InstallError('Manifest does not match the release allowlist.')
    for name in PACKAGE_FILES:
        if digest(safe_source(package, name).read_bytes()) != expected[name]:
            raise InstallError('Package checksum failed: ' + name)


def source_module(package, name, relative):
    script_dir = str(package / 'scripts')
    added = script_dir not in sys.path
    if added:
        sys.path.insert(0, script_dir)
    try:
        spec = importlib.util.spec_from_file_location(name, package / relative)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if added:
            sys.path.remove(script_dir)


def gateway_module(package):
    return source_module(package, 'handoff_gateway', 'scripts/gateway.py')


def config_module(package):
    return source_module(package, 'handoff_gateway_config', 'scripts/gateway_config.py')


def host_module(package):
    return source_module(package, 'handoff_host', 'scripts/host.py')


def load_profiles(paths, gateway, configuration):
    """Validate profile data without executing directives or printing private content."""
    result = {}
    for transport, expected in configuration['transports'].items():
        try:
            path = Path(paths[transport]).expanduser().resolve(strict=True)
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ValueError
            data = path.read_bytes()
            text = data.decode('utf-8')
            gateway.derive_config(text, expected)
            result[transport] = data
        except (OSError, UnicodeError, ValueError, KeyError, gateway.GatewayError):
            raise InstallError(transport + ' profile is missing or incompatible with gateway.toml. '
                               'Use the matching IT-issued profile; no contents were printed or changed.') from None
    return result


def requirements(gateway, hostlib, backend='docker'):
    import platform
    system = platform.system()
    architectures = {'Darwin': ('arm64', 'x86_64'), 'Windows': ('AMD64', 'ARM64', 'x86_64')}
    if system not in architectures or platform.machine() not in architectures[system]:
        raise InstallError('This installer targets macOS arm64/x86_64 and Windows AMD64/ARM64 only.')
    if sys.version_info < (3, 11):
        raise InstallError('Python 3.11 or newer is required. No Python installation was attempted.')
    if backend not in ('docker', 'wsl'):
        raise InstallError('--backend must be docker or wsl.')
    if backend == 'wsl' and system != 'Windows':
        raise InstallError('The native WSL2 backend is available only on Windows.')
    checks = [('Git', ['git', '--version'])]
    if backend == 'docker':
        if not Path(gateway.DOCKER).is_file():
            raise InstallError('Docker Desktop CLI not found; choose --backend wsl on Windows or install Docker Desktop.')
        checks[0:0] = [
            ('Docker Linux engine', [gateway.DOCKER, '--context', 'desktop-linux', 'info', '--format', '{{.OSType}}']),
            ('Compose', [gateway.DOCKER, '--context', 'desktop-linux', 'compose', 'version', '--short']),
        ]
    else:
        checks.insert(0, ('WSL', [hostlib.wsl_executable(), '--version']))
    if system == 'Windows':
        checks.append(('PowerShell', [hostlib.powershell_executable(), '-NoLogo', '-NoProfile',
                                      '-NonInteractive', '-Command', '$PSVersionTable.PSVersion.ToString()']))
    for name, args in checks:
        try:
            value = subprocess.run(args, env=gateway.ENV, capture_output=True, text=True, timeout=12)
        except (OSError, subprocess.TimeoutExpired):
            raise InstallError(name + ' is unavailable; install/start it and retry.') from None
        if value.returncode:
            raise InstallError(name + ' is unavailable; no system settings were changed.')
        if name == 'Docker Linux engine' and value.stdout.strip().lower() != 'linux':
            raise InstallError('Docker Desktop must use Linux containers; Windows containers are unsupported.')
        print(name + ': available')
    print(system + ' architecture: ' + platform.machine() + '; Python: ' + platform.python_version())
    print('Browser: available' if any(p.is_file() for p in hostlib.browser_candidates())
          else 'Browser: install Chrome/Chromium/Edge before using vpn-browser (Git does not require it).')


def write_private(path, data, mode=0o600, hostlib=None, system=None):
    hostlib = host_module(PACKAGE) if hostlib is None else hostlib
    if not path.parent.exists():
        hostlib.private_directory(path.parent, parents=True, system=system)
    hostlib.private_write(path, data, mode=mode, exclusive=True, system=system)


def install_files(package, target_home, profiles, python_executable, config_bytes, configuration,
                  legacy_aliases=False, system=None, environ=None, backend='docker'):
    """Install files only; do not run Docker, OpenVPN or any host network command."""
    import platform
    configlib = config_module(package)
    hostlib = host_module(package)
    system = platform.system() if system is None else system
    environ = os.environ if environ is None else environ
    root = hostlib.install_root(target_home, environ, system)
    if system == 'Windows':
        hostlib.ensure_windows_command_paths(root, target_home, python_executable)
    launch_dir = hostlib.command_directory(root, target_home, system)
    commands = [configlib.CLI, configlib.BROWSER_CLI]
    if legacy_aliases:
        commands += ['corp-vpn', 'corp-browser']
    links = [] if system == 'Windows' else [launch_dir / name for name in commands]
    for path in (root, *links):
        if os.path.lexists(path):
            raise InstallError('Refusing to overwrite an existing installation or command: ' + str(path))
    for parent in (root.parent, launch_dir):
        for candidate in (parent, *parent.parents):
            if candidate == target_home.parent:
                break
            if candidate.is_symlink():
                raise InstallError('Refusing a symlink in the installation path: ' + str(candidate))
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    created_links = []
    try:
        hostlib.private_directory(root, system=system)
        for name in INSTALL_FILES:
            write_private(root / name, safe_source(package, name).read_bytes(), hostlib=hostlib, system=system)
        for name in ('config', 'runtime', 'backups', 'validation', 'bin'):
            hostlib.private_directory(root / name, exist_ok=True, system=system)
        write_private(root / 'gateway.toml', config_bytes, hostlib=hostlib, system=system)
        for transport, data in profiles.items():
            write_private(root / 'config' / configuration['transports'][transport]['profile_file'], data,
                          hostlib=hostlib, system=system)
        write_private(root / 'settings.json', (json.dumps({
            'default_transport': configuration['default_transport'], 'backend': backend}, indent=2) + '\n').encode(),
                      hostlib=hostlib, system=system)
        metadata = {
            'project': configlib.PROJECT,
            'deployment_id': configuration['id'],
            'installed_at': time.time(),
            'kit_version': (package / 'VERSION').read_text().strip(),
            'source_hashes': {
                configuration['transports'][kind]['profile_file']: digest(value)
                for kind, value in profiles.items()
            },
            'legacy_aliases': bool(legacy_aliases),
            'installed_backend': backend,
            'wsl_distro': None,
            'wsl_created_by_project': False,
        }
        write_private(root / 'installation.json', (json.dumps(metadata, indent=2) + '\n').encode(),
                      hostlib=hostlib, system=system)
        if system == 'Windows':
            metadata['resolved_install_root'] = str((root/'installation.json').resolve().parent)
            hostlib.private_write(root/'installation.json', (json.dumps(metadata, indent=2)+'\n').encode(),
                                  system=system)
        launchers = ((configlib.CLI, 'gateway.py'), (configlib.BROWSER_CLI, 'browser.py'))
        if system == 'Windows':
            for name, script in launchers:
                write_private(root/'bin'/(name+'.cmd'), hostlib.batch_launcher(python_executable, script),
                              hostlib=hostlib, system=system)
            if legacy_aliases:
                for alias, script in (('corp-vpn','gateway.py'),('corp-browser','browser.py')):
                    write_private(root/'bin'/(alias+'.cmd'), hostlib.batch_launcher(python_executable, script),
                                  hostlib=hostlib, system=system)
        else:
            for name, script in launchers:
                launcher = '#!/bin/sh\nPYTHONDONTWRITEBYTECODE=1 exec ' + shlex.quote(str(python_executable)) + ' ' + \
                           shlex.quote(str(root / 'scripts' / script)) + ' "$@"\n'
                write_private(root / 'bin' / name, launcher.encode(), 0o700,
                              hostlib=hostlib, system=system)
            if legacy_aliases:
                for alias, target in (('corp-vpn', configlib.CLI), ('corp-browser', configlib.BROWSER_CLI)):
                    (root / 'bin' / alias).symlink_to(root / 'bin' / target)
            launch_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            for link in links:
                link.symlink_to(root / 'bin' / link.name)
                created_links.append(link)
    except BaseException:
        for link in created_links:
            if link.is_symlink() and link.resolve().is_relative_to(root):
                link.unlink()
        if root.exists() and not root.is_symlink():
            shutil.rmtree(root)
        raise
    return (root/'installation.json').resolve().parent if system == 'Windows' else root


def profile_arguments(values, configuration):
    result = {}
    for value in values:
        if '=' not in value:
            raise InstallError('--profile must use TRANSPORT=/path/to/profile.ovpn.')
        name, path = value.split('=', 1)
        if name not in configuration['transports'] or name in result or not path:
            raise InstallError('Unknown or duplicate --profile transport: ' + name)
        result[name] = Path(path)
    return result


def windows_shell_instructions(root, backend):
    """Copyable PowerShell commands using the actual resolved installation root."""
    if backend not in ('docker', 'wsl'):
        raise InstallError('Unknown backend for Windows launcher instructions.')
    def literal(value):
        value = str(value)
        if any(character in value for character in ('\r', '\n', '\x00')):
            raise InstallError('Unsafe path in Windows launcher instructions.')
        return "'" + value.replace("'", "''") + "'"
    root = Path(root)
    command = '& ' + literal(root/'bin'/'vpn-gateway.cmd')
    lines = ['Next: configure windows_outer_adapter_contains and enable the outer VPN.',
             'Run these commands in Windows PowerShell (no permanent PATH changes):']
    if backend == 'wsl':
        lines.append(command + ' install --backend wsl')
    lines.append(command + ' start --backend ' + backend)
    lines += ['For short command names in this PowerShell window only:',
              '$gatewayBin = ' + literal(root/'bin'),
              "$env:Path = $gatewayBin + ';' + $env:Path",
              'vpn-gateway status',
              'Repeat the two PATH setup lines in each new PowerShell window.']
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='read-only environment/profile checks; no installation')
    parser.add_argument('--config', type=Path, help='private deployment gateway.toml')
    parser.add_argument('--profiles-dir', type=Path, help='directory containing profile_file names from gateway.toml')
    parser.add_argument('--profile', action='append', default=[], metavar='NAME=PATH',
                        help='profile path for one configured transport; repeat for every transport')
    parser.add_argument('--legacy-aliases', action='store_true',
                        help='also create corp-vpn/corp-browser aliases if those names are unused')
    parser.add_argument('--backend', choices=('docker','wsl'), default='docker',
                        help='initial backend; wsl is Docker-free and Windows-only')
    args = parser.parse_args(argv)
    os.umask(0o077)
    sys.dont_write_bytecode = True
    verify_manifest(PACKAGE)
    gateway = gateway_module(PACKAGE)
    configlib = config_module(PACKAGE)
    hostlib = host_module(PACKAGE)
    elevated = hostlib.is_elevated()
    if elevated is None:
        raise InstallError('Cannot verify whether the installer is elevated; nothing was changed.')
    if elevated:
        raise InstallError('Run as your normal desktop user, not sudo/root/Administrator.')
    configuration = None
    config_bytes = None
    if args.config:
        try:
            config_path = args.config.expanduser().resolve(strict=True)
            config_bytes = config_path.read_bytes()
            configuration = configlib.load_config(config_path)
        except configlib.ConfigError as exc:
            raise InstallError(str(exc)) from None
    if args.profiles_dir and args.profile:
        raise InstallError('Use either --profiles-dir or repeated --profile options.')
    if (args.profiles_dir or args.profile) and not configuration:
        raise InstallError('--config is required when profiles are supplied.')
    paths = {}
    if configuration and args.profiles_dir:
        directory = args.profiles_dir.expanduser()
        paths = {name: directory / item['profile_file'] for name, item in configuration['transports'].items()}
    elif configuration and args.profile:
        paths = profile_arguments(args.profile, configuration)
    if paths and set(paths) != set(configuration['transports']):
        missing = sorted(set(configuration['transports']) - set(paths))
        raise InstallError('Profiles are required for every transport; missing: ' + ', '.join(missing))
    profiles = load_profiles(paths, gateway, configuration) if paths else None
    requirements(gateway, hostlib, args.backend)
    if profiles:
        print('Profiles: structurally compatible with gateway.toml; originals unchanged.')
    if args.check:
        print('Read-only checks complete. Outer-VPN inheritance and private access require the first start/test.')
        return 0
    if not configuration or not profiles:
        raise InstallError('Supply --config and either --profiles-dir or one --profile per transport.')
    root = install_files(PACKAGE, Path.home(), profiles, Path(sys.executable).resolve(),
                         config_bytes, configuration, args.legacy_aliases,
                         system=hostlib.SYSTEM, environ=os.environ, backend=args.backend)
    print('Installed: ' + str(root))
    print('No VPN was started; host DNS/routes, global Git and browser profiles were not changed.')
    if hostlib.IS_WINDOWS:
        print('\n'.join(windows_shell_instructions(root, args.backend)))
    else:
        print('Next: enable your outer VPN, then run ~/.local/bin/vpn-gateway start')
        print('If commands are not in PATH, run: export PATH="$HOME/.local/bin:$PATH"')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (InstallError, OSError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(1)
