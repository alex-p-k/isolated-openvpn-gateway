"""Offline product tests. No installed distro/browser/profile is modified."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import browser
import cli_ui
import gateway
import git_integration
import host
import path_integration
import product
import profile_import
import setup_wizard

PROFILE = '''client
dev tun
remote vpn.example.invalid 1194 udp4
auth-user-pass
remote-cert-tls server
auth SHA256
<ca>
SYNTHETIC-CA-NOT-A-CERTIFICATE
</ca>
'''


class ProfileTests(unittest.TestCase):
    def test_remote_protocol_forms_and_original_preservation(self):
        for source in (PROFILE, PROFILE.replace(' 1194 udp4', ' 1194') + 'proto udp4\n', '\ufeff'+PROFILE):
            with self.subTest(source=source[:6]):
                normalized, metadata = profile_import.normalize(source)
                self.assertIn('remote vpn.example.invalid 1194 udp4', normalized)
                self.assertEqual(metadata['remote_protocol'], 'udp4')
                self.assertIn('dev tun0', gateway.derive_config(source, metadata))

    def test_ambiguous_inputs_refused_without_private_values(self):
        for suffix in ('remote PRIVATE-ENDPOINT 1194 udp4\n', 'proto tcp-client\n', 'proto udp4\nproto udp4\n'):
            with self.assertRaises(product.ProductError) as caught:
                profile_import.normalize(PROFILE+suffix)
            self.assertNotIn('PRIVATE-ENDPOINT', str(caught.exception))

    def test_unknown_scripts_are_not_imported(self):
        with self.assertRaises(gateway.GatewayError) as caught:
            gateway.derive_config(PROFILE+'up PRIVATE-COMMAND\n')
        self.assertIn('up', str(caught.exception))
        self.assertNotIn('PRIVATE-COMMAND', str(caught.exception))

    def test_endpoint_no_protocol_is_rejected(self):
        with self.assertRaises(product.ProductError):
            profile_import.normalize(PROFILE.replace(' 1194 udp4', ' 1194'))

    def test_toml_roundtrip_unicode_and_quotes(self):
        import tomllib
        _, metadata = profile_import.normalize(PROFILE)
        value = profile_import.deployment(metadata, ['VPN "adapter" \\ тест'])
        parsed = tomllib.loads(value.decode())
        self.assertEqual(parsed['gateway']['windows_outer_adapter_contains'], ['VPN "adapter" \\ тест'])
        self.assertNotIn('SYNTHETIC-CA', value.decode())


class PrimitiveTests(unittest.TestCase):
    def test_non_tty_does_not_call_input(self):
        with patch.object(sys.stdin, 'isatty', return_value=False), patch('builtins.input') as prompt:
            with self.assertRaises(product.ProductError): product.ask('Question')
        prompt.assert_not_called()

    def test_exception_output_never_dumps_subprocess_arguments(self):
        secret = 'PRIVATE-PASSWORD-ENDPOINT'
        for error in (OSError(secret), subprocess.TimeoutExpired([secret], 5, output=secret), Exception(secret)):
            rendered = product.error_text(error)
            self.assertNotIn(secret, rendered)
            self.assertIn('Next:', rendered)

    def test_path_add_idempotent_and_rollback_preserves_later_edits(self):
        entry = r'C:\Users\Test User\bin'
        before = r'C:\Tools;C:\Other'
        after, added = path_integration.add_entry(before, entry)
        self.assertTrue(added)
        self.assertEqual(path_integration.add_entry(after, entry.upper()), (after, False))
        self.assertEqual(path_integration.remove_entry(after+';C:\\New', entry), before+';C:\\New')

    def test_path_duplicate_refuses_ambiguous_removal(self):
        with self.assertRaises(product.ProductError): path_integration.remove_entry('A;B;A', 'A')

    def test_safe_windows_launcher_with_spaces_and_unsafe_paths(self):
        text = host.batch_launcher(Path('C:/Python Space/python.exe'), 'gateway.py').decode()
        self.assertIn('"C:', text)
        for path in ('C:/percent%/python.exe', 'C:/bang!/python.exe', 'C:/quote"/python.exe'):
            with self.assertRaises(OSError): host.ensure_windows_command_paths(path)

    def test_bootstrap_does_not_search_private_app_runtimes(self):
        text = (ROOT/'setup.cmd').read_text()
        self.assertIn('where py.exe', text)
        self.assertIn('where python.exe', text)
        self.assertNotIn('Codex', text)
        self.assertNotIn('AppData', text)


class ReportTests(unittest.TestCase):
    def api(self, ready=True):
        cfg = {'windows_outer_adapter_contains': ['Outer VPN']}
        data = dict(ready=ready, tun_exists=ready, dns_pushed=ready, initialization_completed=ready)
        fake = SimpleNamespace(ROOT=ROOT, RUNTIME=ROOT/'runtime', STATE=ROOT/'runtime'/'state', PROJECT=gateway.PROJECT,
                               WSL_DISTRO='Managed', Path=Path, GatewayError=gateway.GatewayError,
                               host=SimpleNamespace(IS_WINDOWS=True, installation_root_matches=lambda *_: True),
                               configuration=lambda: cfg, active_backend=lambda _: 'wsl', wsl_names=lambda: ['Managed'],
                               wsl_owned=lambda: True, wsl_status_data=lambda: data, socks_available=lambda: ready,
                               listener_result=lambda: {'loopback_only': ready},
                               wsl_backend=SimpleNamespace(networking_mode=lambda _: 'mirrored'),
                               network_snapshot=lambda: {'public_ip': '203.0.113.1'}, verify_outer_host=lambda *_: None)
        fake.read_json = lambda path: {'project': gateway.PROJECT} if path.name == 'installation.json' else {}
        return fake

    def test_ready_is_not_corporate_reachability_or_fail_closed_proof(self):
        data = cli_ui.report(self.api(), diagnose=True)
        self.assertEqual(data['state'], 'ready')
        self.assertEqual({r['id']: r['state'] for r in data['checks']}['fail_closed'], 'not_checked')
        out = io.StringIO()
        with contextlib.redirect_stdout(out): cli_ui.render(data, structured=True)
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed['schema_version'], 1)
        self.assertNotIn('203.0.113.1', out.getvalue())

    def test_stopped_and_missing_backend(self):
        fake = self.api(False)
        self.assertEqual(cli_ui.report(fake)['state'], 'stopped')
        fake.wsl_names = lambda: []
        self.assertEqual(cli_ui.report(fake)['state'], 'error')

    def test_report_never_mutates_or_calls_preflight(self):
        fake = self.api()
        fake.preflight = Mock(side_effect=AssertionError('must not preflight'))
        fake.stop_internal = Mock(side_effect=AssertionError('must not stop'))
        fake.private_write = Mock(side_effect=AssertionError('must not write'))
        cli_ui.report(fake, diagnose=True)
        fake.preflight.assert_not_called()
        fake.stop_internal.assert_not_called()
        fake.private_write.assert_not_called()

    def test_doctor_outer_failure_does_not_claim_ready(self):
        fake = self.api()
        fake.verify_outer_host = Mock(side_effect=gateway.GatewayError('outer route unavailable'))
        self.assertEqual(cli_ui.report(fake, diagnose=True)['state'], 'blocked')

    def test_docker_does_not_call_wsl(self):
        fake = self.api()
        fake.active_backend = lambda _: 'docker'
        fake.docker = lambda *_a, **_k: SimpleNamespace(returncode=0, stdout='linux')
        fake.container_id = lambda _: None
        fake.wsl_names = Mock(side_effect=AssertionError('WSL must not be called'))
        self.assertEqual(cli_ui.report(fake)['backend'], 'docker')
        fake.wsl_names.assert_not_called()

    def test_noninstalled_status_is_actionable(self):
        fake = self.api()
        fake.read_json = lambda _: {}
        self.assertEqual(cli_ui.report(fake)['state'], 'not_configured')


class BrowserTests(unittest.TestCase):
    def test_posix_firefox_uses_record_lock_not_stale_file_existence(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(host, 'IS_WINDOWS', False):
            profile = Path(temp)
            (profile/'.parentlock').touch()
            fcntl = SimpleNamespace(LOCK_SH=1, LOCK_NB=2, LOCK_UN=4, lockf=Mock())
            with patch.dict(sys.modules, {'fcntl': fcntl}):
                self.assertFalse(browser.firefox_busy(profile))
                self.assertEqual(fcntl.lockf.call_count, 2)
                fcntl.lockf.side_effect = BlockingIOError()
                self.assertTrue(browser.firefox_busy(profile))
            self.assertTrue((profile/'.parentlock').is_file())

    def test_firefox_prefs_and_chrome_flags_keep_protection(self):
        cfg = {'socks_host':'127.0.0.1', 'socks_port':1080}
        prefs = browser.firefox_preferences(cfg)
        self.assertTrue(prefs['network.proxy.socks5_remote_dns'])
        self.assertFalse(prefs['network.proxy.failover_direct'])
        self.assertFalse(prefs['network.proxy.allow_bypass'])
        self.assertFalse(prefs['media.peerconnection.enabled'])
        self.assertEqual(prefs['network.proxy.no_proxies_on'], '')
        flags = browser.browser_flags(cfg)
        self.assertTrue(any(x.startswith('--host-resolver-rules=') for x in flags))
        self.assertNotIn('direct://', ' '.join(flags))
        self.assertNotIn('--test-type', flags)

    def test_urls_refuse_flags_and_credentials(self):
        for value in ('--no-proxy-server', 'file:///secret', 'https://user:secret@example.test/', 'https://test/\nflag'):
            with self.assertRaises(product.ProductError): browser.validate_url(value)
        with self.assertRaises(product.ProductError): browser.saved_url('https://test/?token=secret')

    def test_firefox_launch_uses_owned_separate_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'gateway'; root.mkdir()
            base = Path(temp)/'corporate-browser'
            exe = Path(temp)/'firefox.exe'; exe.touch()
            (root/'installation.json').write_text(json.dumps({'project':gateway.PROJECT}))
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(browser, 'ROOT', root))
                stack.enter_context(patch.object(browser, 'PROFILE', base))
                stack.enter_context(patch.object(browser, 'gateway_ready', return_value=True))
                stack.enter_context(patch.object(browser, 'load_config', return_value={'socks_host':'127.0.0.1','socks_port':1080}))
                stack.enter_context(patch.object(host, 'installation_root_matches', return_value=True))
                stack.enter_context(patch.object(host, 'firefox_candidates', return_value=(exe,)))
                stack.enter_context(patch.object(host, 'secure_windows_acl'))
                stack.enter_context(patch.object(browser, 'firefox_version', return_value=143))
                popen = stack.enter_context(patch.object(browser.subprocess, 'Popen'))
                browser.main(['--browser','firefox','https://example.invalid/'])
                args = popen.call_args.args[0]
                self.assertIn('-no-remote', args)
                self.assertIn(str(base.with_name(base.name+'-Firefox')), args)
                self.assertFalse(base.exists())
                self.assertTrue((base.with_name(base.name+'-Firefox')/'user.js').is_file())
                popen.reset_mock()
                stack.enter_context(patch.object(browser, 'firefox_busy', return_value=True))
                with self.assertRaises(product.ProductError): browser.main(['--browser','firefox','https://example.invalid/'])
                popen.assert_not_called()


class GitProductTests(unittest.TestCase):
    def test_git_idempotent_and_remote_override_cannot_silently_bypass(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(host, 'secure_windows_acl'), contextlib.ExitStack() as stack:
            root = Path(temp)/'gateway'; root.mkdir()
            repo = Path(temp)/'repo with spaces'; repo.mkdir()
            clean = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
            for name in ('GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'NO_PROXY', 'no_proxy'):
                clean.pop(name, None)
            stack.enter_context(patch.dict(os.environ, clean, clear=True))
            def run(args, **kwargs):
                kwargs.setdefault('check', False)
                return subprocess.run(args, capture_output=True, text=True, env=clean, **kwargs)
            run(['git', 'init', str(repo)])
            run(['git', '-C', str(repo), 'remote', 'add', 'origin', 'https://git.example.invalid/team/repo.git'])
            api = gateway.api()
            api.ROOT, api.run = root, run
            api.configuration = lambda: {'socks_host':'127.0.0.1','socks_port':1080}
            stack.enter_context(patch.object(git_integration, 'confirm', return_value=True))
            self.assertEqual(git_integration.configure(api, repo), 0)
            before = (repo/'.git'/'config').read_bytes()
            git_integration.confirm.reset_mock()
            self.assertEqual(git_integration.configure(api, repo), 0)
            git_integration.confirm.assert_not_called()
            self.assertEqual(before, (repo/'.git'/'config').read_bytes())
            run(['git', '-C', str(repo), 'config', '--local', 'remote.origin.proxy', ''])
            self.assertFalse(git_integration.expected(api, repo)['configured'])
            with self.assertRaises(product.ProductError): git_integration.configure(api, repo)
            run(['git', '-C', str(repo), 'remote', 'set-url', 'origin', 'ftp://example.invalid/repo'])
            with self.assertRaises(product.ProductError): git_integration.expected(api, repo)


class SetupTests(unittest.TestCase):
    def test_noninteractive_setup_does_not_install(self):
        with patch.object(sys.stdin, 'isatty', return_value=False), patch.object(setup_wizard, 'installer_module') as install:
            with self.assertRaises(product.ProductError): setup_wizard.main([], ROOT)
        install.assert_not_called()

    def test_checkpoints_contain_no_private_inputs(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(host, 'secure_windows_acl'):
            root = Path(temp)
            setup_wizard.checkpoint(root, 'files')
            setup_wizard.checkpoint(root, 'files')
            setup_wizard.checkpoint(root, 'backend')
            state = json.loads((root/'setup-state.json').read_text())
        self.assertEqual(state['completed'], ['files', 'backend'])
        self.assertEqual(set(state), {'schema_version','completed','updated_at'})

    def test_update_recovery_preserves_private_files_and_removes_only_new_app_file(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(host, 'secure_windows_acl'):
            root = Path(temp); backup = root/'backups'/'app-update-123'; backup.mkdir(parents=True)
            (root/'settings.json').write_text('PRIVATE-SETTINGS')
            (root/'old.py').write_text('new')
            (root/'added.py').write_text('new')
            (backup/'old.py').write_text('old')
            (backup/'installation.json').write_text('{}')
            (backup/'inventory.json').write_text(json.dumps({'old.py':True,'added.py':False}))
            (root/'update-pending.json').write_text(json.dumps({'backup':str(backup),'files':['old.py','added.py']}))
            setup_wizard.recover_update(root, SimpleNamespace(INSTALL_FILES=['old.py','added.py']))
            self.assertEqual((root/'old.py').read_text(), 'old')
            self.assertFalse((root/'added.py').exists())
            self.assertEqual((root/'settings.json').read_text(), 'PRIVATE-SETTINGS')
            self.assertFalse((root/'update-pending.json').exists())

    def test_update_journal_cannot_target_private_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); backup=root/'backups'/'app-update-1'; backup.mkdir(parents=True)
            (root/'update-pending.json').write_text(json.dumps({'backup':str(backup),'files':['runtime/auth']}))
            (backup/'inventory.json').write_text(json.dumps({'runtime/auth':False}))
            with self.assertRaises(product.ProductError): setup_wizard.recover_update(root, SimpleNamespace(INSTALL_FILES=['old.py']))

    def test_existing_setup_resumes_and_does_not_switch_backend(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            root = Path(temp)/'IsolatedOpenVPNGateway'; root.mkdir()
            (root/'installation.json').write_text(json.dumps({'project':gateway.PROJECT}))
            (root/'settings.json').write_text(json.dumps({'backend':'wsl','browser_kind':'chromium'}))
            stub = SimpleNamespace(requirements=Mock(), gateway_module=lambda _: object())
            stack.enter_context(patch.object(sys.stdin,'isatty',return_value=True))
            stack.enter_context(patch.object(host,'is_elevated',return_value=False))
            stack.enter_context(patch.object(host,'install_root',return_value=root))
            stack.enter_context(patch.object(host,'installation_root_matches',return_value=True))
            stack.enter_context(patch.object(host,'secure_windows_acl'))
            stack.enter_context(patch.object(setup_wizard,'installer_module',return_value=stub))
            stack.enter_context(patch.object(setup_wizard,'confirm',return_value=False))
            stack.enter_context(patch.object(path_integration,'register_location'))
            invoke = stack.enter_context(patch.object(setup_wizard,'run_installed'))
            self.assertEqual(setup_wizard.main(['--browser','skip'], ROOT), 0)
            invoke.assert_called_once_with(root, 'install', '--backend', 'wsl')
            self.assertEqual(json.loads((root/'settings.json').read_text())['browser_kind'], 'chromium')
            invoke.reset_mock()
            with self.assertRaises(product.ProductError): setup_wizard.main(['--backend','docker'], ROOT)
            invoke.assert_not_called()

    def test_interrupted_backend_stage_preserves_checkpoint_and_resumes(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            root = Path(temp)/'IsolatedOpenVPNGateway'; root.mkdir()
            (root/'installation.json').write_text(json.dumps({'project':gateway.PROJECT}))
            (root/'settings.json').write_text(json.dumps({'backend':'wsl'}))
            stub = SimpleNamespace(requirements=Mock(), gateway_module=lambda _: object())
            for target, name, value in ((sys.stdin,'isatty',True), (host,'is_elevated',False),
                                        (host,'install_root',root), (host,'installation_root_matches',True)):
                stack.enter_context(patch.object(target, name, return_value=value))
            stack.enter_context(patch.object(host, 'secure_windows_acl'))
            stack.enter_context(patch.object(setup_wizard, 'installer_module', return_value=stub))
            stack.enter_context(patch.object(setup_wizard, 'confirm', return_value=False))
            stack.enter_context(patch.object(path_integration, 'register_location'))
            invoke = stack.enter_context(patch.object(setup_wizard, 'run_installed', side_effect=KeyboardInterrupt))
            with self.assertRaises(KeyboardInterrupt): setup_wizard.main(['--browser','skip'], ROOT)
            self.assertEqual(json.loads((root/'setup-state.json').read_text())['completed'], ['files'])
            self.assertFalse((root/'runtime'/'auth').exists())
            invoke.side_effect = None
            self.assertEqual(setup_wizard.main(['--browser','skip'], ROOT), 0)
            self.assertEqual(json.loads((root/'setup-state.json').read_text())['completed'], ['files','backend','preferences','complete'])

    def test_new_profile_setup_keeps_original_and_selects_wsl_without_docker(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            root = Path(temp)/'IsolatedOpenVPNGateway'
            ovpn = Path(temp)/'profile with spaces.ovpn'; ovpn.write_text(PROFILE, encoding='utf-8')
            original = ovpn.read_bytes()
            def install_files(_package, _home, profiles, _python, config_bytes, config, **kwargs):
                self.assertEqual(profiles, {'primary': original})
                self.assertEqual(kwargs['backend'], 'wsl')
                self.assertEqual(config['windows_outer_adapter_contains'], ['Synthetic outer adapter'])
                root.mkdir()
                (root/'installation.json').write_text(json.dumps({'project':gateway.PROJECT}))
                (root/'settings.json').write_text(json.dumps({'backend':'wsl'}))
                return root
            stub = SimpleNamespace(requirements=Mock(), verify_manifest=Mock(), gateway_module=lambda _: gateway,
                                   install_files=install_files)
            stack.enter_context(patch.object(sys.stdin,'isatty',return_value=True))
            stack.enter_context(patch.object(host,'IS_WINDOWS',True))
            stack.enter_context(patch.object(host,'is_elevated',return_value=False))
            stack.enter_context(patch.object(host,'install_root',return_value=root))
            stack.enter_context(patch.object(host,'secure_windows_acl'))
            stack.enter_context(patch.object(setup_wizard,'installer_module',return_value=stub))
            stack.enter_context(patch.object(setup_wizard,'confirm',side_effect=[True,True,False,False,False]))
            stack.enter_context(patch.object(path_integration,'register_location'))
            invoke = stack.enter_context(patch.object(setup_wizard,'run_installed'))
            self.assertEqual(setup_wizard.main(['--backend','wsl','--ovpn',str(ovpn),'--outer-adapter','Synthetic outer adapter','--browser','skip'], ROOT),0)
            self.assertEqual(ovpn.read_bytes(), original)
            invoke.assert_called_once_with(root,'install','--backend','wsl')
            self.assertEqual(json.loads((root/'setup-state.json').read_text())['completed'][-1], 'complete')

    def update_fixture(self, stack):
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        (root/'scripts').mkdir()
        for name in ('vpn.py','socks.py','wsl_manager.py','wsl_bridge.py','wsl_dependencies.py','wsl_sockd.conf'):
            (root/'scripts'/name).write_bytes((ROOT/'scripts'/name).read_bytes())
        (root/'scripts'/'gateway.py').write_text('old application')
        (root/'settings.json').write_text('PRIVATE-SETTINGS-UNCHANGED')
        (root/'installation.json').write_text('{}')
        stack.enter_context(patch.object(setup_wizard,'owned',return_value={'kit_version':'2026.09.01.2','project':gateway.PROJECT}))
        stack.enter_context(patch.object(setup_wizard,'load_config',return_value={'transports':{}}))
        stack.enter_context(patch.object(setup_wizard,'confirm',return_value=True))
        stack.enter_context(patch.object(setup_wizard,'run_installed'))
        stack.enter_context(patch.object(host,'secure_windows_acl'))
        installer = setup_wizard.installer_module(ROOT)
        stack.enter_context(patch.object(installer,'verify_manifest'))
        stack.enter_context(patch.object(installer,'load_profiles'))
        stack.enter_context(patch.object(installer,'INSTALL_FILES',('scripts/gateway.py','VERSION')))
        return root, installer

    def test_update_smoke_failure_restores_files_without_private_backup(self):
        with contextlib.ExitStack() as stack:
            root, installer = self.update_fixture(stack)
            stack.enter_context(patch.object(subprocess,'run',return_value=SimpleNamespace(returncode=1)))
            with self.assertRaises(product.ProductError): setup_wizard.update_files(ROOT,root,installer)
            self.assertEqual((root/'scripts'/'gateway.py').read_text(),'old application')
            self.assertFalse((root/'VERSION').exists())
            self.assertFalse((root/'update-pending.json').exists())
            self.assertEqual((root/'settings.json').read_text(),'PRIVATE-SETTINGS-UNCHANGED')
            self.assertFalse(any(p.name == 'settings.json' for p in (root/'backups').rglob('*')))

    def test_update_success_preserves_settings_and_versions_application(self):
        with contextlib.ExitStack() as stack:
            root, installer = self.update_fixture(stack)
            stack.enter_context(patch.object(subprocess,'run',return_value=SimpleNamespace(returncode=0)))
            self.assertTrue(setup_wizard.update_files(ROOT,root,installer))
            self.assertEqual((root/'VERSION').read_text(), (ROOT/'VERSION').read_text())
            self.assertEqual((root/'settings.json').read_text(),'PRIVATE-SETTINGS-UNCHANGED')
            self.assertIn('app_hashes',json.loads((root/'installation.json').read_text()))

    def test_update_modified_linux_asset_refused_before_stop(self):
        with contextlib.ExitStack() as stack:
            root, installer = self.update_fixture(stack)
            (root/'scripts'/'vpn.py').write_text('modified')
            with self.assertRaises(product.ProductError): setup_wizard.update_files(ROOT,root,installer)
            setup_wizard.run_installed.assert_not_called()

    def test_update_modified_app_refused_before_stop(self):
        with contextlib.ExitStack() as stack:
            root, installer = self.update_fixture(stack)
            stack.enter_context(patch.object(setup_wizard, 'owned', return_value={
                'kit_version':'2026.09.01.2', 'app_hashes':{'scripts/gateway.py':'wrong-digest'}}))
            with self.assertRaises(product.ProductError): setup_wizard.update_files(ROOT, root, installer)
            setup_wizard.run_installed.assert_not_called()

    def test_update_recovery_refuses_active_session(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(host, 'secure_windows_acl'):
            root = Path(temp); (root/'runtime').mkdir()
            (root/'runtime'/'active-backend').write_text('wsl')
            (root/'update-pending.json').write_text('{"backup":"unused"}')
            with self.assertRaises(product.ProductError) as caught:
                setup_wizard.recover_update(root, SimpleNamespace(INSTALL_FILES=[]))
            self.assertEqual(caught.exception.code, 'UPDATE_BUSY')
            self.assertTrue((root/'update-pending.json').exists())

    def test_checked_destination_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(product.ProductError): setup_wizard.checked_destination(Path(temp),'../elsewhere')


class PathRegistrationTests(unittest.TestCase):
    def test_location_registration_removes_only_owned_value(self):
        for preexisting in (False, True):
            with self.subTest(preexisting=preexisting), tempfile.TemporaryDirectory() as temp, patch.object(host, 'secure_windows_acl'):
                root = Path(temp)
                values = {'InstallRoot': str(root)} if preexisting else {}
                def query(_key, name):
                    if name not in values: raise FileNotFoundError
                    return values[name], 1
                registry = SimpleNamespace(HKEY_CURRENT_USER=1, KEY_READ=1, KEY_SET_VALUE=2, REG_SZ=1,
                    CreateKeyEx=lambda *_: contextlib.nullcontext(), OpenKey=lambda *_: contextlib.nullcontext(),
                    QueryValueEx=query, SetValueEx=lambda _k,n,_z,_t,v: values.update({n:v}),
                    DeleteValue=lambda _k,n: values.pop(n))
                with patch.dict(sys.modules, {'winreg': registry}):
                    path_integration.register_location(root)
                    path_integration.register_location(root)
                    self.assertEqual(json.loads((root/'location-registration.json').read_text())['owned'], not preexisting)
                    path_integration.unregister_location(root)
                    self.assertEqual('InstallRoot' in values, preexisting)

    def test_optin_registration_and_exact_owned_rollback(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            root=Path(temp)
            entry=str(root/'bin')
            state={'path':'C:\\Old','kind':2}
            stack.enter_context(patch.object(host,'secure_windows_acl'))
            stack.enter_context(patch.object(path_integration,'read_user_path',side_effect=lambda:(state['path'],state['kind'])))
            stack.enter_context(patch.object(path_integration,'write_user_path',side_effect=lambda v,k:state.update(path=v,kind=k)))
            stack.enter_context(patch.object(path_integration.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=str(root/'bin'/'vpn-gateway.cmd')+'\n')))
            stack.enter_context(patch.object(path_integration,'unregister_location'))
            path_integration.register(root)
            path_integration.register(root)
            self.assertEqual(state['path'].count(entry),1)
            state['path']+=';C:\\Later'
            path_integration.unregister(root)
            self.assertEqual(state['path'],'C:\\Old;C:\\Later')

    def test_existing_unowned_path_entry_is_not_removed(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            root=Path(temp)
            stack.enter_context(patch.object(host,'secure_windows_acl'))
            stack.enter_context(patch.object(path_integration,'read_user_path',return_value=(str(root/'bin'),2)))
            write=stack.enter_context(patch.object(path_integration,'write_user_path'))
            stack.enter_context(patch.object(path_integration.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=str(root/'bin'/'vpn-gateway.cmd')+'\n')))
            stack.enter_context(patch.object(path_integration,'unregister_location'))
            path_integration.register(root)
            path_integration.unregister(root)
            write.assert_not_called()

    @unittest.skipUnless(os.name == 'nt','Real CMD bootstrap check requires Windows')
    def test_bootstrap_missing_python_is_actionable_without_path_mutation(self):
        executable = str(Path(os.environ['SystemRoot'])/'System32'/'cmd.exe')
        result = subprocess.run([executable,'/d','/c',str(ROOT/'setup.cmd'),'--help'],env=dict(os.environ,PATH=''),
                                capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,1)
        self.assertIn('PYTHON_REQUIRED',result.stdout)

    @unittest.skipUnless(os.name == 'nt','Real CMD bootstrap check requires Windows')
    def test_bootstrap_help_from_unicode_checkout_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='gateway bootstrap ') as temp:
            package = Path(temp)/'пакет с пробелами'; package.mkdir()
            installer = setup_wizard.installer_module(ROOT)
            for name in installer.PACKAGE_FILES:
                destination = package/name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT/name, destination)
            result = subprocess.run([str(Path(os.environ['SystemRoot'])/'System32'/'cmd.exe'),
                                     '/d','/c',str(package/'setup.cmd'),'--help'],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('--installed-root', result.stdout)
            self.assertFalse((package/'installation.json').exists())

    def test_lock_probe_does_not_create_a_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'.lock'
            self.assertFalse(host.lifecycle_busy(path))
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
