import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


wsl_backend = module('test_wsl_backend', ROOT/'scripts'/'wsl_backend.py')
gateway = module('test_wsl_gateway', ROOT/'scripts'/'gateway.py')
vpn = module('test_wsl_vpn', ROOT/'scripts'/'vpn.py')
if os.name == 'nt':
    with patch.dict(sys.modules, {'pwd':SimpleNamespace()}):
        manager_module = module('test_wsl_manager', ROOT/'scripts'/'wsl_manager.py')
else:
    manager_module = module('test_wsl_manager', ROOT/'scripts'/'wsl_manager.py')
installer = module('test_wsl_installer', ROOT/'install.py')
configlib = installer.config_module(ROOT)
CONFIG = configlib.load_config(ROOT/'gateway.example.toml')


class PureWslTests(unittest.TestCase):
    def test_slirp_targets_named_namespace_not_a_racing_keeper_pid(self):
        keeper = SimpleNamespace(pid=12345)
        slirp = SimpleNamespace(pid=12346, poll=lambda: None)
        with patch.object(manager_module, 'require_root'), \
             patch.object(manager_module, 'network_ready', side_effect=[False, True]), \
             patch.object(manager_module, 'network_stop'), \
             patch.object(manager_module, 'restore_resolver'), \
             patch.object(manager_module, 'private_write'), \
             patch.object(manager_module, 'run'), \
             patch.object(manager_module.subprocess, 'Popen', side_effect=[keeper, slirp]) as popen:
            manager_module.network_start()
        command = popen.call_args_list[1].args[0]
        self.assertEqual(command[0], '/usr/bin/slirp4netns')
        self.assertIn('--netns-type=path', command)
        self.assertEqual(command[-2:], [str(manager_module.NETNS_PATH), 'eth0'])
        self.assertNotIn(str(keeper.pid), command)

    def test_transport_comparison_rechecks_outer_path_before_next_vpn(self):
        with patch.object(gateway, 'backend_name', return_value='wsl'), \
             patch.object(gateway, 'stop_internal') as stop, \
             patch.object(gateway, 'preflight', side_effect=[{}, {}, gateway.GatewayError('outer unavailable')]) as preflight, \
             patch.object(gateway, 'prompt_credentials') as credentials, \
             patch.object(gateway, 'configuration', return_value={'transports':{'udp':{},'tcp':{}}}), \
             patch.object(gateway, 'start_transport', return_value=False) as start, \
             patch.object(gateway, 'wsl_status_data', return_value={}), \
             patch.object(gateway, 'sanitized_events', return_value={
                 'available': False, 'text': '', 'tail_lines': 200, 'scope': 'backend_history'}), \
             patch.object(gateway, 'private_write'):
            with self.assertRaisesRegex(gateway.GatewayError, 'outer unavailable'):
                gateway.compare_transports('wsl')
        self.assertEqual(preflight.call_count, 3)
        credentials.assert_called_once_with('wsl')
        start.assert_called_once_with('udp', backend='wsl')
        self.assertEqual(stop.call_args.kwargs, {'backend':'wsl'})

    def test_wsl_network_command_enters_only_the_managed_namespace(self):
        with patch.object(gateway, 'wsl') as invoke:
            gateway.wsl_network('/sbin/ip', 'link', 'show', 'tun0', check=False, timeout=15)
        invoke.assert_called_once_with('/usr/sbin/ip', 'netns', 'exec',
            'isolated-openvpn-gateway', '/sbin/ip', 'link', 'show', 'tun0',
            check=False, timeout=15)

    def test_validation_checks_tunnel_routes_and_firewall_inside_gateway_namespace(self):
        for missing in (False, True):
            with self.subTest(namespace_missing=missing), \
                 patch.object(gateway, 'backend_name', return_value='wsl'), \
                 patch.object(gateway, 'read_json', return_value={}), \
                 patch.object(gateway, 'network_snapshot', return_value={}), \
                 patch.object(gateway, 'wsl_status_data', return_value={
                     'ready':True, 'outer_interface':'eth0'}), \
                 patch.object(gateway, 'socks_available', return_value=True), \
                 patch.object(gateway, 'listener_result', return_value={
                     key:True for key in ('listener_present','loopback_only',
                         'ipv4_wildcard_absent','ipv6_wildcard_absent')}), \
                 patch.object(gateway, 'private_write'), \
                 patch('builtins.print'), \
                 patch.object(gateway, 'wsl') as invoke:
                invoke.side_effect = [
                    SimpleNamespace(returncode=int(missing), stdout='dev tun0'),
                    SimpleNamespace(returncode=int(missing), stdout=''),
                    SimpleNamespace(returncode=int(missing), stdout=''),
                    SimpleNamespace(returncode=1, stdout='')]
                result = gateway.validation(backend='wsl')
                self.assertEqual(invoke.call_count, 4)
                for call in invoke.call_args_list:
                    self.assertEqual(call.args[:4], ('/usr/sbin/ip','netns','exec',
                        'isolated-openvpn-gateway'))
                for key in ('proxy_route_tun0','tun0_exists','fail_closed_firewall_installed'):
                    self.assertEqual(result[key], not missing)
                if missing:
                    self.assertFalse(result['passed'])

    def test_socks_greeting_handles_fragmented_reply(self):
        with patch.object(gateway,'configuration',return_value=CONFIG), \
             patch.object(gateway.socket,'create_connection') as connect:
            stream = connect.return_value.__enter__.return_value
            stream.recv.side_effect = [b'\x05', b'\x00']
            self.assertTrue(gateway.socks_available())
            stream.settimeout.assert_called_once_with(10)

    def test_recorded_msix_root_works_outside_packaged_terminal_but_not_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base/'Packages'/'App'/'LocalCache'/'Local'/'IsolatedOpenVPNGateway'
            with patch.object(gateway.host,'IS_WINDOWS',True), \
                 patch.object(gateway.host,'install_root',return_value=base/'IsolatedOpenVPNGateway'), \
                 patch.object(gateway.host,'local_app_data',return_value=base):
                self.assertTrue(gateway.host.installation_root_matches(root,{'resolved_install_root':str(root)}))
                self.assertFalse(gateway.host.installation_root_matches(root,{}))
                outside = base.parent/'IsolatedOpenVPNGateway'
                self.assertFalse(gateway.host.installation_root_matches(outside,{'resolved_install_root':str(outside)}))

    def test_linux_stdin_is_binary_and_preserves_exact_lf(self):
        completed = SimpleNamespace(returncode=0, stdout=b'ok\n', stderr=b'')
        with patch.object(gateway.subprocess, 'run', return_value=completed) as run:
            result = gateway.wsl('/usr/bin/python3','-c','pass',input='#!/usr/bin/python3\n')
        self.assertIs(run.call_args.kwargs['text'],False)
        self.assertNotIn('encoding',run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs['input'],b'#!/usr/bin/python3\n')
        self.assertEqual(result.stdout,'ok\n')

    def test_dante_checksum_mismatch_prevents_extraction(self):
        dependencies = module('test_wsl_dependencies', ROOT/'scripts'/'wsl_dependencies.py')
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp)/'source.tar.gz'
            archive.write_bytes(b'untrusted download')
            with patch.object(dependencies.tarfile, 'open') as extract:
                with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                    dependencies.verified_extract(archive, Path(tmp))
                extract.assert_not_called()

    def test_packaged_windows_installation_uses_resolved_marker_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base/'IsolatedOpenVPNGateway'
            root.mkdir()
            marker = root/'installation.json'
            marker.write_text('{}')
            redirected = base/'package-cache'/'IsolatedOpenVPNGateway'
            original_resolve = Path.resolve
            def resolve(path, *args, **kwargs):
                return redirected/'installation.json' if path == marker else original_resolve(path,*args,**kwargs)
            with patch.object(gateway.host, 'local_app_data', return_value=base), \
                 patch.object(Path, 'resolve', resolve):
                self.assertEqual(gateway.host.install_root(system='Windows'), redirected)

    def test_localized_wsl_bytes_decode_without_corrupting_output(self):
        value = 'Версия WSL: 2.7.12.0\r\n'
        self.assertEqual(wsl_backend.parse_version(value.encode('utf-16-le')), (2,7,12,0))
        self.assertEqual(wsl_backend.normalize_output('Linux utf8'.encode()), 'Linux utf8')

    def test_windows_home_edition_detection(self):
        self.assertTrue(wsl_backend.edition_is_home('CoreSingleLanguage', 'Windows 11 Home'))
        self.assertTrue(wsl_backend.edition_is_home('Core', 'Windows 10'))
        self.assertFalse(wsl_backend.edition_is_home('Professional', 'Windows 11 Pro'))

    def test_backend_selection_is_explicit_and_docker_remains_default(self):
        with patch.object(gateway.host, 'IS_WINDOWS', False), \
             patch.object(gateway, 'preferences', return_value={}):
            self.assertEqual(gateway.backend_name(), 'docker')
            with self.assertRaises(gateway.GatewayError):
                gateway.backend_name('wsl')
        with patch.object(gateway.host, 'IS_WINDOWS', True):
            self.assertEqual(gateway.backend_name('wsl'), 'wsl')
        self.assertEqual(gateway.parse_cli(['start','tcp','--backend','wsl']),
                         ('start',['tcp'],'wsl'))

    def test_wsl_commands_preserve_paths_with_spaces_without_shell_quoting(self):
        command = wsl_backend.install_command(Path(r'C:\Users\Test User\Gateway\wsl'))
        self.assertEqual(command[-2:], [r'C:\Users\Test User\Gateway\wsl', '--no-launch'])
        self.assertNotIn('sh', command)
        run = wsl_backend.wsl_command('/usr/bin/install', '-D', '/mnt/c/Test User/a', '/opt/a')
        self.assertEqual(run[-2], '/mnt/c/Test User/a')
        self.assertEqual(run[:6], ['wsl.exe','--distribution','IsolatedOpenVPNGateway',
                                   '--user','root','--exec'])

    def test_safe_powershell_literal(self):
        self.assertEqual(wsl_backend.powershell_literal("C:\\O'Brien\\a"),
                         "'C:\\O''Brien\\a'")
        with self.assertRaises(ValueError):
            wsl_backend.powershell_literal('bad\ncommand')

    def test_wsl_nat_and_mirrored_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'.wslconfig'
            self.assertEqual(wsl_backend.networking_mode(path), 'nat')
            path.write_text('[wsl2]\nnetworkingMode=mirrored\n')
            self.assertEqual(wsl_backend.networking_mode(path), 'mirrored')
            path.write_text('[wsl2]\nnetworkingMode=unexpected\n')
            self.assertEqual(wsl_backend.networking_mode(path), 'unknown')

    def test_listener_validation_rejects_ipv4_and_ipv6_wildcards(self):
        good = [{'LocalAddress':'127.0.0.1','LocalPort':1080,'OwningProcess':12}]
        self.assertTrue(wsl_backend.listener_validation(good,1080)['loopback_only'])
        for address in ('0.0.0.0','::','192.0.2.20'):
            records = good + [{'LocalAddress':address,'LocalPort':1080,'OwningProcess':12}]
            self.assertFalse(wsl_backend.listener_validation(records,1080)['loopback_only'])

    def test_wsl_version_and_distro_parsing(self):
        self.assertTrue(wsl_backend.modern_install_supported('WSL version: 2.4.4.0'))
        self.assertFalse(wsl_backend.modern_install_supported('WSL version: 2.3.9.0'))
        listing = '  NAME                      STATE           VERSION\n* IsolatedOpenVPNGateway    Running         2\n'
        self.assertEqual(wsl_backend.distro_version(listing), 2)


