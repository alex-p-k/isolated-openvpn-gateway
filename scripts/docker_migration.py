"""Reviewed macOS Docker migration with staged images and application rollback."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

import host
from product import ProductError, confirm, read_object
import setup_wizard

SUPPORTED = {'2026.08.30.1', '2026.09.01.2', '2026.09.10.1', '2026.09.11.1'}
LABEL = 'io.isolated-openvpn-gateway.source-sha256'


def gateway_at(package, root, installer):
    gateway = installer.gateway_module(package)
    gateway.ROOT = root
    gateway.RUNTIME = root/'runtime'
    gateway.STATE = root/'runtime'/'state'
    gateway.AUTH = root/'runtime'/'auth'
    gateway.PREFS = root/'settings.json'
    return gateway


def application_changes(package, root, installer):
    changes = {name: installer.safe_source(package, name).read_bytes() for name in installer.INSTALL_FILES}
    for name, script in (('vpn-gateway', 'gateway.py'), ('vpn-browser', 'browser.py')):
        changes['bin/'+name] = ('#!/bin/sh\nPYTHONDONTWRITEBYTECODE=1 exec ' +
                              shlex.quote(str(Path(sys.executable).resolve())) + ' ' +
                              shlex.quote(str(root/'scripts'/script)) + ' "$@"\n').encode()
    return changes


def stage_images(package, gateway):
    """Content-derived project tags never replace the old :local images."""
    refs = {}
    for service, files in {
        'vpn': ('Dockerfile.vpn', 'scripts/vpn.py'),
        'socks': ('Dockerfile.socks', 'scripts/socks.py', 'scripts/sockd.conf'),
    }.items():
        digest = hashlib.sha256(b''.join(name.encode()+b'\0'+(package/name).read_bytes() for name in files)).hexdigest()
        tag = 'isolated-openvpn-gateway-' + service + ':rev-' + digest[:16]
        existing = gateway.docker('image', 'inspect', tag, check=False)
        if existing.returncode == 0:
            metadata = json.loads(existing.stdout)[0]
            if (metadata.get('Config', {}).get('Labels') or {}).get(LABEL) != digest:
                raise ProductError('IMAGE_CONFLICT', 'A staged image tag has an unexpected source identity; it was not overwritten.')
        else:
            print('Building staged ' + service + ' image; current gateway remains running.', flush=True)
            gateway.docker('build', '--label', LABEL+'='+digest, '-t', tag,
                           '-f', str(package/('Dockerfile.'+service)), str(package), capture=False, timeout=900)
        # Compile without writing inside the immutable image and verify package binaries.
        script = '/opt/gateway/' + ('vpn.py' if service == 'vpn' else 'socks.py')
        gateway.docker('run', '--rm', '--cap-drop', 'ALL', '--read-only', '--network', 'none',
                       '--security-opt', 'no-new-privileges', '--entrypoint', 'python3', tag, '-c',
                       'import pathlib,sys; compile(pathlib.Path(sys.argv[1]).read_text(),sys.argv[1],"exec")', script,
                       timeout=25)
        executable = '/usr/sbin/openvpn' if service == 'vpn' else '/usr/sbin/sockd'
        gateway.docker('run', '--rm', '--cap-drop', 'ALL', '--read-only', '--network', 'none',
                       '--entrypoint', executable, tag, '--version' if service == 'vpn' else '-v', timeout=25)
        refs[service] = tag
    return refs


def private_inventory(root):
    """Hash only preservation targets; never read credentials or runtime."""
    paths = [root/name for name in ('gateway.toml', 'settings.json', 'git-changes.json')]
    paths += list((root/'config').iterdir())
    result = {}
    for path in paths:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ProductError('MIGRATION_INPUT', 'A private preservation target is not a regular file.')
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def check_containers(gateway, root):
    images = {}
    for service in ('vpn', 'socks'):
        cid = gateway.container_id(service)
        if not cid:
            continue
        data = json.loads(gateway.docker('inspect', cid).stdout)[0]
        labels = data.get('Config', {}).get('Labels', {})
        if (labels.get('com.docker.compose.project') != gateway.PROJECT or
                labels.get('com.docker.compose.service') != service or
                Path(labels.get('com.docker.compose.project.working_dir', '')).resolve() != root):
            raise ProductError('MIGRATION_OWNERSHIP', 'Existing containers are not owned by this installation.')
        images[service] = data['Image']
    return images


def apply_files(package, root, installer, marker, refs, old_images):
    """Gateway must be stopped and lifecycle lock held by caller."""
    changes = application_changes(package, root, installer)
    preservation = private_inventory(root)
    before = {}
    for name in changes:
        path = setup_wizard.checked_destination(root, name)
        before[name] = path.read_bytes() if path.is_file() else None
    backup = root/'backups'/('app-update-' + str(time.time_ns()))
    host.private_directory(backup, parents=True)
    for name, data in before.items():
        if data is not None:
            path = backup/name
            host.private_directory(path.parent, parents=True, exist_ok=True)
            host.private_write(path, data)
    # Preservation copies are not destinations in the recovery journal.
    for name in preservation:
        path = backup/'private-preservation'/name
        host.private_directory(path.parent, parents=True, exist_ok=True)
        host.private_write(path, (root/name).read_bytes())
    host.private_write(backup/'docker-images.json', json.dumps(old_images))
    host.private_write(backup/'inventory.json', json.dumps({name: value is not None for name, value in before.items()}))
    host.private_write(backup/'installation.json', json.dumps(marker))
    host.private_write(root/'update-pending.json', json.dumps({'backup': str(backup), 'files': list(changes)}))
    try:
        for name, data in changes.items():
            path = setup_wizard.checked_destination(root, name)
            host.private_directory(path.parent, parents=True, exist_ok=True)
            host.private_write(path, data, mode=0o700 if name.startswith('bin/') else 0o600)
        updated = dict(marker, kit_version=(package/'VERSION').read_text().strip(), installed_backend='docker',
                       docker_images=refs, migration_backup=str(backup),
                       app_hashes={name: installer.digest(data) for name, data in changes.items()})
        host.private_write(root/'installation.json', json.dumps(updated, indent=2))
        result = subprocess.run([str(root/'bin'/'vpn-gateway'), '--version'], capture_output=True, timeout=20)
        if result.returncode or result.stdout.decode().strip() != 'vpn-gateway ' + updated['kit_version']:
            raise ProductError('MIGRATION_SMOKE', 'Migrated launcher smoke check failed.')
        if private_inventory(root) != preservation:
            raise ProductError('MIGRATION_PRESERVATION', 'Private preservation hashes changed; migration acceptance failed.')
        (root/'update-pending.json').unlink()
    except BaseException:
        setup_wizard.recover_update(root, installer, locked=True)
        raise
    return backup


def migrate(package, root, installer):
    if host.SYSTEM != 'Darwin' or host.IS_WINDOWS:
        raise ProductError('MIGRATION_PLATFORM', 'This reviewed migration is for macOS Docker only.')
    package, root = Path(package).resolve(), Path(root).resolve()
    marker = setup_wizard.owned(root)
    if package == root or marker.get('kit_version') not in SUPPORTED:
        raise ProductError('MIGRATION_VERSION', 'Use a verified source checkout with a reviewed installed version.')
    installer.verify_manifest(package)
    gateway = gateway_at(package, root, installer)
    if gateway.active_backend() != 'docker' or gateway.backend_name() != 'docker':
        raise ProductError('MIGRATION_BACKEND', 'This migration cannot switch an existing backend.')
    config = gateway.configuration()
    installer.load_profiles({name: root/'config'/row['profile_file'] for name, row in config['transports'].items()}, gateway, config)
    setup_wizard.verify_installed_files(root, marker, installer)
    # Refuse foreign command links; preserve legacy aliases exactly as they are.
    names = ['vpn-gateway', 'vpn-browser']
    if marker.get('legacy_aliases'):
        names += ['corp-vpn', 'corp-browser']
    for name in names:
        link = Path.home()/'.local/bin'/name
        if link.exists() and (not link.is_symlink() or not link.resolve().is_relative_to(root/'bin')):
            raise ProductError('MIGRATION_LAUNCHER', 'A command name is not an owned gateway symlink.')
    preservation = private_inventory(root)
    check_containers(gateway, root)
    refs = stage_images(package, gateway)
    if not confirm('Staged images passed checks. Stop this gateway and migrate its application (fresh VPN credentials required afterward)'):
        return False
    with host.lifecycle_lock(root/'.lock'):
        setup_wizard.verify_installed_files(root, marker, installer)
        if setup_wizard.owned(root) != marker or private_inventory(root) != preservation:
            raise ProductError('MIGRATION_CHANGED', 'Installation changed during staging; retry after reviewing it.')
        old_images = check_containers(gateway, root)
        gateway.stop_internal(backend='docker')
        for name in ('client.ovpn', 'selected-transport', 'active-backend'):
            (root/'runtime'/name).unlink(missing_ok=True)
        backup = apply_files(package, root, installer, marker, refs, old_images)
    print('macOS migration completed; gateway is stopped and credentials removed.')
    print('Private rollback backup: ' + str(backup))
    print('Next: vpn-gateway start --open-browser')
    return True


def rollback(package, root, installer):
    """Restore this migration's recorded application; never restore credentials."""
    if host.SYSTEM != 'Darwin' or host.IS_WINDOWS:
        raise ProductError('MIGRATION_PLATFORM', 'Docker migration rollback is macOS-only.')
    package, root = Path(package).resolve(), Path(root).resolve()
    installer.verify_manifest(package)
    marker = setup_wizard.owned(root)
    backup = Path(marker.get('migration_backup', ''))
    if backup.parent != root/'backups' or not backup.name.startswith('app-update-') or backup.is_symlink():
        raise ProductError('MIGRATION_BACKUP', 'No valid migration backup was recorded; nothing changed.')
    inventory = read_object(backup/'inventory.json')
    if not set(inventory) <= setup_wizard.update_allowlist(installer):
        raise ProductError('MIGRATION_BACKUP', 'Unexpected backup inventory; nothing changed.')
    gateway = gateway_at(package, root, installer)
    setup_wizard.verify_installed_files(root, marker, installer)
    if not confirm('Stop the gateway and restore the previous application; a new start needs fresh credentials'):
        return False
    with host.lifecycle_lock(root/'.lock'):
        check_containers(gateway, root)
        gateway.stop_internal(backend='docker')
        for name in ('client.ovpn', 'selected-transport', 'active-backend'):
            (root/'runtime'/name).unlink(missing_ok=True)
        host.private_write(root/'update-pending.json', json.dumps({'backup': str(backup), 'files': list(inventory)}))
        setup_wizard.recover_update(root, installer, locked=True)
    print('Previous application restored. Old Docker image tags were not overwritten by migration. Run vpn-gateway start.')
    return True
