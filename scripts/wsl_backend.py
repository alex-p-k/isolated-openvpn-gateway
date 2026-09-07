#!/usr/bin/env python3
"""Pure helpers for the managed Docker-free WSL2 backend."""
from __future__ import annotations

import configparser
import ipaddress
from pathlib import Path
import re


DISTRO = 'IsolatedOpenVPNGateway'
BASE_DISTRIBUTION = 'Debian'
INTERNAL_SOCKS_PORT = 11080
MIN_WSL_VERSION = (2, 4, 4)


def normalize_output(value):
    """Remove UTF-16 NUL artifacts produced by some inbox wsl.exe builds."""
    if isinstance(value, bytes):
        encoding = 'utf-16' if value.startswith((b'\xff\xfe', b'\xfe\xff')) else (
            'utf-16-le' if b'\x00' in value else 'utf-8')
        return value.decode(encoding, errors='replace').replace('\r', '')
    return str(value or '').replace('\x00', '').replace('\r', '')


def parse_version(value):
    match = re.search(r'(?im)^.*\bWSL\b[^0-9\r\n]*([0-9]+(?:\.[0-9]+){1,3})',
                      normalize_output(value))
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split('.'))


def modern_install_supported(value):
    version = parse_version(value)
    if version is None:
        return False
    padded = version + (0,) * (3 - len(version))
    return padded[:3] >= MIN_WSL_VERSION


def distro_names(value):
    lines = []
    for line in normalize_output(value).splitlines():
        name = line.strip().lstrip('*').strip()
        if name and not name.lower().startswith(('windows subsystem', 'copyright')):
            lines.append(name)
    return lines


def distro_is_installed(value, distro=DISTRO):
    return distro.casefold() in {name.casefold() for name in distro_names(value)}


def distro_version(value, distro=DISTRO):
    for line in normalize_output(value).splitlines():
        fields = line.strip().lstrip('*').strip().split()
        if len(fields) >= 3 and fields[0].casefold() == distro.casefold() and fields[-1] in {'1', '2'}:
            return int(fields[-1])
    return None


def wsl_command(*args, distro=DISTRO, user='root', executable='wsl.exe'):
    command = [executable, '--distribution', distro]
    if user:
        command += ['--user', user]
    return command + ['--exec', *[str(value) for value in args]]


def install_command(location, distro=DISTRO, base=BASE_DISTRIBUTION,
                    executable='wsl.exe'):
    return [executable, '--install', base, '--name', distro, '--location',
            str(Path(location)), '--no-launch']


def unregister_command(distro=DISTRO, executable='wsl.exe'):
    return [executable, '--unregister', distro]


def terminate_command(distro=DISTRO, executable='wsl.exe'):
    return [executable, '--terminate', distro]


def edition_is_home(edition_id='', product_name=''):
    edition = str(edition_id).casefold()
    product = str(product_name).casefold()
    return edition.startswith('core') or ' home' in product or product.startswith('home')


def architecture_supported(value):
    return str(value).upper() in {'AMD64', 'X86_64', 'ARM64'}


def networking_mode(path):
    """Read %USERPROFILE%/.wslconfig without ever changing it."""
    path = Path(path)
    if not path.is_file():
        return 'nat'
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, configparser.Error):
        return 'unknown'
    value = parser.get('wsl2', 'networkingMode', fallback='nat').strip().casefold()
    return value if value in {'nat', 'mirrored', 'virtioproxy', 'none'} else 'unknown'


def listener_validation(records, port):
    """Require a real IPv4 loopback listener and reject every wildcard/LAN bind."""
    selected = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        try:
            if int(record.get('LocalPort')) != int(port):
                continue
        except (TypeError, ValueError):
            continue
        selected.append(str(record.get('LocalAddress', '')).strip())
    return {
        'listener_present': '127.0.0.1' in selected,
        'loopback_only': bool(selected) and set(selected) == {'127.0.0.1'},
        'ipv4_wildcard_absent': '0.0.0.0' not in selected,
        'ipv6_wildcard_absent': '::' not in selected and '[::]' not in selected,
        'addresses': selected,
    }


def parse_public_ip(value):
    try:
        address = ipaddress.ip_address(normalize_output(value).strip())
        return str(address) if address.version == 4 else None
    except ValueError:
        return None


def powershell_literal(value):
    """Quote data for a PowerShell single-quoted literal; never for a command."""
    value = str(value)
    if any(ch in value for ch in ('\r', '\n', '\x00')):
        raise ValueError('PowerShell data contains a forbidden control character.')
    return "'" + value.replace("'", "''") + "'"
