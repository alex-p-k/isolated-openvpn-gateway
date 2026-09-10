"""Opt-in HKCU PATH integration with exact-entry ownership and rollback."""
import json
import os
from pathlib import Path
import subprocess

import host
from product import ProductError, read_object


def normalize(value):
    return os.path.normcase(os.path.normpath(os.path.expandvars(value.strip().strip('"')))).casefold()


def add_entry(current, entry):
    parts = current.split(';') if current else []
    if any(normalize(part) == normalize(entry) for part in parts if part):
        return current, False
    return entry + (';' + current if current else ''), True


def remove_entry(current, entry):
    parts = current.split(';')
    matches = [i for i, part in enumerate(parts) if normalize(part) == normalize(entry)]
    if len(matches) > 1:
        raise ProductError('PATH_CONFLICT', 'The owned PATH entry was duplicated; review it before removing the gateway.')
    if matches:
        parts.pop(matches[0])
    return ';'.join(parts)


def read_user_path():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
        try:
            return winreg.QueryValueEx(key, 'Path')
        except FileNotFoundError:
            return '', winreg.REG_EXPAND_SZ


def write_user_path(value, kind):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, 'Path', 0, kind, value)
    # Inform Explorer; existing terminals deliberately retain their environment.
    import ctypes
    result = ctypes.c_size_t()
    ctypes.windll.user32.SendMessageTimeoutW(0xffff, 0x001A, 0, 'Environment', 2, 2000, ctypes.byref(result))


def register(root):
    root = Path(root)
    entry = str(root/'bin')
    host.ensure_windows_command_paths(entry)
    before, kind = read_user_path()
    after, added = add_entry(before, entry)
    path = root/'path-registration.json'
    record = read_object(path)
    if record and record.get('entry') != entry:
        raise ProductError('PATH_CONFLICT', 'PATH ownership belongs to a different command directory.')
    if not record or added:
        record = dict(entry=entry, owned=added or record.get('owned', False), before=before, kind=kind)
        host.private_write(path, json.dumps(record, indent=2))
    if added:
        try:
            write_user_path(after, kind)
        except BaseException:
            # Remove only this entry from the latest value, preserving concurrent edits.
            latest, latest_kind = read_user_path()
            if normalize(entry) in [normalize(x) for x in latest.split(';')]:
                write_user_path(remove_entry(latest, entry), latest_kind)
            raise
    env = dict(os.environ)
    env['PATH'] = entry + ';' + env.get('PATH', '')
    result = subprocess.run(['where.exe', 'vpn-gateway.cmd'], env=env, capture_output=True, text=True, timeout=10)
    found = result.stdout.splitlines()
    if result.returncode or not found or normalize(found[0]) != normalize(str(root/'bin'/'vpn-gateway.cmd')):
        raise ProductError('PATH_VERIFY', 'The command could not be resolved in a fresh child process.')
    print('User PATH: registered and child-process resolution verified. Open a NEW terminal for short commands.')


def unregister(root):
    record = read_object(Path(root)/'path-registration.json')
    entry = str(Path(root)/'bin')
    if record.get('owned'):
        if record.get('entry') != entry:
            raise ProductError('PATH_CONFLICT', 'Unexpected PATH ownership record; nothing removed.')
        current, kind = read_user_path()
        write_user_path(remove_entry(current, entry), kind)
        print('Removed only the gateway-owned user PATH entry; unrelated entries were preserved.')
    unregister_location(root)


def register_location(root):
    """Discover the physical root from both ordinary and packaged terminals."""
    import winreg
    root = Path(root)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r'Software\IsolatedOpenVPNGateway', 0,
                           winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        try:
            previous, _ = winreg.QueryValueEx(key, 'InstallRoot')
        except FileNotFoundError:
            previous = None
        if previous is not None and normalize(previous) != normalize(str(root)):
            raise ProductError('INSTALL_LOCATION', 'Another install location is registered; it was not overwritten.')
        journal = root/'location-registration.json'
        record = read_object(journal)
        if record and record.get('entry') != str(root):
            raise ProductError('INSTALL_LOCATION', 'Installation-location ownership belongs to a different directory.')
        if not record or previous is None:
            host.private_write(journal, json.dumps({'entry': str(root), 'owned': previous is None}))
        if previous is None:
            winreg.SetValueEx(key, 'InstallRoot', 0, winreg.REG_SZ, str(root))


def unregister_location(root):
    record = read_object(Path(root)/'location-registration.json')
    if not record.get('owned'):
        return
    import winreg
    if record.get('entry') != str(root):
        raise ProductError('INSTALL_LOCATION', 'Unexpected installation-location ownership record.')
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\IsolatedOpenVPNGateway', 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            current, _ = winreg.QueryValueEx(key, 'InstallRoot')
            if normalize(current) != normalize(str(root)):
                raise ProductError('INSTALL_LOCATION', 'Registered installation location changed; it was preserved.')
            winreg.DeleteValue(key, 'InstallRoot')
    except FileNotFoundError:
        pass
