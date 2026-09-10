"""Repository-local configuration and read-only checks; no global writes."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

from product import ProductError, ask, confirm


def command(gateway, repo, *args, **kwargs):
    result = gateway.run(['git', '-C', str(repo), *args], check=False, **kwargs)
    if result.returncode not in (0, 1) or (result.returncode and args[0] != 'config'):
        raise ProductError('GIT_COMMAND', 'Git could not inspect the selected repository; command output was withheld.',
                           'Check the repository path and Git installation, then retry.')
    return result


def expected(gateway, repo, remote=None):
    repo = Path(repo).expanduser().resolve()
    names = command(gateway, repo, 'remote').stdout.splitlines()
    if not names:
        raise ProductError('GIT_REMOTE', 'This repository has no remote; nothing was changed.')
    if remote is None:
        remote = names[0] if len(names) == 1 else ask('Remote name', 'origin' if 'origin' in names else None)
    if remote not in names or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', remote):
        raise ProductError('GIT_REMOTE', 'Choose an existing simple remote name.')
    fetch = command(gateway, repo, 'remote', 'get-url', '--all', remote).stdout.splitlines()
    push = command(gateway, repo, 'remote', 'get-url', '--push', '--all', remote).stdout.splitlines()
    if len(fetch) != 1 or push != fetch:
        raise ProductError('GIT_REMOTE_COMPLEX', 'Separate/multiple fetch or push URLs require manual scoped review.')
    url = fetch[0]
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise ProductError('GIT_REMOTE', 'Malformed remote URL; its private value was not printed.') from None
    cfg = gateway.configuration()
    config, text = None, None
    if parsed.scheme == 'https':
        gateway.safe_target(url)
        key = 'http.' + url + '.proxy'
        value = f'socks5h://{cfg["socks_host"]}:{cfg["socks_port"]}'
    else:
        if ('://' in url and parsed.scheme != 'ssh') or '\\' in url or re.match(r'^[A-Za-z]:/', url):
            raise ProductError('GIT_REMOTE', 'Only HTTPS and SSH remotes are supported; nothing was changed.')
        match = re.fullmatch(r'(?:[^@/:\s]+@)?([^/:\s]+):[^\r\n]+', url)
        name = parsed.hostname if parsed.scheme == 'ssh' else match.group(1) if match else None
        if not name or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', name) or parsed.password or parsed.query or parsed.fragment:
            raise ProductError('GIT_REMOTE', 'Unsupported or credential-bearing remote URL.')
        key = 'core.sshCommand'
        config = gateway.ROOT/'config'/('ssh-repo-' + hashlib.sha256(str(repo).encode()).hexdigest()[:16] + '.conf')
        value, text = gateway.repository_ssh_settings(name, config)
    previous = command(gateway, repo, 'config', '--local', '--get-all', key).stdout.splitlines()
    effective = (command(gateway, repo, 'config', '--get-urlmatch', 'http.proxy', url).stdout.strip()
                 if parsed.scheme == 'https' else command(gateway, repo, 'config', '--get', key).stdout.strip())
    env_override = any(os.environ.get(name) for name in
                       ('GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'NO_PROXY', 'no_proxy'))
    remote_proxy = command(gateway, repo, 'config', '--get-all', 'remote.' + remote + '.proxy')
    remote_override = parsed.scheme == 'https' and remote_proxy.returncode == 0 and remote_proxy.stdout.splitlines() != [value]
    helper_matches = config is None or (config.is_file() and not config.is_symlink() and config.read_text(encoding='utf-8') == text)
    return dict(repo=repo, remote=remote, url=url, key=key, value=value, previous=previous,
                effective=effective, helper=config, helper_text=text, helper_matches=helper_matches,
                override=env_override or remote_override,
                configured=previous == [value] and effective == value and helper_matches and not env_override and not remote_override)


def configure(gateway, repo, remote=None):
    item = expected(gateway, repo, remote)
    if item['configured']:
        print('Git: already configured for this repository and remote. No changes made.')
        return 0
    if item['override']:
        raise ProductError('GIT_OVERRIDE', 'A Git/SSH/no-proxy environment or remote.proxy override exists; no configuration was changed.',
                           'Review the environment and git config --show-origin for this repository, then retry.')
    helper = item['helper']
    if helper and os.path.lexists(helper) and not item['helper_matches']:
        raise ProductError('GIT_HELPER_CONFLICT', 'An existing SSH helper differs; it was preserved for manual review.')
    print('Preview: repository-local ' + ('HTTPS remote proxy -> socks5h loopback' if helper is None else 'core.sshCommand -> owned SOCKS bridge'))
    print('Global Git/SSH configuration will not be changed. Existing private values are not printed.')
    if not confirm('Apply this repository-local change' + (' and replace its existing local value' if item['previous'] else '')):
        print('Git configuration cancelled; no changes made.')
        return 0
    config_path = Path(command(gateway, item['repo'], 'rev-parse', '--path-format=absolute', '--git-path', 'config').stdout.strip())
    backup = gateway.ROOT/'backups'/('git-config-' + str(time.time_ns()) + '.backup')
    gateway.private_write(backup, config_path.read_bytes())
    records = gateway.read_json(gateway.ROOT/'git-changes.json', [])
    existing = next((r for r in records if r['repo'] == str(item['repo']) and r['key'] == item['key']), None)
    record = dict(repo=str(item['repo']), key=item['key'], value=item['value'], backup=str(backup), previous=item['previous'])
    if existing:
        # Preserve the original rollback only if our previously installed value is still present.
        if item['previous'] == [existing['value']]:
            record['previous'] = existing.get('previous', [])
        records.remove(existing)
    records.append(record)
    made_helper = False
    try:
        if helper and not helper.exists():
            gateway.private_write(helper, item['helper_text'])
            made_helper = True
        command(gateway, item['repo'], 'config', '--local', '--replace-all', item['key'], item['value'])
        if not expected(gateway, item['repo'], item['remote'])['configured']:
            raise ProductError('GIT_EFFECTIVE_CONFLICT', 'A higher-priority Git setting prevents this proxy from taking effect; change rolled back.')
        gateway.private_write(gateway.ROOT/'git-changes.json', json.dumps(records, indent=2))
    except BaseException:
        gateway.host.private_write(config_path, backup.read_bytes())
        if made_helper:
            helper.unlink()
        raise
    print('Git configured for this repository only. Next: vpn-gateway git check "' + str(item['repo']) + '" --remote')
    return 0


def check(gateway, repo, remote=None, network=False):
    item = expected(gateway, repo, remote)
    print('Repository-local proxy: ' + ('configured' if item['configured'] else 'NOT configured or overridden'))
    if not item['configured']:
        print('Next: vpn-gateway git configure "' + str(item['repo']) + '"')
        return 1
    if not network:
        print('Remote access: not checked (use --remote for read-only git ls-remote).')
        return 0
    if not gateway.ready(gateway.active_backend()):
        raise ProductError('GATEWAY_STOPPED', 'The gateway is not ready; no Git network request was made.', 'vpn-gateway start')
    env = dict(gateway.ENV, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never')
    if item['helper']:
        env['GIT_SSH_COMMAND'] = item['value'] + ' -o BatchMode=yes -o StrictHostKeyChecking=yes'
    result = gateway.run(['git', '-C', str(item['repo']), 'ls-remote', '--exit-code', item['remote'], 'HEAD'],
                         env=env, check=False, timeout=30)
    if result.returncode == 0:
        print('Remote read-only access: passed. No push or checkout was performed.')
        return 0
    reason = (result.stderr or '').lower()
    if any(word in reason for word in ('authentication', 'permission denied', 'could not read username', 'host key verification')):
        raise ProductError('GIT_AUTH_REQUIRED', 'Remote authentication or SSH host-key approval is required; proxy settings were preserved.',
                           'Authenticate locally with your normal Git workflow; never send credentials to support.')
    raise ProductError('GIT_REMOTE_UNVERIFIED', 'Read-only remote access could not be verified; private server output was withheld.')
