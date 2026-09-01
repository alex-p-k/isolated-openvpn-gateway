#!/usr/bin/env python3
"""Host-platform boundary for macOS and Windows.

Container networking stays Linux-only. This module contains the small amount
of host-specific filesystem, executable discovery, ACL and locking behavior.
"""
import contextlib
import csv
import os
from pathlib import Path
import platform
import shutil
import subprocess


SYSTEM = platform.system()
IS_WINDOWS = SYSTEM == 'Windows'


def install_root(home=None, environ=None, system=None):
    home = Path.home() if home is None else Path(home)
    environ = os.environ if environ is None else environ
    system = SYSTEM if system is None else system
    if system == 'Windows':
        base = Path(environ.get('LOCALAPPDATA') or home/'AppData'/'Local')
        return base/'IsolatedOpenVPNGateway'
    return home/'.local'/'share'/'isolated-openvpn-gateway'


def browser_root(home=None, environ=None, system=None):
    home = Path.home() if home is None else Path(home)
    environ = os.environ if environ is None else environ
    system = SYSTEM if system is None else system
    if system == 'Windows':
        base = Path(environ.get('LOCALAPPDATA') or home/'AppData'/'Local')
        return base/'IsolatedOpenVPNBrowser'
    return home/'.local'/'share'/'isolated-openvpn-browser'


def command_directory(root, home=None, system=None):
    system = SYSTEM if system is None else system
    return Path(root)/'bin' if system == 'Windows' else Path(home or Path.home())/'.local'/'bin'


def docker_executable(environ=None, system=None):
    environ = os.environ if environ is None else environ
    system = SYSTEM if system is None else system
    candidates = [Path(shutil.which('docker') or ('docker.exe' if system == 'Windows' else '/usr/local/bin/docker'))]
    if system == 'Windows':
        for variable in ('ProgramFiles', 'LOCALAPPDATA'):
            if environ.get(variable):
                base = Path(environ[variable])
                candidates.extend((base/'Docker'/'Docker'/'resources'/'bin'/'docker.exe',
                                   base/'Programs'/'Docker'/'Docker'/'resources'/'bin'/'docker.exe'))
    else:
        candidates.extend((Path.home()/'.docker'/'bin'/'docker',
                           Path('/Applications/Docker.app/Contents/Resources/bin/docker'),
                           Path.home()/'Applications/Docker.app/Contents/Resources/bin/docker'))
    return next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)),
                'docker.exe' if system == 'Windows' else '/usr/local/bin/docker')


def curl_executable(environ=None, system=None):
    environ = os.environ if environ is None else environ
    system = SYSTEM if system is None else system
    found = shutil.which('curl.exe' if system == 'Windows' else 'curl')
    if found:
        return found
    if system == 'Windows' and environ.get('SystemRoot'):
        return str(Path(environ['SystemRoot'])/'System32'/'curl.exe')
    return '/usr/bin/curl'


