#!/usr/bin/env python3
"""Export only reviewed source files, even if private data exists next to them."""
import argparse
import hashlib
import importlib.util
from pathlib import Path
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('kit_install', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def manifest_bytes(package):
    return ''.join(hashlib.sha256(installer.safe_source(package, name).read_bytes()).hexdigest() +
                   '  ' + name + '\n' for name in installer.PACKAGE_FILES).encode()


def build(package, archive):
    entries = {name: installer.safe_source(package, name).read_bytes() for name in installer.PACKAGE_FILES}
    entries['MANIFEST.sha256'] = manifest_bytes(package)
    # No directory walk, no config/runtime data, no symlink or metadata export.
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as out:
        for name, data in entries.items():
            info = zipfile.ZipInfo('isolated-openvpn-gateway/' + name, date_time=(2026, 8, 30, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            out.writestr(info, data)
    archive.chmod(0o600)
    return hashlib.sha256(archive.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--manifest-only', action='store_true')
    args = parser.parse_args()
    (ROOT / 'MANIFEST.sha256').write_bytes(manifest_bytes(ROOT))
    if args.manifest_only:
        print('MANIFEST.sha256 refreshed from the explicit source allowlist.')
        return
    if args.output is None:
        parser.error('--output is required unless --manifest-only is used')
    value = build(ROOT, args.output)
    checksum = args.output.with_suffix(args.output.suffix + '.sha256')
    with checksum.open('x') as stream:
        stream.write(value + '  ' + args.output.name + '\n')
    checksum.chmod(0o600)
    print('Built source-only release: ' + str(args.output))
    print('SHA256: ' + value)


if __name__ == '__main__':
    main()
