#!/usr/bin/env python3
"""Launch a dedicated browser profile; never edit a user's main profile."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import re
from urllib.parse import urlsplit

from gateway_config import BROWSER_CLI, PROJECT, ConfigError, load_config
import host
from product import ProductError, ask, read_object, error_text


ROOT = Path(__file__).resolve().parent.parent
PROFILE = None  # Resolve only when launching, not during imports/tests/help.


def browser_flags(configuration):
    proxy = '%s:%d' % (configuration['socks_host'], configuration['socks_port'])
    return ['--proxy-server=socks5://' + proxy,
            '--proxy-bypass-list=<-loopback>',
            '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE localhost , EXCLUDE 127.0.0.1',
            '--disable-quic', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
            '--disable-features=DnsOverHttps', '--disable-background-networking',
            '--no-first-run', '--no-default-browser-check']


def validate_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password
                or any(ord(ch) < 32 for ch in value) or '\\' in value):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise ProductError('BROWSER_URL', 'Supply an http(s) URL without embedded credentials or control characters.',
                           'vpn-gateway browser https://YOUR-CORPORATE-HOST/') from None
    return value


def saved_url(value):
    validate_url(value)
    if urlsplit(value).query or urlsplit(value).fragment:
        raise ProductError('BROWSER_URL', 'Saved start URLs must not contain a query or fragment; use a plain landing-page URL.')
    return value


def firefox_preferences(configuration):
    return {
        'network.proxy.type': 1, 'network.proxy.socks': configuration['socks_host'],
        'network.proxy.socks_port': configuration['socks_port'], 'network.proxy.socks_version': 5,
        'network.proxy.socks5_remote_dns': True, 'network.proxy.socks_remote_dns': True,
        'network.proxy.no_proxies_on': '', 'network.proxy.failover_direct': False,
        'network.proxy.allow_bypass': False, 'network.proxy.allow_hijacking_localhost': True,
        'network.proxy.http': '', 'network.proxy.ssl': '', 'network.proxy.autoconfig_url': '',
        'network.trr.mode': 5, 'network.http.http3.enable': False,
        'network.dns.disablePrefetch': True, 'network.prefetch-next': False,
        'network.predictor.enabled': False, 'network.http.speculative-parallel-limit': 0,
        'media.peerconnection.enabled': False, 'browser.shell.checkDefaultBrowser': False,
        'browser.startup.page': 0, 'browser.startup.homepage': 'about:blank',
        'browser.newtabpage.enabled': False, 'network.captive-portal-service.enabled': False,
        'network.connectivity-service.enabled': False,
    }


def firefox_version(executable):
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if host.IS_WINDOWS else {}
    result = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=10, **options)
    match = re.search(r'Firefox\s+(\d+)\.', result.stdout)
    if result.returncode or not match or int(match[1]) < 128:
        raise ProductError('FIREFOX_VERSION', 'Firefox 128+ is required; the executable version could not be accepted.',
                           'Install a currently maintained Firefox release from https://www.mozilla.org/firefox/')
    return int(match[1])


def firefox_busy(profile):
    if not host.IS_WINDOWS:
        # Firefox retains .parentlock after exit. Test its POSIX record lock,
        # not existence (flock is a different, incompatible locking API).
        import fcntl
        lock = profile/'.parentlock'
        if lock.is_symlink():
            return True  # A legacy/NFS lock needs manual review; never remove it.
        try:
            with lock.open('rb') as stream:
                fcntl.lockf(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.lockf(stream, fcntl.LOCK_UN)
        except FileNotFoundError:
            return os.path.lexists(profile/'lock')
        except OSError:
            return True
        legacy = profile/'lock'
        if os.path.lexists(legacy):
            # Mozilla marks modern obsolete symlinks with +PID: obtaining the
            # record lock above proves that modern process no longer owns it.
            try:
                return not bool(re.fullmatch(r'[0-9.]+:\+\d+', os.readlink(legacy)))
            except OSError:
                return True
        return False
    lock = profile/'parent.lock'
    try:
        with lock.open('r+b'):
            pass
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    return False


def gateway_ready():
    import gateway
    import cli_ui
    return cli_ui.report(gateway.api())['state'] == 'ready'


def main(argv=None):
    parser = argparse.ArgumentParser(prog=BROWSER_CLI, description=__doc__)
    parser.add_argument('--browser', choices=('chromium', 'firefox'), help='one launch only; save the choice using setup')
    parser.add_argument('urls', nargs='*')
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    marker = {}
    try:
        marker = json.loads((ROOT/'installation.json').read_text())
    except (OSError, ValueError):
        pass
    if marker.get('project') != PROJECT or not host.installation_root_matches(ROOT, marker):
        command = host.command_directory(ROOT)/(BROWSER_CLI+'.cmd' if host.IS_WINDOWS else BROWSER_CLI)
        raise ProductError('NOT_INSTALLED', 'The gateway installation is not owned or is missing.', 'Run .\\setup.cmd from the checkout.')
    try:
        configuration = load_config(ROOT/'gateway.toml')
    except ConfigError as exc:
        raise ProductError('CONFIG_INVALID', 'The deployment configuration is invalid.', 'vpn-gateway setup') from None
    if not gateway_ready():
        raise ProductError('GATEWAY_STOPPED', 'The gateway is not ready; no browser was launched.', 'vpn-gateway start --open-browser')
    settings = read_object(ROOT/'settings.json')
    kind = args.browser or settings.get('browser_kind', 'chromium')
    if kind not in ('chromium', 'firefox'):
        raise ProductError('BROWSER_CONFIG', 'Unknown saved browser selection.', 'vpn-gateway setup')
    urls = args.urls or ([settings['browser_url']] if settings.get('browser_url') else [ask('Corporate http(s) URL')])
    urls = [validate_url(value) for value in urls]
    os.umask(0o077)
    candidates = host.firefox_candidates() if kind == 'firefox' else host.browser_candidates()
    browser=next((str(x) for x in candidates if x.is_file()),None)
    if not browser:
        raise ProductError('BROWSER_MISSING', 'The selected browser is not installed; no alternate browser was opened.', 'vpn-gateway setup')
    profile = PROFILE or host.browser_root()
    if kind == 'firefox':
        profile = profile.with_name(profile.name + '-Firefox')
        firefox_version(browser)
    ownership = profile/'.isolated-openvpn-gateway-owned'
    if any(p.is_symlink() for p in (profile, *profile.parents)):
        raise ProductError('BROWSER_OWNERSHIP', 'Refusing a symlink in the private browser path.')
    if profile.exists() and not ownership.is_file():
        raise ProductError('BROWSER_OWNERSHIP', 'An existing browser directory is not project-owned.')
    if not profile.exists():
        host.private_directory(profile, parents=True)
    elif not host.IS_WINDOWS:
        profile.chmod(0o700)
    if not ownership.exists():
        host.private_write(ownership, b'', exclusive=True)
    process_args = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if host.IS_WINDOWS:
        process_args['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        process_args['start_new_session'] = True
    with host.lifecycle_lock(profile/'.gateway-launch.lock'):
        if kind == 'firefox':
            if firefox_busy(profile):
                raise ProductError('BROWSER_BUSY', 'The separate Firefox profile is already open or locked; no URL was sent to another profile.',
                                   'Use its open window, or close that profile and run vpn-gateway browser again.')
            preferences = firefox_preferences(configuration)
            text = '// Managed SOCKS-only profile. Main Firefox settings are untouched.\n'
            text += ''.join('user_pref(' + json.dumps(key) + ', ' + json.dumps(value) + ');\n'
                            for key, value in preferences.items())
            host.private_write(profile/'user.js', text)
            command = [browser, '-no-remote', '-profile', str(profile), *urls]
        else:
            command = [browser, '--user-data-dir='+str(profile), *browser_flags(configuration), *urls]
        subprocess.Popen(command, **process_args)
    print('Browser launch requested: separate ' + kind + ' profile, SOCKS5-only configuration.')
    if kind == 'chromium':
        print('Chrome may warn about --host-resolver-rules. This flag blocks local DNS; it was not removed or hidden.')
    else:
        print('Firefox support: experimental until real DNS and positive/negative network acceptance passes on this version.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('Cancelled; no credentials were stored.', file=sys.stderr)
        sys.exit(130)
    except (ProductError, OSError, subprocess.TimeoutExpired) as exc:
        print(error_text(exc), file=sys.stderr)
        sys.exit(1)
