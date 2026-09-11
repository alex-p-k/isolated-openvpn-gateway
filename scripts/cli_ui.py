"""Read-only reporting and additive command routing around the lifecycle core."""
import argparse
import json
import platform
import subprocess
import time

from product import ProductError, error_text, platform_instruction


def record(gateway, state, code=None):
    gateway.private_write(gateway.RUNTIME/'cli-state.json', json.dumps({
        'schema_version': 1, 'state': state, 'code': code, 'updated_at': time.time()}))


def report(gateway, explicit=None, diagnose=False):
    result = dict(schema_version=1, state='not_configured', backend=None, checks=[],
                  next_action='Run .\\setup.cmd from the source checkout.')
    def check(name, state, message):
        result['checks'].append(dict(id=name, state=state, message=message))
    marker = gateway.read_json(gateway.ROOT/'installation.json')
    if marker.get('project') != gateway.PROJECT or not gateway.host.installation_root_matches(gateway.ROOT, marker):
        check('installation', 'fail', 'No owned installation at this entry point.')
        return result
    check('installation', 'pass', 'Owned per-user installation.')
    if (gateway.ROOT/'update-pending.json').exists():
        check('update', 'fail', 'An interrupted application update must be recovered from the verified checkout.')
        result.update(state='blocked', next_action='Run setup.cmd (Windows) or python3 install.py --wizard (macOS) from the verified checkout.')
        return result
    try:
        cfg = gateway.configuration()
        backend = gateway.active_backend(explicit)
    except (ValueError, gateway.GatewayError):
        check('configuration', 'fail', 'Configuration or saved backend is invalid.')
        result.update(state='error', next_action='vpn-gateway setup')
        return result
    result['backend'] = backend
    check('configuration', 'pass', 'Deployment configuration accepted.')
    if backend == 'wsl':
        check('networking_mode', 'info', gateway.wsl_backend.networking_mode(gateway.Path.home()/'.wslconfig'))
    live = gateway.connection_state(backend)
    data, available = live['data'], live['available']
    data = dict(data, tun_exists=live['tun_exists'])
    check('backend', 'pass' if available else 'fail', 'Selected backend available.' if available else 'Selected backend unavailable or not owned.')
    socks, listener, connected = live['socks'], live['loopback_only'], live['ready']
    check('listener', 'pass' if listener is True else 'fail' if listener is False else 'not_checked',
          'Only IPv4 loopback is listening.' if listener is True else
          'Listener is absent or not restricted to IPv4 loopback.' if listener is False else 'Listener inspection unavailable.')
    check('health', 'pass' if live['healthy'] else 'not_checked',
          'Live tunnel health verified.' if live['healthy'] else 'Live tunnel health not confirmed.')
    check('tunnel', 'pass' if data.get('tun_exists') else 'not_checked', 'tun0 present.' if data.get('tun_exists') else 'No live tun0 confirmed.')
    check('socks', 'pass' if socks and listener else 'not_checked', 'Loopback SOCKS handshake passed.' if socks and listener else 'Loopback SOCKS is not ready.')
    check('corporate_dns', 'pass' if data.get('dns_pushed') else 'not_checked',
          'Server pushed DNS; hostname reachability is a separate test.' if data.get('dns_pushed') else 'No pushed corporate DNS confirmed; no address was invented.')
    check('corporate_resource', 'not_checked', 'Use vpn-gateway test URL or git check PATH --remote for actual access.')
    state = gateway.read_json(gateway.RUNTIME/'cli-state.json')
    connecting = data.get('openvpn_running') and not data.get('initialization_completed')
    if state.get('state') == 'connecting' and gateway.host.lifecycle_busy(gateway.ROOT/'.lock'):
        connecting = True
    result['state'] = ('ready' if connected else 'error' if not available or data.get('hook_error') or (data.get('ready') and not connected) else
                       'connecting' if connecting else
                       'blocked' if state.get('state') == 'blocked' else 'error' if state.get('state') == 'error' else 'stopped')
    result['next_action'] = {'ready': 'vpn-gateway browser', 'stopped': 'vpn-gateway start',
                             'connecting': 'vpn-gateway logs', 'blocked': 'Connect the configured outer VPN; vpn-gateway doctor',
                             'error': 'vpn-gateway doctor; vpn-gateway setup for installation recovery'}[result['state']]
    result['details'] = dict(initialization_completed=bool(data.get('initialization_completed')),
                             tun_exists=bool(data.get('tun_exists')), socks_handshake=socks,
                             loopback_only=listener, pushed_dns=bool(data.get('dns_pushed')),
                             service_manager=('systemd' if data.get('systemd') else 'fallback') if backend == 'wsl' else 'containers')
    selected_transport = gateway.RUNTIME/'selected-transport'
    try:
        transport = selected_transport.read_text().strip()
        if transport in cfg.get('transports', {}):
            result['details']['transport'] = transport
    except OSError:
        pass
    connected_at = data.get('connected_at')
    if connected and isinstance(connected_at, (int, float)):
        result['details']['connected_seconds'] = max(0, int(time.time() - connected_at))
    if diagnose:
        check('platform', 'info', platform.system() + ' ' + platform.release() + ' ' + platform.machine())
        try:
            snapshot = gateway.network_snapshot()
            gateway.verify_outer_host(snapshot, cfg)
            outer = bool(snapshot.get('public_ip'))
            check('outer_host', 'pass' if outer else 'fail', 'Host outer route and public egress available.' if outer else 'Public egress unavailable.')
        except (OSError, ValueError, subprocess.TimeoutExpired, gateway.GatewayError):
            check('outer_host', 'fail', 'Required host outer route/egress could not be verified; no settings changed.')
        if any(row['id'] == 'outer_host' and row['state'] == 'fail' for row in result['checks']):
            result.update(state='blocked', next_action='Restore the configured outer VPN, then run vpn-gateway doctor. This diagnostic did not stop the gateway.')
        check('backend_egress', 'not_checked', 'Rechecked by start before credentials. Doctor never tears down a session or creates a network namespace.')
        check('fail_closed', 'not_checked', 'No disruptive negative test was performed by doctor.')
        check('browser_acceptance', 'not_checked', 'Browser settings alone do not prove DNS/fail-closed; see UX_ACCEPTANCE.md.')
    return result