class RequirementIsolationTests(unittest.TestCase):
    def test_docker_backend_rejects_windows_containers(self):
        completed = SimpleNamespace(returncode=0, stdout='windows\n', stderr='')
        with patch.object(gateway, 'docker', return_value=completed):
            with self.assertRaisesRegex(gateway.GatewayError, 'Linux containers'):
                gateway.engine()

    def test_wsl_backend_does_not_require_docker(self):
        fake_gateway = SimpleNamespace(DOCKER=Path('Z:/missing/docker.exe'), ENV={})
        fake_host = SimpleNamespace(wsl_executable=lambda: 'wsl.exe',
                                    powershell_executable=lambda: 'powershell.exe',
                                    browser_candidates=lambda: ())
        completed = SimpleNamespace(returncode=0, stdout='ok', stderr='')
        with patch('platform.system', return_value='Windows'), \
             patch('platform.machine', return_value='AMD64'), \
             patch.object(installer.subprocess, 'run', return_value=completed) as run, \
             patch.object(sys, 'version_info', (3,11,0)):
            installer.requirements(fake_gateway, fake_host, 'wsl')
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(any(command[:2] == ['wsl.exe','--version'] for command in commands))
        self.assertFalse(any('docker' in str(command).lower() for command in commands))

    def test_docker_backend_does_not_require_wsl(self):
        with tempfile.TemporaryDirectory() as tmp:
            docker = Path(tmp)/'docker.exe'; docker.write_bytes(b'fixture')
            fake_gateway = SimpleNamespace(DOCKER=docker, ENV={})
            fake_host = SimpleNamespace(wsl_executable=lambda: 'missing-wsl.exe',
                                        powershell_executable=lambda: 'powershell.exe',
                                        browser_candidates=lambda: ())
            completed = SimpleNamespace(returncode=0, stdout='linux', stderr='')
            with patch('platform.system', return_value='Windows'), \
                 patch('platform.machine', return_value='AMD64'), \
                 patch.object(installer.subprocess, 'run', return_value=completed) as run, \
                 patch.object(sys, 'version_info', (3,11,0)):
                installer.requirements(fake_gateway, fake_host, 'docker')
        self.assertFalse(any('wsl' in str(call.args[0]).lower() for call in run.call_args_list))


