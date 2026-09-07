#!/usr/bin/env python3
"""Install Dante only in the managed WSL distro; never mix Debian releases."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


DANTE_URL = 'https://www.inet.no/dante/files/dante-1.4.4.tar.gz'
DANTE_SHA256 = '1973c7732f1f9f0a4c0ccf2c1ce462c7c25060b25643ea90f9b98f53a813faec'
BINARY = Path('/usr/sbin/danted')


def verified_extract(archive, directory):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != DANTE_SHA256:
        raise RuntimeError('Official Dante source checksum mismatch; nothing was built.')
    with tarfile.open(archive, 'r:gz') as source:
        # data filter rejects absolute/traversing paths and escaping links.
        source.extractall(directory, filter='data')
    return directory / 'dante-1.4.4'


def install():
    if os.geteuid() != 0:
        raise RuntimeError('Managed WSL dependency installation requires root.')
    if BINARY.is_file():
        return
    policy = subprocess.run(['/usr/bin/apt-cache', 'show', 'dante-server'],
                            capture_output=True, text=True, check=False)
    if policy.returncode == 0 and 'Package: dante-server' in policy.stdout:
        subprocess.run(['/usr/bin/apt-get', 'install', '-y', '--no-install-recommends',
                        'dante-server'], check=True)
        subprocess.run(['/usr/bin/systemctl', 'disable', '--now', 'danted.service'], check=False)
        return
    print('Dante is absent from this Debian release; building verified official Dante 1.4.4.', flush=True)
    subprocess.run(['/usr/bin/apt-get', 'install', '-y', '--no-install-recommends',
                    'build-essential'], check=True)
    with tempfile.TemporaryDirectory(prefix='isolated-gateway-dante-') as temporary:
        directory = Path(temporary)
        archive = directory / 'dante.tar.gz'
        subprocess.run(['/usr/bin/curl', '-4', '--noproxy', '*', '--proto', '=https',
                        '--tlsv1.2', '-fsS', '--connect-timeout', '10', '--retry', '2',
                        '--retry-all-errors', '--max-time', '120', '-o', str(archive),
                        DANTE_URL], check=True)
        source = verified_extract(archive, directory)
        # Build output contains no profiles or credentials. Install only the server.
        subprocess.run([str(source/'configure'), '--disable-client', '--without-libwrap',
                        '--without-pam', '--without-gssapi'], cwd=source, check=True)
        subprocess.run(['/usr/bin/make', '-j2'], cwd=source, check=True)
        staged = BINARY.with_name('danted.gateway-installing')
        try:
            shutil.copyfile(source/'sockd'/'sockd', staged)
            staged.chmod(0o755)
            subprocess.run([str(staged), '-v'], check=True)
            staged.replace(BINARY)
        finally:
            staged.unlink(missing_ok=True)
    print('Verified Dante 1.4.4 installed in the managed distro.', flush=True)


if __name__ == '__main__':
    install()
