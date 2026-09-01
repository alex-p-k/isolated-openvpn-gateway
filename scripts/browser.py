#!/usr/bin/env python3
"""Launch a separate SOCKS-only Chrome profile; never edit the main profile."""
import json
import os
from pathlib import Path
import subprocess
import sys

from gateway_config import BROWSER_CLI, PROJECT, ConfigError, load_config
import host


ROOT = Path(__file__).resolve().parent.parent
PROFILE = host.browser_root()


def browser_flags(configuration):
    proxy = '%s:%d' % (configuration['socks_host'], configuration['socks_port'])
    return ['--proxy-server=socks5://' + proxy,
            '--proxy-bypass-list=<-loopback>',
            '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE localhost , EXCLUDE 127.0.0.1',
            '--disable-quic', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
            '--disable-features=DnsOverHttps', '--disable-background-networking',
            '--no-first-run', '--no-default-browser-check']


def main():
    if sys.argv[1:] in (['--help'], ['-h']):
        print(BROWSER_CLI+' [https://private-host/] — separate Chrome/Chromium profile, SOCKS5 only')
        return
    marker = {}
    try:
        marker = json.loads((ROOT/'installation.json').read_text())
    except (OSError, ValueError):
        pass
    if marker.get('project') != PROJECT or ROOT != host.install_root():
        command = host.command_directory(ROOT)/(BROWSER_CLI+'.cmd' if host.IS_WINDOWS else BROWSER_CLI)
        raise SystemExit('Run install.py first, then use '+str(command)+'.')
    try:
        configuration = load_config(ROOT/'gateway.toml')
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
    os.umask(0o077)
    browser=next((str(x) for x in host.browser_candidates() if x.is_file()),None)
    if not browser:
        raise SystemExit('Google Chrome/Chromium not found; no main browser profile was modified.')
    ownership = PROFILE/'.isolated-openvpn-gateway-owned'
    if PROFILE.is_symlink():
        raise SystemExit('Refusing symlink for private browser profile.')
    if PROFILE.exists() and not ownership.exists():
        raise SystemExit('Existing unowned private browser directory; refusing to overwrite.')
    if not PROFILE.exists():
        host.private_directory(PROFILE, parents=True)
    elif not host.IS_WINDOWS:
        PROFILE.chmod(0o700)
    if not ownership.exists():
        host.private_write(ownership, b'', exclusive=True)
    urls=sys.argv[1:] or ['about:blank']
    if any(not x.startswith(('https://','http://','about:blank')) for x in urls):
        raise SystemExit('Only http(s) URLs or about:blank are accepted; additional browser flags are not accepted.')
    process_args = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if host.IS_WINDOWS:
        process_args['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        process_args['start_new_session'] = True
    subprocess.Popen([browser,'--user-data-dir='+str(PROFILE),*browser_flags(configuration),*urls],
                     **process_args)
    print('Private browser: separate profile; SOCKS5 only; no direct fallback.')


if __name__ == '__main__':
    main()