def render(result, *, structured=False, verbose=False):
    result = dict(result, next_action=platform_instruction(result['next_action']))
    if structured:
        print(json.dumps(result, ensure_ascii=True))
    else:
        print('Gateway: ' + result['state'].replace('_', ' ') + (' | backend: ' + result['backend'] if result['backend'] else ''))
        for row in result['checks']:
            if verbose or row['state'] == 'fail' or row['id'] in ('corporate_dns', 'corporate_resource'):
                print(f'[{row["state"].upper()}] {row["id"]}: {row["message"]}')
        if verbose and result.get('details'):
            for key, value in result['details'].items():
                print(f'  {key}: {value}')
        print('Next: ' + result['next_action'])


def dispatch(gateway, argv):
    # Preserve the legacy parser and positional transport syntax. Route only
    # new commands and read-only reports before any runtime directory/lock write.
    if argv in (['--version'], ['version']):
        print('vpn-gateway ' + (gateway.ROOT/'VERSION').read_text().strip())
        return True, 0
    action = argv[0] if argv else 'status'
    if action in ('status', 'doctor'):
        parser = argparse.ArgumentParser(prog='vpn-gateway ' + action)
        parser.add_argument('--backend', choices=('wsl', 'docker'))
        parser.add_argument('--json', action='store_true')
        parser.add_argument('--verbose', action='store_true')
        args = parser.parse_args(argv[1:])
        result = report(gateway, args.backend, action == 'doctor')
        render(result, structured=args.json, verbose=args.verbose or action == 'doctor')
        failed = result['state'] in ('not_configured', 'error', 'blocked') or any(row['state'] == 'fail' for row in result['checks'])
        return True, 1 if failed else 0
    if action == 'setup':
        import setup_wizard
        return True, setup_wizard.main(argv[1:], package=gateway.ROOT)
    if action == 'browser':
        import browser
        return True, browser.main(argv[1:])
    if action == 'git':
        parser = argparse.ArgumentParser(prog='vpn-gateway git')
        parser.add_argument('operation', choices=('configure', 'check'))
        parser.add_argument('path')
        parser.add_argument('--remote', action='store_true', help='check only: read-only network access')
        parser.add_argument('--remote-name', help='repository remote name; auto-select a sole remote')
        args = parser.parse_args(argv[1:])
        marker = gateway.read_json(gateway.ROOT/'installation.json')
        if marker.get('project') != gateway.PROJECT or not gateway.host.installation_root_matches(gateway.ROOT, marker):
            raise ProductError('NOT_INSTALLED', 'Git integration requires an owned installation.', 'Run .\\setup.cmd first.')
        import git_integration
        if args.operation == 'configure':
            if args.remote:
                parser.error('--remote is available only for git check')
            with gateway.host.lifecycle_lock(gateway.ROOT/'.lock'):
                return True, git_integration.configure(gateway, args.path, args.remote_name)
        return True, git_integration.check(gateway, args.path, args.remote_name, args.remote)
    return False, None
