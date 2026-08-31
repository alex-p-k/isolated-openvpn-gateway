#!/usr/bin/env python3
"""Launch a separate SOCKS-only Chrome profile; never edit the main profile."""
import json
import os
from pathlib import Path
import subprocess
import sys

from gateway_config import BROWSER_CLI, BROWSER_DIR, INSTALL_DIR, PROJECT, ConfigError, load_config


ROOT = Path(__file__).resolve().parent.parent
PROFILE = Path.home() / BROWSER_DIR


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
    if marker.get('project') != PROJECT or ROOT != Path.home()/INSTALL_DIR:
        raise SystemExit('Run install.py first, then use ~/.local/bin/'+BROWSER_CLI+'.')
    try:
        configuration = load_config(ROOT/'gateway.toml')
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
    os.umask(0o077)
    binaries=['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
              '/Applications/Chromium.app/Contents/MacOS/Chromium',
              str(Path.home()/'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
              str(Path.home()/'Applications/Chromium.app/Contents/MacOS/Chromium')]
    browser=next((x for x in binaries if Path(x).is_file()),None)
    if not browser:
        raise SystemExit('Google Chrome/Chromium not found; no main browser profile was modified.')
    ownership = PROFILE/'.isolated-openvpn-gateway-owned'
    if PROFILE.is_symlink():
        raise SystemExit('Refusing symlink for private browser profile.')
    if PROFILE.exists() and not ownership.exists():
        raise SystemExit('Existing unowned private browser directory; refusing to overwrite.')
    PROFILE.mkdir(mode=0o700,parents=True,exist_ok=True)
    PROFILE.chmod(0o700)
    ownership.touch(mode=0o600)
    urls=sys.argv[1:] or ['about:blank']
    if any(not x.startswith(('https://','http://','about:blank')) for x in urls):
        raise SystemExit('Only http(s) URLs or about:blank are accepted; additional browser flags are not accepted.')
    subprocess.Popen([browser,'--user-data-dir='+str(PROFILE),*browser_flags(configuration),*urls],
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                     start_new_session=True)
    print('Private browser: separate profile; SOCKS5 only; no direct fallback.')


if __name__ == '__main__':
    main()