def powershell_executable(environ=None):
    environ = os.environ if environ is None else environ
    found = shutil.which('powershell.exe') or shutil.which('pwsh.exe') or shutil.which('pwsh')
    if found:
        return found
    if environ.get('SystemRoot'):
        return str(Path(environ['SystemRoot'])/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe')
    return 'powershell.exe'


def browser_candidates(home=None, environ=None, system=None):
    home = Path.home() if home is None else Path(home)
    environ = os.environ if environ is None else environ
    system = SYSTEM if system is None else system
    if system == 'Windows':
        bases = [Path(environ[x]) for x in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA') if environ.get(x)]
        paths = []
        for base in bases:
            paths.extend((base/'Google'/'Chrome'/'Application'/'chrome.exe',
                          base/'Chromium'/'Application'/'chrome.exe',
                          base/'Microsoft'/'Edge'/'Application'/'msedge.exe'))
        return tuple(dict.fromkeys(paths))
    return (Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
            Path('/Applications/Chromium.app/Contents/MacOS/Chromium'),
            home/'Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            home/'Applications/Chromium.app/Contents/MacOS/Chromium')


def ssh_executable(environ=None, system=None):
    environ = os.environ if environ is None else environ
    system = SYSTEM if system is None else system
    found = shutil.which('ssh.exe' if system == 'Windows' else 'ssh')
    if found:
        return found
    if system == 'Windows':
        candidates = []
        if environ.get('SystemRoot'):
            candidates.append(Path(environ['SystemRoot'])/'System32'/'OpenSSH'/'ssh.exe')
        if environ.get('ProgramFiles'):
            candidates.append(Path(environ['ProgramFiles'])/'Git'/'usr'/'bin'/'ssh.exe')
        return str(next((p for p in candidates if p.is_file()), candidates[0] if candidates else Path('ssh.exe')))
    return '/usr/bin/ssh'


def ssh_config_paths(home=None, environ=None, system=None):
    home = Path.home() if home is None else Path(home)
    environ = os.environ if environ is None else environ
    system = SYSTEM if system is None else system
    if system == 'Windows':
        program_data = Path(environ.get('ProgramData') or 'C:/ProgramData')
        return home/'.ssh'/'config', program_data/'ssh'/'ssh_config'
    return Path('~/.ssh/config'), Path('/etc/ssh/ssh_config')


def is_elevated(system=None):
    system = SYSTEM if system is None else system
    if system == 'Windows':
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return None
    return os.geteuid() == 0


def windows_current_sid():
    result = subprocess.run(['whoami.exe', '/user', '/fo', 'csv', '/nh'],
                            capture_output=True, text=True, timeout=8)
    if result.returncode:
        raise OSError('Cannot determine the current Windows user SID.')
    rows = list(csv.reader(result.stdout.splitlines()))
    if len(rows) != 1 or len(rows[0]) < 2 or not rows[0][1].startswith('S-1-'):
        raise OSError('Cannot parse the current Windows user SID.')
    return rows[0][1]


def windows_acl_command(path, sid, directory=False):
    grant = '*' + sid + (':(OI)(CI)(F)' if directory else ':(F)')
    return ['icacls.exe', str(path), '/inheritance:r', '/grant:r', grant]


def secure_windows_acl(path, directory=False):
    result = subprocess.run(windows_acl_command(path, windows_current_sid(), directory),
                            capture_output=True, text=True, timeout=12)
    if result.returncode:
        raise OSError('Failed to apply a private NTFS ACL.')


def private_directory(path, *, parents=False, exist_ok=False, system=None):
    system = SYSTEM if system is None else system
    path = Path(path)
    if path.is_symlink():
        raise OSError('Refusing a symlink for a private directory: ' + str(path))
    existed = path.exists()
    path.mkdir(mode=0o700, parents=parents, exist_ok=exist_ok)
    if system == 'Windows':
        try:
            secure_windows_acl(path, directory=True)
        except OSError:
            if not existed:
                with contextlib.suppress(OSError):
                    path.rmdir()
            raise
    else:
        path.chmod(0o700)
    return path


def private_write(path, data, *, mode=0o600, exclusive=False, system=None):
    system = SYSTEM if system is None else system
    path = Path(path)
    if path.is_symlink():
        raise OSError('Refusing a symlink for a private file: ' + str(path))
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, mode)
    binary = isinstance(data, bytes)
    if binary:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
    else:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(data)
    if system == 'Windows':
        try:
            secure_windows_acl(path)
        except OSError:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
    else:
        path.chmod(mode)


def batch_launcher(python_executable, script_name):
    # Percent expansion occurs even inside quotes in cmd.exe.
    python = str(python_executable).replace('%', '%%').replace('\r', '').replace('\n', '')
    return ('@echo off\r\n'
            '@chcp 65001 >nul\r\n'
            'set "PYTHONDONTWRITEBYTECODE=1"\r\n'
            '"' + python + '" "%~dp0..\\scripts\\' + script_name + '" %*\r\n').encode()


def ensure_windows_command_paths(*paths):
    for path in paths:
        value = str(path)
        if any(character in value for character in ('%', '!', '"', '\r', '\n')):
            raise OSError('Windows install and Python paths cannot contain %, !, quotes or newlines.')


@contextlib.contextmanager
def lifecycle_lock(path):
    path = Path(path)
    if IS_WINDOWS:
        import msvcrt
        with path.open('a+b') as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise BlockingIOError('Another lifecycle command is still running.') from None
            try:
                yield
            finally:
                handle.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        with path.open('w') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BlockingIOError('Another lifecycle command is still running.') from None
            yield


@contextlib.contextmanager
def maybe_lifecycle_lock(path, enabled):
    if enabled:
        with lifecycle_lock(path):
            yield
    else:
        yield