class WslGatewayTests(unittest.TestCase):
    def failure_probe(self, diagnostic):
        stopped = {'network_namespace':True, 'openvpn_running':False, 'tun_exists':False}
        with patch.object(gateway.host, 'IS_WINDOWS', True), \
             patch.object(gateway, 'ready', return_value=True), \
             patch.object(gateway, 'wsl_status_data', side_effect=({'dns':[]},stopped)), \
             patch.object(gateway, 'public_ip', side_effect=('203.0.113.1',None,'203.0.113.1')), \
             patch.object(gateway.time, 'sleep'), \
             patch.object(gateway, 'wsl_manager', side_effect=(SimpleNamespace(returncode=0),diagnostic)), \
             patch.object(gateway, 'restore_wsl_after_failure_test', return_value=True) as restore, \
             contextlib.redirect_stdout(io.StringIO()):
            result = gateway.failure_test('wsl')
        restore.assert_called_once()
        return result

    def test_negative_probe_error_is_not_proof_of_firewall_blocking(self):
        failed = SimpleNamespace(returncode=1, stdout='')
        self.assertFalse(self.failure_probe(failed)['firewall_forced_outer_blocked'])
        partial = SimpleNamespace(returncode=0, stdout=json.dumps({'proxy_outer_blocked':True}))
        self.assertFalse(self.failure_probe(partial)['firewall_forced_outer_blocked'])

    def test_negative_probe_requires_control_and_observed_firewall_reject(self):
        evidence = {key:True for key in ('outer_control_succeeded','proxy_outer_blocked',
                                        'firewall_reject_observed','probe_route_removed')}
        for missing in (None, 'outer_control_succeeded', 'firewall_reject_observed', 'probe_route_removed'):
            current = dict(evidence)
            if missing: current[missing] = False
            result = self.failure_probe(SimpleNamespace(returncode=0, stdout=json.dumps(current)))
            self.assertEqual(result['firewall_forced_outer_blocked'], missing is None)
            self.assertTrue(result['state_restored'])

    def test_negative_probe_restores_tunnel_after_diagnostic_exception(self):
        with patch.object(gateway.host, 'IS_WINDOWS', True), \
             patch.object(gateway, 'ready', return_value=True), \
             patch.object(gateway, 'wsl_status_data', return_value={'dns':[]}), \
             patch.object(gateway, 'public_ip', return_value='203.0.113.1'), \
             patch.object(gateway, 'wsl_manager', side_effect=gateway.GatewayError('diagnostic failed')), \
             patch.object(gateway, 'restore_wsl_after_failure_test', return_value=True) as restore:
            with self.assertRaisesRegex(gateway.GatewayError, 'diagnostic failed'):
                gateway.failure_test('wsl')
        restore.assert_called_once()

    def test_incomplete_owned_distro_can_resume_without_reinstalling(self):
        with patch.object(gateway.host, 'IS_WINDOWS', True), \
             patch.object(gateway, 'wsl_version', return_value='WSL version: 2.7.12'), \
             patch.object(gateway, 'wsl_names', return_value=[wsl_backend.DISTRO]), \
             patch.object(gateway, 'wsl_owned', return_value=True), \
             patch.object(gateway, 'read_json', return_value={'wsl_install_phase':'awaiting-network'}), \
             patch.object(gateway, 'provision_wsl_backend') as provision, \
             patch.object(gateway, 'run') as run:
            gateway.install_backend('wsl')
        provision.assert_called_once_with(gateway.ROOT/'wsl')
        run.assert_not_called()

    def test_linux_assets_have_lf_shebang_and_stdin_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'hook with spaces.py'
            source.write_bytes(b'#!/usr/bin/python3\r\nprint("ok")\r\n')
            with patch.object(gateway, 'wsl') as invoke:
                gateway.copy_wsl_asset(source, '/opt/project/hook.py')
        args, kwargs = invoke.call_args
        self.assertEqual(kwargs['input'], '#!/usr/bin/python3\nprint("ok")\n')
        self.assertEqual(args[-2:], ('/opt/project/hook.py','0555'))
        self.assertNotIn(str(source), args)

    def test_comparison_stop_preserves_wsl_auth(self):
        with patch.object(gateway.host, 'IS_WINDOWS', True), \
             patch.object(gateway, 'stop_forwarder'), \
             patch.object(gateway, 'wsl_names', return_value=[wsl_backend.DISTRO]), \
             patch.object(gateway, 'wsl_owned', return_value=True), \
             patch.object(gateway, 'wsl', return_value=SimpleNamespace(returncode=0)), \
             patch.object(gateway, 'cleanup_auth') as cleanup, \
             patch.object(gateway, 'wsl_manager', return_value=SimpleNamespace(returncode=0)) as manager:
            gateway.stop_internal(keep_auth=True, backend='wsl')
        cleanup.assert_not_called()
        manager.assert_called_once_with('stop-keep-auth', check=False, timeout=35)

    def test_public_ip_timeout_is_unavailable_not_an_unhandled_exception(self):
        with patch.object(gateway, 'configuration', return_value=CONFIG), \
             patch.object(gateway, 'run', side_effect=gateway.subprocess.TimeoutExpired('curl', 20)):
            self.assertIsNone(gateway.public_ip())

    def test_outer_vpn_egress_mismatch_blocks_before_credentials(self):
        listing = SimpleNamespace(returncode=0,
                                  stdout='IsolatedOpenVPNGateway Running 2\n', stderr='')
        ok = SimpleNamespace(returncode=0, stdout='', stderr='')
        mismatch = SimpleNamespace(returncode=0, stdout='198.51.100.20', stderr='')
        snapshot = {'public_ip':'203.0.113.10','diagnostics_ok':True,
                    'adapters':{'stdout':'[{"Name":"Outer VPN","ifIndex":9,"Status":"Up"}]'},
                    'public_route':{'stdout':'[{"InterfaceIndex":9}]'}}
        cfg = dict(CONFIG); cfg['windows_outer_adapter_contains']=['Outer VPN']
        with patch.object(gateway.host, 'IS_WINDOWS', True), \
             patch.object(gateway, 'wsl_names', return_value=[wsl_backend.DISTRO]), \
             patch.object(gateway, 'wsl_owned', return_value=True), \
             patch.object(gateway, 'run', return_value=listing), \
             patch.object(gateway, 'wsl', side_effect=(ok,mismatch,ok)), \
             patch.object(gateway, 'network_snapshot', return_value=snapshot), \
             patch.object(gateway, 'configuration', return_value=cfg), \
             patch.object(gateway, 'private_write'):
            with self.assertRaisesRegex(gateway.GatewayError, 'Windows/WSL egress differs'):
                gateway.wsl_preflight()

    def test_credential_cleanup_removes_host_and_wsl_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp)/'auth'; auth.write_text('secret')
            with patch.object(gateway, 'AUTH', auth), \
                 patch.object(gateway, 'wsl_names', return_value=[wsl_backend.DISTRO]), \
                 patch.object(gateway, 'wsl_owned', return_value=True), \
                 patch.object(gateway, 'wsl') as invoke:
                gateway.cleanup_auth('wsl')
            self.assertFalse(auth.exists())
            invoke.assert_called_once_with('/usr/bin/rm','-f','/run/isolated-openvpn-gateway/auth',
                                           check=False, timeout=20)

    def test_credential_cleanup_does_not_modify_an_unowned_distro(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(gateway, 'AUTH', Path(tmp)/'absent'), \
                 patch.object(gateway, 'wsl_names', return_value=[wsl_backend.DISTRO]), \
                 patch.object(gateway, 'wsl_owned', return_value=False), \
                 patch.object(gateway, 'wsl') as invoke:
                with self.assertRaisesRegex(gateway.GatewayError, 'unowned'):
                    gateway.cleanup_auth('wsl')
        invoke.assert_not_called()

    def test_credentials_use_stdin_not_arguments_or_environment(self):
        completed = SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(gateway, 'wsl_names', return_value=[]), \
             patch.object(gateway.sys.stdin, 'isatty', return_value=True), \
             patch.object(gateway.getpass, 'getpass', side_effect=['alice','secret-value']), \
             patch.object(gateway, 'wsl_manager', return_value=completed) as manager:
            gateway.prompt_credentials('wsl')
        args, kwargs = manager.call_args
        self.assertEqual(args, ('put-auth',))
        self.assertEqual(kwargs['input'], 'alice\nsecret-value\n')
        self.assertNotIn('secret-value', repr(args))
        self.assertNotIn('env', kwargs)

    def test_uninstall_refuses_unowned_distro(self):
        with patch.object(gateway, 'read_json', return_value={
                'project':'isolated-openvpn-gateway','wsl_distro':wsl_backend.DISTRO,
                'wsl_created_by_project':False}), \
             patch.object(gateway, 'run') as run:
            with self.assertRaisesRegex(gateway.GatewayError, 'does not own'):
                gateway.uninstall_wsl_backend(confirm=False)
        run.assert_not_called()


class WslFirewallTests(unittest.TestCase):
    def setUp(self):
        resolver = patch.object(manager_module.socket, 'getaddrinfo',
            return_value=[(2,1,6,'',('1.1.1.1',443))])
        resolver.start()
        self.addCleanup(resolver.stop)

    def test_namespace_resolver_does_not_replace_outer_wsl_dns(self):
        with tempfile.TemporaryDirectory() as tmp:
            etc = Path(tmp)
            (etc/'resolv.outer').write_bytes(b'nameserver 10.0.2.3\n')
            with patch.object(manager_module, 'ETC', etc), \
                 patch.object(manager_module, 'private_write') as write:
                manager_module.restore_resolver('outer')
            self.assertEqual(write.call_args.args[0], manager_module.INNER_RESOLV)
            self.assertNotEqual(write.call_args.args[0], Path('/etc/resolv.conf'))
        self.assertIn('BindPaths=/etc/netns/', manager_module.VPN_UNIT)
        self.assertIn('BindReadOnlyPaths=/etc/netns/', manager_module.SOCKS_UNIT)

    def test_firewall_probe_restores_narrow_route_even_when_curl_raises(self):
        commands = []
        def inside(args, **kwargs):
            commands.append(args)
            if args[0] == '/usr/sbin/runuser':
                raise OSError('diagnostic failed')
            return SimpleNamespace(returncode=0, stdout='')
        with patch.object(manager_module, 'network_ready', return_value=True), \
             patch.object(manager_module, 'inside', side_effect=inside), \
             patch.object(manager_module, 'firewall_reject_count', return_value=7):
            with self.assertRaises(OSError):
                manager_module.proxy_outer_test()
        added = next(args for args in commands if args[:3] == ['/usr/sbin/ip','rule','add'])
        deleted = next(args for args in commands if args[:3] == ['/usr/sbin/ip','rule','del'])
        self.assertEqual(added[3:], deleted[3:])
        self.assertIn('1.1.1.1/32', added)
        self.assertIn('10000-10000', added)

    def test_firewall_probe_requires_independent_positive_control_and_counter(self):
        for outer_ok, counters, expected in ((True,[7,8],True),(True,[7,7],False),(False,[],False)):
            commands = []
            def inside(args, **kwargs):
                commands.append(args)
                code = 7 if args[0] == '/usr/sbin/runuser' or (
                    args[0] == '/usr/bin/curl' and not outer_ok) else 0
                return SimpleNamespace(returncode=code, stdout='')
            output = io.StringIO()
            with patch.object(manager_module, 'network_ready', return_value=True), \
                 patch.object(manager_module, 'inside', side_effect=inside), \
                 patch.object(manager_module, 'firewall_reject_count', side_effect=counters), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(manager_module.proxy_outer_test(),0)
            self.assertEqual(all(json.loads(output.getvalue()).values()), expected)
            if not outer_ok:
                self.assertFalse(any(args[0] == '/usr/sbin/ip' for args in commands))

    def test_wsl_hooks_receive_paths_after_openvpn_strips_environment(self):
        with patch.object(vpn, 'BACKEND', 'wsl'):
            options = vpn.hook_environment_options()
        triples = [options[index:index+3] for index in range(0, len(options), 3)]
        values = {item[1]:item[2] for item in triples}
        self.assertTrue(all(item[0] == '--setenv' for item in triples))
        self.assertEqual(values['GATEWAY_BACKEND'], 'wsl')
        self.assertEqual(values['GATEWAY_STATE'], str(vpn.STATE))
        self.assertEqual(values['GATEWAY_AUTH'], str(vpn.AUTH))
        with patch.object(vpn, 'BACKEND', 'docker'):
            self.assertEqual(vpn.hook_environment_options(), [])

    def test_wsl_firewall_is_idempotent_uid_scoped_and_has_no_global_policy(self):
        calls = []

        def command(*args, check=True):
            calls.append(args)
            if args == ('ip','-j','-4','route','show','default'):
                return SimpleNamespace(returncode=0, stdout=json.dumps([
                    {'dev':'eth0','gateway':'172.20.0.1'}]), stderr='')
            deleting = len(args) > 2 and args[1] in ('rule','-D') and 'del' in args
            if args[:3] == ('ip','rule','del') or (len(args) > 1 and args[1] == '-D'):
                return SimpleNamespace(returncode=1, stdout='', stderr='')
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        with patch.object(vpn, 'command', side_effect=command), \
             patch.object(vpn, 'read_state', return_value={}), \
             patch.object(vpn, 'write_state'), patch.object(vpn, 'BACKEND', 'wsl'):
            vpn.wsl_firewall('192.0.2.40', 1194, 'udp')
        rendered = [' '.join(call) for call in calls]
        self.assertTrue(any('uidrange 10000-10000 lookup 100' in line for line in rendered))
        self.assertTrue(any('owner --uid-owner 10000 -j IOVG_WSL_PROXY' in line for line in rendered))
        self.assertTrue(any('IOVG_WSL_PROXY -o tun0 -j ACCEPT' in line for line in rendered))
        self.assertTrue(any('IOVG_WSL_PROXY -j REJECT' in line for line in rendered))
        self.assertFalse(any(' -P ' in (' '+line+' ') for line in rendered))

    def test_wsl_resolver_is_distro_local_and_uses_only_pushed_dns(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolv = Path(tmp)/'resolv.conf'
            with patch.object(vpn, 'RESOLV', resolv):
                vpn.resolver(['10.20.30.40'], ['corp.example'])
            self.assertEqual(resolv.read_text(),
                             'nameserver 10.20.30.40\nsearch corp.example\noptions timeout:2 attempts:2\n')

    def test_acceptance_script_is_read_only_at_host_layer(self):
        text = (ROOT/'tools'/'windows_acceptance.ps1').read_text()
        for forbidden in ('Set-Net', 'New-Net', 'netsh', 'Set-Dns', 'New-NetRoute',
                          'Remove-NetRoute', 'Set-NetFirewall'):
            self.assertNotIn(forbidden, text)
        self.assertIn('compare-transports --backend', text)

    def test_gateway_firewall_runs_in_nested_network_namespace(self):
        manager = (ROOT/'scripts'/'wsl_manager.py').read_text()
        gateway_source = (ROOT/'scripts'/'gateway.py').read_text()
        self.assertIn("NETNS = 'isolated-openvpn-gateway'", manager)
        self.assertIn('NetworkNamespacePath=/run/netns/isolated-openvpn-gateway', manager)
        self.assertIn("'/usr/bin/slirp4netns'", manager)
        self.assertIn("'--disable-host-loopback'", manager)
        self.assertIn("'netns', 'exec', NETNS", manager)
        self.assertIn("elif action == 'proxy-outer-test': return proxy_outer_test()", manager)
        self.assertIn("forced=wsl_manager('proxy-outer-test'", gateway_source)


if __name__ == '__main__':
    unittest.main()
