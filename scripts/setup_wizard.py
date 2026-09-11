"""Resumable source installation. Credentials are never wizard state."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import host
import browser
from gateway_config import PROJECT, load_config
from product import ProductError, ask, confirm, read_object
import profile_import


def installer_module(package):
    spec = importlib.util.spec_from_file_location('product_installer', package/'install.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def owned(root):
    marker = read_object(root/'installation.json')
    if marker.get('project') != PROJECT or not host.installation_root_matches(root, marker):
        raise ProductError('INSTALL_OWNERSHIP', 'The installation directory is not owned by this project; it was not changed.')
    return marker


def checkpoint(root, phase):
    state = read_object(root/'setup-state.json')
    completed = state.get('completed', [])
    if phase not in completed:
        completed.append(phase)
    # Only stage names and time: no paths, endpoints, usernames or passwords.
    host.private_write(root/'setup-state.json', json.dumps(dict(schema_version=1, completed=completed, updated_at=time.time())))


def run_installed(root, *arguments):
    result = subprocess.run([sys.executable, str(root/'scripts'/'gateway.py'), *arguments])
    if result.returncode:
        raise ProductError('SETUP_STAGE', 'The stage did not complete. Finished owned stages are preserved.', 'Run .\\setup.cmd again to resume.')


def encode_config(configuration):
    gateway_fields = ('id', 'display_name', 'default_transport', 'dns_canary', 'socks_port',
                      'require_outer_vpn', 'outer_interface_prefix', 'windows_outer_adapter_contains')
    lines = ['[gateway]']
    for name in gateway_fields:
        if name in configuration:
            lines.append(name + ' = ' + json.dumps(configuration[name], ensure_ascii=True))
    for name, transport in configuration['transports'].items():
        lines += ['', '[transports.' + name + ']']
        for key, value in transport.items():
            if value is not None:
                lines.append(key + ' = ' + json.dumps(value, ensure_ascii=True))
    return ('\n'.join(lines) + '\n').encode('utf-8')


def choose_adapters(gateway, explicit):
    if explicit:
        values = [explicit]
    else:
        command = gateway.windows_network_commands()['adapters']
        result = gateway.run(command, check=False, timeout=20)
        try:
            rows = json.loads(result.stdout)
            values = [row['Name'] for row in rows if row.get('Status') == 'Up' and isinstance(row.get('Name'), str)]
        except (ValueError, KeyError, TypeError):
            values = []
        if result.returncode or not values:
            raise ProductError('OUTER_ADAPTER', 'No active adapter could be inspected. Connect the external VPN and retry.')
        print('Active adapters (select the EXTERNAL VPN, not Wi-Fi/Ethernet):')
        for number, value in enumerate(values, 1):
            print(f'  {number}. {value}')
        choice = ask('External VPN adapter number')
        if not choice.isdigit() or not 1 <= int(choice) <= len(values):
            raise ProductError('OUTER_ADAPTER', 'Choose an adapter from the numbered list.')
        values = [values[int(choice)-1]]
    if not confirm('Use the selected external VPN adapter for mandatory preflight checks'):
        raise ProductError('SETUP_CANCELLED', 'Adapter selection cancelled; no installation was created.', 'Run .\\setup.cmd again.')
    return values


def inputs(args, installer, gateway, package):
    if args.ovpn and args.config:
        raise ProductError('SETUP_INPUT', 'Use either --ovpn or --config, not both.')
    if not args.ovpn and not args.config:
        mode = ask('Input: 1 = single .ovpn, 2 = IT gateway.toml kit', '1')
        if mode not in ('1', '2'):
            raise ProductError('SETUP_INPUT', 'Choose input type 1 or 2.')
        value = Path(ask('Path to .ovpn' if mode == '1' else 'Path to gateway.toml').strip('"')).expanduser()
        if mode == '1': args.ovpn = value
        else: args.config = value
    if args.ovpn:
        path = args.ovpn.expanduser().resolve(strict=True)
        if path.stat().st_size > 1024 * 1024:
            raise ProductError('PROFILE_SIZE', 'Profile exceeds the 1 MiB limit.')
        original = path.read_bytes()
        try:
            source, metadata = profile_import.normalize(original.decode('utf-8-sig'))
            gateway.derive_config(source, metadata)
        except (ValueError, gateway.GatewayError) as exc:
            # derive_config emits only known directives, never profile values.
            message = str(exc) if isinstance(exc, gateway.GatewayError) else 'Profile must be valid UTF-8.'
            raise ProductError('PROFILE_UNSUPPORTED', message, 'Ask IT for a compatible profile; originals were not changed.') from None
        adapters = choose_adapters(gateway, args.outer_adapter) if host.IS_WINDOWS else []
        data = profile_import.deployment(metadata, adapters)
        with tempfile.TemporaryDirectory(prefix='gateway-import-') as directory:
            temporary = Path(directory)
            host.private_directory(temporary, exist_ok=True)
            host.private_write(temporary/'gateway.toml', data)
            config = load_config(temporary/'gateway.toml')
        # Preserve the exact supplied bytes. Runtime normalization is separate.
        return data, config, {'primary': original}
    config_path = args.config.expanduser().resolve(strict=True)
    config = load_config(config_path)
    if host.IS_WINDOWS and (not config['windows_outer_adapter_contains'] or args.outer_adapter or
                           config['windows_outer_adapter_contains'] == ['REPLACE-WITH-OUTER-VPN-ADAPTER']):
        config['windows_outer_adapter_contains'] = choose_adapters(gateway, args.outer_adapter)
    directory = args.profiles_dir or Path(ask('Directory containing the IT profiles', str(config_path.parent)).strip('"')).expanduser()
    paths = {name: directory/item['profile_file'] for name, item in config['transports'].items()}
    return encode_config(config), config, installer.load_profiles(paths, gateway, config)


def checked_destination(root, name):
    path = root/name
    if not path.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in (path, *path.parents)):
        raise ProductError('UPDATE_OWNERSHIP', 'An application destination contains a symlink or escapes its installation.')
    return path


def update_allowlist(installer):
    return set(installer.INSTALL_FILES) | {
        'bin/'+name+'.cmd' for name in ('vpn-gateway', 'vpn-browser', 'corp-vpn', 'corp-browser')} | {'bin/vpn-gateway', 'bin/vpn-browser'}


def verify_installed_files(root, marker, installer):
    allowed = update_allowlist(installer)
    for name, digest in marker.get('app_hashes', {}).items():
        if name not in allowed:
            raise ProductError('UPDATE_OWNERSHIP', 'The application hash inventory contains an unexpected path.')
        path = checked_destination(root, name)
        if path.exists() and installer.digest(path.read_bytes()) != digest:
            raise ProductError('UPDATE_MODIFIED', 'An installed application file was edited; preserve it and review before updating.')


def update_files(package, root, installer):
    """Journal only public application files; never back up auth, profiles or VHDX."""
    if package.resolve() == root.resolve():
        raise ProductError('UPDATE_SOURCE', 'Update must run from a new verified source checkout.', 'Run .\\setup.cmd --update in the checkout.')
    marker = owned(root)
    if marker.get('kit_version') not in ('2026.09.01.2', '2026.09.10.1', '2026.09.11.1'):
        raise ProductError('UPDATE_VERSION', 'This installed version has no reviewed in-place migration.', 'Use the documented private-input-preserving migration workflow.')
    installer.verify_manifest(package)
    # Validate installed deployment against this version before stopping anything.
    config = load_config(root/'gateway.toml')
    gateway = installer.gateway_module(package)
    installer.load_profiles({name: root/'config'/item['profile_file'] for name, item in config['transports'].items()}, gateway, config)
    for name in ('vpn.py', 'socks.py', 'wsl_manager.py', 'wsl_bridge.py', 'wsl_dependencies.py', 'wsl_sockd.conf'):
        old, new = root/'scripts'/name, package/'scripts'/name
        if not old.is_file() or old.read_bytes().replace(b'\r\n', b'\n') != new.read_bytes().replace(b'\r\n', b'\n'):
            raise ProductError('UPDATE_BACKEND_ASSETS', 'Linux runtime assets changed; this host-CLI update cannot silently replace them.',
                               'Use a separately reviewed backend migration; the running gateway was not stopped.')
    verify_installed_files(root, marker, installer)
    if host.IS_WINDOWS:
        host.ensure_windows_command_paths(root, Path(sys.executable).resolve())
    if not confirm('Stop the current gateway and update owned application files (credentials will be removed)'):
        return False
    run_installed(root, 'stop')
    with host.lifecycle_lock(root/'.lock'):
        if (root/'runtime'/'active-backend').exists():
            raise ProductError('UPDATE_BUSY', 'A new session started before the update lock; no application files were changed.')
        changes = {name: installer.safe_source(package, name).read_bytes() for name in installer.INSTALL_FILES}
        for name, script in (('vpn-gateway', 'gateway.py'), ('vpn-browser', 'browser.py')):
            if host.IS_WINDOWS:
                changes['bin/'+name+'.cmd'] = host.batch_launcher(Path(sys.executable).resolve(), script)
        if host.IS_WINDOWS and marker.get('legacy_aliases'):
            for name, script in (('corp-vpn', 'gateway.py'), ('corp-browser', 'browser.py')):
                changes['bin/'+name+'.cmd'] = host.batch_launcher(Path(sys.executable).resolve(), script)
        # Recheck under the lifecycle lock, after the pre-stop compatibility check.
        verify_installed_files(root, marker, installer)
        before = {}
        for name in changes:
            path = checked_destination(root, name)
            before[name] = path.read_bytes() if path.exists() else None
        backup = root/'backups'/('app-update-' + str(time.time_ns()))
        host.private_directory(backup, parents=True)
        for name, data in before.items():
            if data is not None:
                path = backup/name
                host.private_directory(path.parent, parents=True, exist_ok=True)
                host.private_write(path, data)
        host.private_write(backup/'inventory.json', json.dumps({name: value is not None for name, value in before.items()}))
        host.private_write(backup/'installation.json', json.dumps(marker))
        host.private_write(root/'update-pending.json', json.dumps({'backup': str(backup), 'files': list(changes)}))
        try:
            for name, data in changes.items():
                destination = checked_destination(root, name)
                host.private_directory(destination.parent, parents=True, exist_ok=True)
                host.private_write(destination, data, mode=0o700 if name.startswith('bin/') else 0o600)
            result = subprocess.run([sys.executable, str(root/'scripts'/'gateway.py'), '--version'],
                                    capture_output=True, timeout=20)
            if result.returncode:
                raise ProductError('UPDATE_VERIFY', 'Updated CLI smoke check failed; previous application files restored.')
            marker.update(kit_version=(package/'VERSION').read_text().strip(),
                          app_hashes={name: installer.digest(value) for name, value in changes.items()})
            host.private_write(root/'installation.json', json.dumps(marker, indent=2))
            (root/'update-pending.json').unlink()
        except BaseException:
            recover_update(root, installer, locked=True)
            raise
        print('Application updated. WSL assets/configuration were preserved; the gateway is stopped. Next: vpn-gateway start')
        return True


def recover_update(root, installer, *, locked=False):
    record = read_object(root/'update-pending.json')
    if not record:
        return
    if not locked:
        with host.lifecycle_lock(root/'.lock'):
            return recover_update(root, installer, locked=True)
    if (root/'runtime'/'active-backend').exists():
        raise ProductError('UPDATE_BUSY', 'An active session conflicts with the update journal; no files were restored.')
    backup = Path(record.get('backup', ''))
    if backup.parent != root/'backups' or not backup.name.startswith('app-update-') or backup.is_symlink():
        raise ProductError('UPDATE_RECOVERY', 'Invalid update journal; no automatic recovery was attempted.')
    inventory = read_object(checked_destination(root, str(backup.relative_to(root)/'inventory.json')))
    if set(inventory) != set(record.get('files', [])):
        raise ProductError('UPDATE_RECOVERY', 'Update journal and backup inventory disagree.')
    allowed = update_allowlist(installer)
    if not set(inventory) <= allowed:
        raise ProductError('UPDATE_RECOVERY', 'Unexpected backup destination; manual review required.')
    for name, existed in inventory.items():
        path = checked_destination(root, name)
        if existed:
            host.private_write(path, checked_destination(backup, name).read_bytes(), mode=0o700 if name.startswith('bin/') else 0o600)
        else:
            path.unlink(missing_ok=True)
    host.private_write(checked_destination(root, 'installation.json'), checked_destination(backup, 'installation.json').read_bytes())
    (root/'update-pending.json').unlink()
    print('Interrupted update rolled back. Gateway remains stopped; no credentials were restored.')


def main(argv=None, package=None):
    package = Path(package or Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('wsl', 'docker'))
    parser.add_argument('--ovpn', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--profiles-dir', type=Path)
    parser.add_argument('--outer-adapter')
    parser.add_argument('--browser', choices=('chromium', 'firefox', 'skip'))
    parser.add_argument('--url')
    parser.add_argument('--rollback-docker', action='store_true', help='restore the recorded macOS Docker migration backup')
    parser.add_argument('--update', action='store_true')
    parser.add_argument('--migrate-docker', action='store_true', help='reviewed macOS Docker runtime migration; requires --update')
    parser.add_argument('--installed-root', type=Path, help='explicit owned legacy/MSIX installation to resume or update')
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        raise ProductError('INPUT_REQUIRED', 'Setup changes require interactive confirmation; no changes were made.',
                           'Run .\\setup.cmd in a terminal; install.py --check is available for automation.')
    if host.is_elevated() is not False:
        raise ProductError('NORMAL_USER_REQUIRED', 'Run setup as your normal desktop user, not Administrator/root.')
    os.umask(0o077)
    installer = installer_module(package)
    root = (args.installed_root.expanduser().resolve(strict=True) if args.installed_root else
            package if (package/'installation.json').is_file() else host.install_root())
    exists = root.exists()
    if exists:
        owned(root)
        with host.lifecycle_lock(root/'.setup.lock'):
            recover_update(root, installer)
    if args.rollback_docker:
        if args.update or args.migrate_docker:
            parser.error('--rollback-docker cannot be combined with update')
        with host.lifecycle_lock(root/'.setup.lock'):
            import docker_migration
            docker_migration.rollback(package, root, installer)
            return 0
    if args.migrate_docker and not args.update:
        parser.error('--migrate-docker requires --update')
    if args.update:
        if not exists:
            raise ProductError('NOT_INSTALLED', 'There is no installation to update.', 'Run .\\setup.cmd without --update.')
        with host.lifecycle_lock(root/'.setup.lock'):
            if args.migrate_docker:
                import docker_migration
                docker_migration.migrate(package, root, installer)
                return 0
            return 0 if update_files(package, root, installer) else 0
    if exists and (args.ovpn or args.config or args.outer_adapter):
        raise ProductError('SETUP_EXISTING', 'Existing private deployment inputs are preserved; importing over them is not supported.',
                           'Use the documented migration workflow after backing up private inputs.')
    settings = read_object(root/'settings.json') if exists else {}
    backend = settings.get('backend') or args.backend or ask('Backend: wsl (Docker-free) or docker', 'wsl' if host.IS_WINDOWS else 'docker')
    if args.backend and exists and args.backend != backend:
        raise ProductError('BACKEND_EXPLICIT', 'Setup does not switch an existing backend.', 'vpn-gateway backend set ' + args.backend)
    if backend not in ('wsl', 'docker'):
        raise ProductError('BACKEND_INVALID', 'Choose wsl or docker.')
    gateway = installer.gateway_module(package)
    print('[1/5] Checking prerequisites for ' + backend + ' (no host network changes).', flush=True)
    try:
        installer.requirements(gateway, host, backend)
    except installer.InstallError:
        raise ProductError('PREREQUISITE', 'Selected backend prerequisites are unavailable.',
                           ('Check wsl --version. If absent, enable WSL in a separate Administrator terminal using wsl --install --no-distribution; '
                            'reboot if requested, then rerun setup. Official guide: https://learn.microsoft.com/windows/wsl/install'
                            if backend == 'wsl' else 'Start Docker Desktop with Linux containers, then rerun setup.')) from None
    if not exists:
        installer.verify_manifest(package)
        print('[2/5] Importing private inputs; original files will remain unchanged.', flush=True)
        data, config, profiles = inputs(args, installer, gateway, package)
        if not confirm('Install this deployment into a separate per-user gateway directory'):
            return 0
        root = installer.install_files(package, Path.home(), profiles, Path(sys.executable).resolve(),
                                       data, config, backend=backend)
        settings = read_object(root/'settings.json')
    else:
        print('[2/5] Existing owned installation found; private inputs and backend preserved.')
    with host.lifecycle_lock(root/'.setup.lock'):
        checkpoint(root, 'files')
        print('[3/5] Preparing selected backend. Downloads/builds can take several minutes; rerun setup after interruption.', flush=True)
        run_installed(root, 'install', '--backend', backend)
        checkpoint(root, 'backend')
        print('[4/5] Command and browser preferences.', flush=True)
        if host.IS_WINDOWS:
            import path_integration
            path_integration.register_location(root)
            if (root/'path-registration.json').is_file() or confirm('Add gateway commands to your user PATH'):
                path_integration.register(root)
        kind = args.browser
        if kind is None:
            kind = ask('Browser: chromium, firefox (experimental), or skip', settings.get('browser_kind', 'chromium'))
        if kind not in ('chromium', 'firefox', 'skip'):
            raise ProductError('BROWSER_CONFIG', 'Choose chromium, firefox or skip.')
        if kind != 'skip':
            settings['browser_kind'] = kind
            candidates = host.firefox_candidates() if kind == 'firefox' else host.browser_candidates()
            if any(path.is_file() for path in candidates):
                print('Selected browser executable: detected; its main profile will not be changed.')
            else:
                print('Selected browser is not installed. Install it separately before vpn-gateway browser; gateway setup can continue.')
            url = args.url or ask('Corporate start URL (blank to leave unchanged)', settings.get('browser_url', ''))
            if url:
                settings['browser_url'] = browser.saved_url(url)
            if kind == 'firefox':
                print('Firefox is explicit opt-in and not yet network-accepted. The existing Chrome profile stays intact.')
        host.private_write(root/'settings.json', json.dumps(settings, indent=2))
        checkpoint(root, 'preferences')
        print('[5/5] Optional first-use checks.', flush=True)
        if confirm('Configure a corporate Git repository (repository-local only)'):
            repo = ask('Repository path on the Windows filesystem' if host.IS_WINDOWS else 'Repository path')
            run_installed(root, 'git', 'configure', repo)
        if confirm('Connect now (external VPN preflight runs before hidden credential prompts)'):
            run_installed(root, 'start')
            if kind != 'skip' and confirm('Open the separate corporate browser'):
                run_installed(root, 'browser')
        checkpoint(root, 'complete')
    print('Setup complete. Daily: vpn-gateway start | status | browser | stop')
    print('Troubleshooting: vpn-gateway doctor. Existing terminals may need to be reopened for PATH.')
    return 0
