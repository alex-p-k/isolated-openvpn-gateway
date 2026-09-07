import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import contextlib
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


host = module('windows_test_host', ROOT/'scripts'/'host.py')
gateway = module('windows_test_gateway', ROOT/'scripts'/'gateway.py')
socks_connect = module('windows_test_socks_connect', ROOT/'scripts'/'socks_connect.py')
browser = module('windows_test_browser', ROOT/'scripts'/'browser.py')
installer = module('windows_test_installer', ROOT/'install.py')
configlib = module('windows_test_config', ROOT/'scripts'/'gateway_config.py')
CONFIG = configlib.load_config(ROOT/'gateway.example.toml')

PROFILE = '''client
dev tun
auth-user-pass
remote vpn.example.com 443 tcp-client
http-proxy 192.0.2.80 443
ncp-ciphers AES-128-GCM
cipher AES-128-CBC
remote-cert-tls server
auth SHA256
key-direction 1
<ca>
OPAQUE CA TEST FIXTURE
</ca>
<tls-auth>
OPAQUE KEY TEST FIXTURE
</tls-auth>
'''


class HostTests(unittest.TestCase):
    def test_windows_paths_stay_in_local_app_data(self):
        home = Path('C:/Users/Test User')
        env = {'LOCALAPPDATA': 'C:/Users/Test User/AppData/Local'}
        self.assertEqual(host.install_root(home, env, 'Windows'),
                         Path('C:/Users/Test User/AppData/Local/IsolatedOpenVPNGateway'))
        self.assertEqual(host.browser_root(home, env, 'Windows'),
                         Path('C:/Users/Test User/AppData/Local/IsolatedOpenVPNBrowser'))
        self.assertEqual(host.command_directory(host.install_root(home, env, 'Windows'), home, 'Windows'),
                         Path('C:/Users/Test User/AppData/Local/IsolatedOpenVPNGateway/bin'))

    def test_windows_browser_candidates_cover_chrome_chromium_and_edge(self):
        values = [str(x).replace('\\','/') for x in host.browser_candidates(
            environ={'ProgramFiles':'C:/Program Files',
                     'ProgramFiles(x86)':'C:/Program Files (x86)',
                     'LOCALAPPDATA':'C:/Users/A/AppData/Local'}, system='Windows')]
        self.assertTrue(any(x.endswith('Google/Chrome/Application/chrome.exe') for x in values))
        self.assertTrue(any(x.endswith('Chromium/Application/chrome.exe') for x in values))
        self.assertTrue(any(x.endswith('Microsoft/Edge/Application/msedge.exe') for x in values))

    def test_batch_launcher_uses_relative_script_and_escapes_percent(self):
        value = host.batch_launcher(r'C:\Users\100% User\Python\python.exe', 'gateway.py').decode()
        self.assertIn(r'100%% User', value)
        self.assertIn(r'%~dp0..\scripts\gateway.py', value)
        self.assertIn('chcp 65001', value)
        self.assertIn('PYTHONDONTWRITEBYTECODE', value)
        self.assertNotIn('password', value.lower())

    def test_ntfs_acl_command_is_sid_based_and_removes_inheritance(self):
        command = host.windows_acl_command(Path(r'C:\Private\auth'), 'S-1-5-21-1234', False)
        self.assertEqual(command[:4], ['icacls.exe', r'C:\Private\auth', '/inheritance:r', '/grant:r'])
        self.assertEqual(command[-1], '*S-1-5-21-1234:(F)')
        directory = host.windows_acl_command(Path(r'C:\Private'), 'S-1-5-21-1234', True)
        self.assertEqual(directory[-1], '*S-1-5-21-1234:(OI)(CI)(F)')

    def test_windows_command_paths_reject_percent_expansion(self):
        with self.assertRaises(OSError):
            host.ensure_windows_command_paths(r'C:\Users\100% User\Gateway')
        with self.assertRaises(OSError):
            host.ensure_windows_command_paths(r'C:\Users\Bang!User\Gateway')

    def test_failed_windows_acl_removes_new_private_object(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(host, 'secure_windows_acl', side_effect=OSError('ACL failed')):
            directory = Path(tmp)/'private'
            with self.assertRaises(OSError):
                host.private_directory(directory, system='Windows')
            self.assertFalse(directory.exists())
            file = Path(tmp)/'private.txt'
            with self.assertRaises(OSError):
                host.private_write(file, 'private', system='Windows')
            self.assertFalse(file.exists())

    def test_windows_lifecycle_lock_locks_and_unlocks_one_byte(self):
        calls = []
        fake = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2,
                               locking=lambda fd, mode, size: calls.append((mode, size)))
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(host, 'IS_WINDOWS', True), patch.dict(sys.modules, {'msvcrt': fake}):
            with host.lifecycle_lock(Path(tmp)/'gateway.lock'):
                self.assertEqual(calls, [(1, 1)])
        self.assertEqual(calls, [(1, 1), (2, 1)])

    def test_windows_config_requires_bounded_adapter_matchers(self):
        self.assertEqual(CONFIG['windows_outer_adapter_contains'],
                         ['REPLACE-WITH-OUTER-VPN-ADAPTER'])
        text = (ROOT/'gateway.example.toml').read_text().replace(
            'windows_outer_adapter_contains = ["REPLACE-WITH-OUTER-VPN-ADAPTER"]',
            'windows_outer_adapter_contains = ["Outer VPN", "Outer VPN"]')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'gateway.toml'; path.write_text(text)
            value = configlib.load_config(path)
            path.write_text(text.replace('["Outer VPN", "Outer VPN"]', '["x"]'))
            with self.assertRaises(configlib.ConfigError):
                configlib.load_config(path)
        self.assertEqual(value['windows_outer_adapter_contains'], ['Outer VPN'])


class WindowsInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gateway-windows-install-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base/'Windows User'
        self.home.mkdir()
        self.local = self.home/'AppData'/'Local'
        self.env = {'LOCALAPPDATA': str(self.local)}
        self.profiles = {
            'tcp': PROFILE.encode(),
            'udp': PROFILE.replace('remote vpn.example.com 443 tcp-client',
                                   'remote vpn.example.com 1194 udp4').replace(
                                       'http-proxy 192.0.2.80 443\n','').encode(),
        }

    def test_windows_install_uses_cmd_launchers_and_private_acl_calls(self):
        with patch.object(installer, 'host_module', return_value=host), \
             patch.object(host, 'secure_windows_acl') as acl:
            root = installer.install_files(
                ROOT, self.home, self.profiles, Path(r'C:\Program Files\Python\python.exe'),
                (ROOT/'gateway.example.toml').read_bytes(), CONFIG,
                system='Windows', environ=self.env)
        self.assertEqual(root, self.local/'IsolatedOpenVPNGateway')
        self.assertFalse((self.home/'.local').exists())
        for name in ('vpn-gateway.cmd','vpn-browser.cmd'):
            launcher = root/'bin'/name
            self.assertTrue(launcher.is_file())
            self.assertFalse(launcher.is_symlink())
            self.assertIn(b'%~dp0..\\scripts\\', launcher.read_bytes())
        for name in ('WINDOWS.md','scripts/host.py','scripts/socks_connect.py'):
            self.assertTrue((root/name).is_file())
        self.assertFalse((root/'tests'/'test_windows.py').exists())
        self.assertEqual((root/'config'/CONFIG['transports']['tcp']['profile_file']).read_bytes(),
                         self.profiles['tcp'])
        self.assertGreater(acl.call_count, 10)

    def test_windows_acl_failure_rolls_back_new_root(self):
        with patch.object(installer, 'host_module', return_value=host), \
             patch.object(host, 'secure_windows_acl', side_effect=OSError('ACL failure')):
            with self.assertRaises(OSError):
                installer.install_files(
                    ROOT, self.home, self.profiles, Path(r'C:\Python\python.exe'),
                    (ROOT/'gateway.example.toml').read_bytes(), CONFIG,
                    system='Windows', environ=self.env)
        self.assertFalse((self.local/'IsolatedOpenVPNGateway').exists())


class WindowsGatewayTests(unittest.TestCase):
    def test_powershell_output_and_decoder_use_explicit_utf8(self):
        completed = SimpleNamespace(returncode=0, stdout='ready', stderr='')
        command = gateway.powershell("Write-Output 'ready'")
        self.assertIn('[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)', command[-1])
        with patch.object(gateway.subprocess, 'run', return_value=completed) as run:
            gateway.run(command)
        self.assertEqual(run.call_args.kwargs['encoding'], 'utf-8')

    @unittest.skipUnless(gateway.host.IS_WINDOWS, 'requires real Windows PowerShell encoding')
    def test_real_windows_unicode_json_is_identical_without_a_console(self):
        import json
        expected = 'Test \u0416\u043b\u044e\u0437 \u65e5\u672c\u8a9e'
        command = gateway.powershell("@{Alias='" + expected + "'} | ConvertTo-Json -Compress")
        outputs = []
        for flags in (0, gateway.subprocess.CREATE_NO_WINDOW):
            response = gateway.run(command, creationflags=flags, timeout=15)
            self.assertEqual(json.loads(response.stdout), {'Alias':expected})
            outputs.append(response.stdout)
        self.assertEqual(outputs[0], outputs[1])

    def setUp(self):
        config = dict(CONFIG)
        config['windows_outer_adapter_contains'] = ['Outer VPN']
        self.config = config

    def test_windows_network_snapshot_commands_are_read_only(self):
        with patch.object(gateway, 'POWERSHELL', 'powershell.exe'):
            commands = gateway.windows_network_commands()
        text = '\n'.join(' '.join(value) for value in commands.values())
        self.assertIn('Get-DnsClientServerAddress', text)
        self.assertIn('Get-NetRoute', text)
        self.assertIn('Find-NetRoute', text)
        self.assertIn('Get-NetAdapter', text)
        self.assertNotIn('Set-', text)
        self.assertNotIn('New-', text)

    def test_outer_route_can_match_adapter_description_and_rejects_malformed_json(self):
        snapshot = {
            'adapters': {'stdout':'[{"Name":"Local Area 7","InterfaceDescription":"Amnezia Wintun",'
                                   '"ifIndex":9,"Status":"Up"}]'},
            'public_route': {'stdout':'[{"InterfaceIndex":9}]'},
        }
        self.assertTrue(gateway.windows_outer_route_matches(snapshot, ['amnezia']))
        snapshot['public_route']['stdout'] = 'not-json'
        self.assertFalse(gateway.windows_outer_route_matches(snapshot, ['amnezia']))

    def test_host_comparison_includes_default_route(self):
        before = {'diagnostics_ok':True, 'dns':{}, 'default':{'stdout':'vpn'},
                  'static_routes':[], 'public_route_signature':'vpn', 'public_ip':'203.0.113.5',
                  'git_global_proxy':{}}
        after = dict(before); after['default'] = {'stdout':'wifi'}
        self.assertFalse(gateway.compare_host(before, after)['host_default_route_preserved'])

    def test_windows_outer_adapter_and_matching_egress_pass_preflight(self):
        snapshot = {'public_ip':'203.0.113.10','diagnostics_ok':True,
                    'adapters':{'stdout':'[{"Name":"Outer VPN","InterfaceDescription":"Wintun",'
                                          '"ifIndex":42,"Status":"Up"}]'},
                    'public_route':{'stdout':'[{"InterfaceIndex":42,"InterfaceAlias":"Outer VPN"}]'}}
        tun = SimpleNamespace(returncode=0, stdout='', stderr='')
        completed = SimpleNamespace(returncode=0, stdout='203.0.113.10', stderr='')
        with patch.object(gateway.host, 'IS_WINDOWS', True), \
             patch.object(gateway, 'configuration', return_value=self.config), \
             patch.object(gateway, 'engine'), patch.object(gateway, 'images'), \
             patch.object(gateway, 'network_snapshot', return_value=snapshot), \
             patch.object(gateway, 'docker', side_effect=(tun,completed)) as docker, \
             patch.object(gateway, 'private_write'):
            result = gateway.preflight()
        self.assertEqual(result['docker_public_ip'], '203.0.113.10')
        self.assertEqual(docker.call_count, 2)

    def test_windows_preflight_fails_closed_without_configured_adapter(self):
        config = dict(self.config); config['windows_outer_adapter_contains'] = []
        snapshot = {'public_ip':'203.0.113.10','diagnostics_ok':True,
                    'adapters':{'stdout':'[]'},'public_route':{'stdout':'[]'}}
        tun = SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(gateway.host, 'IS_WINDOWS', True), \
             patch.object(gateway, 'configuration', return_value=config), \
             patch.object(gateway, 'engine'), patch.object(gateway, 'images'), \
             patch.object(gateway, 'network_snapshot', return_value=snapshot), \
             patch.object(gateway, 'docker', return_value=tun) as docker:
            with self.assertRaises(gateway.GatewayError):
                gateway.preflight()
        self.assertEqual(docker.call_count, 1)

    def test_windows_preflight_rejects_up_adapter_not_used_by_public_route(self):
        snapshot = {'public_ip':'203.0.113.10','diagnostics_ok':True,
                    'adapters':{'stdout':'[{"Name":"Outer VPN","ifIndex":42,"Status":"Up"}]'},
                    'public_route':{'stdout':'[{"InterfaceIndex":7,"InterfaceAlias":"Wi-Fi"}]'}}
        tun = SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(gateway.host, 'IS_WINDOWS', True), \
             patch.object(gateway, 'configuration', return_value=self.config), \
             patch.object(gateway, 'engine'), patch.object(gateway, 'images'), \
             patch.object(gateway, 'network_snapshot', return_value=snapshot), \
             patch.object(gateway, 'docker', return_value=tun) as docker:
            with self.assertRaisesRegex(gateway.GatewayError, 'selected Windows public route'):
                gateway.preflight()
        self.assertEqual(docker.call_count, 1)

    def test_preflight_refuses_missing_tun_before_host_or_credentials(self):
        failed = SimpleNamespace(returncode=1, stdout='', stderr='device missing')
        with patch.object(gateway, 'configuration', return_value=self.config), \
             patch.object(gateway, 'engine'), patch.object(gateway, 'images'), \
             patch.object(gateway, 'docker', return_value=failed), \
             patch.object(gateway, 'network_snapshot') as snapshot:
            with self.assertRaises(gateway.GatewayError):
                gateway.preflight()
        snapshot.assert_not_called()

    def test_windows_ssh_uses_bundled_python_socks_bridge(self):
        config_path = Path(r'C:\Users\A\AppData\Local\Gateway\ssh.conf')
        with patch.object(gateway.host, 'IS_WINDOWS', True), \
             patch.object(gateway.host, 'ssh_config_paths', return_value=(
                 Path(r'C:\Users\A\.ssh\config'),Path(r'C:\ProgramData\ssh\ssh_config'))), \
             patch.object(gateway.host, 'ssh_executable', return_value=r'C:\Windows\System32\OpenSSH\ssh.exe'), \
             patch.object(gateway, 'configuration', return_value=self.config), \
             patch.object(gateway, 'ROOT', Path(r'C:\Users\A\Gateway')):
            value, text = gateway.repository_ssh_settings('git.private.example', config_path)
        self.assertIn('ssh.exe" -F', value)
        self.assertIn('socks_connect.py', text)
        self.assertIn('127.0.0.1 1080 %h %p', text)
        self.assertNotIn('/usr/bin/nc', text)
        self.assertIn('C:/ProgramData/ssh/ssh_config', text)

    def test_windows_proxy_command_is_valid_openssh_config_syntax(self):
        proxy = ('"C:/Program Files/Python/python.exe" '
                 '"C:/Users/A/Gateway/scripts/socks_connect.py" 127.0.0.1 1080 %h %p')
        text = gateway.repository_ssh_config(
            'git.private.example', 'C:/missing/user-config', 'C:/missing/system-config',
            proxy_command=proxy)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'ssh.conf'; path.write_text(text)
            ssh = shutil.which('ssh')
            if not ssh:
                self.skipTest('OpenSSH client is unavailable')
            result = subprocess.run([ssh,'-G','-F',path,'git.private.example'],
                                    capture_output=True,text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('socks_connect.py', result.stdout)
        self.assertIn('127.0.0.1 1080 %h %p', result.stdout)

    def test_windows_uninstall_defers_root_removal_until_lock_is_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'IsolatedOpenVPNGateway'; root.mkdir()
            (root/'installation.json').write_text('{"project":"isolated-openvpn-gateway"}')
            with patch.object(gateway.host, 'IS_WINDOWS', True), \
                 patch.object(gateway.host, 'install_root', return_value=root), \
                 patch.object(gateway.host, 'browser_root', return_value=Path(tmp)/'absent-browser'), \
                 patch.object(gateway, 'ROOT', root), patch('builtins.input', return_value='REMOVE'), \
                 patch.object(gateway, 'stop_internal'), patch.object(gateway, 'docker'):
                self.assertTrue(gateway.uninstall())
            self.assertTrue(root.exists())

    def test_windows_main_removes_root_only_after_lock_context_closes(self):
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'IsolatedOpenVPNGateway'; root.mkdir()
            runtime = root/'runtime'
            (root/'installation.json').write_text('{"project":"isolated-openvpn-gateway"}')

            @contextlib.contextmanager
            def lifecycle(_path, _enabled):
                events.append('locked')
                yield
                events.append('unlocked')

            def remove(path):
                events.append('remove')
                self.assertEqual(Path(path), root)

            with patch.object(sys, 'argv', ['vpn-gateway','uninstall']), \
                 patch.object(gateway, 'ROOT', root), patch.object(gateway, 'RUNTIME', runtime), \
                 patch.object(gateway.host, 'IS_WINDOWS', True), \
                 patch.object(gateway.host, 'install_root', return_value=root), \
                 patch.object(gateway.host, 'maybe_lifecycle_lock', side_effect=lifecycle), \
                 patch.object(gateway, 'uninstall', return_value=True), \
                 patch.object(gateway.shutil, 'rmtree', side_effect=remove):
                self.assertEqual(gateway.main(), 0)
        self.assertEqual(events, ['locked','unlocked','remove'])


class WindowsBrowserTests(unittest.TestCase):
    def test_windows_browser_uses_detached_process_and_isolated_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); root = base/'IsolatedOpenVPNGateway'; root.mkdir()
            profile = base/'IsolatedOpenVPNBrowser'; executable = base/'msedge.exe'
            executable.write_bytes(b'fixture')
            (root/'installation.json').write_text('{"project":"isolated-openvpn-gateway"}')
            launched = {}

            def popen(args, **kwargs):
                launched['args'] = args; launched['kwargs'] = kwargs
                return SimpleNamespace()

            with patch.object(sys, 'argv', ['vpn-browser','https://private.example']), \
                 patch.object(browser, 'ROOT', root), patch.object(browser, 'PROFILE', profile), \
                 patch.object(browser.host, 'IS_WINDOWS', True), \
                 patch.object(browser.host, 'install_root', return_value=root), \
                 patch.object(browser.host, 'browser_candidates', return_value=(executable,)), \
                 patch.object(browser.host, 'secure_windows_acl'), \
                 patch.object(browser, 'load_config', return_value=self._config()), \
                 patch.object(browser.subprocess, 'CREATE_NEW_PROCESS_GROUP', 1, create=True), \
                 patch.object(browser.subprocess, 'DETACHED_PROCESS', 8, create=True), \
                 patch.object(browser.subprocess, 'Popen', side_effect=popen):
                browser.main()
        self.assertEqual(launched['kwargs']['creationflags'], 9)
        self.assertTrue(any(str(value).startswith('--user-data-dir=') for value in launched['args']))
        self.assertFalse(any('direct://' in str(value) for value in launched['args']))
        self.assertIn('--disable-quic', launched['args'])

    @staticmethod
    def _config():
        return {'socks_host':'127.0.0.1','socks_port':1080}


class SocksConnectTests(unittest.TestCase):
    def test_stdio_relay_reads_short_packets_without_waiting_for_eof(self):
        import threading
        arrived = threading.Event()
        class Source:
            packets = [b'SSH-2.0-test\r\n', b'']
            def read(self, length):
                raise AssertionError('Buffered read waits for more SSH data')
            def read1(self, length):
                return self.packets.pop(0)
        class Stream:
            def sendall(self, data):
                self.sent = data
                arrived.set()
            def shutdown(self, how): pass
            def recv(self, length):
                if not arrived.wait(2):
                    raise TimeoutError('Short SSH packet was not forwarded')
                return b''
        stream = Stream()
        socks_connect.relay(stream, Source(), io.BytesIO())
        self.assertEqual(stream.sent, b'SSH-2.0-test\r\n')

    def test_proxy_negotiation_is_bounded_before_unlimited_stdio_relay(self):
        events = []
        with patch.object(socks_connect.socket, 'create_connection') as connect, \
             patch.object(socks_connect, 'negotiate', side_effect=lambda *args: events.append('negotiate')), \
             patch.object(socks_connect, 'relay', side_effect=lambda *args: events.append('relay')):
            connect.return_value.__enter__.return_value.settimeout.side_effect = events.append
            self.assertEqual(socks_connect.main(['127.0.0.1','1080','git.example.test','22']), 0)
        self.assertEqual(events, [12, 'negotiate', None, 'relay'])

    def test_socks_handshake_supports_proxy_side_domain_resolution(self):
        class Stream:
            def __init__(self):
                self.buffer = b'\x05\x00\x05\x00\x00\x01' + bytes([127,0,0,1]) + b'\x00\x00'
                self.sent = []
            def sendall(self, data): self.sent.append(data)
            def recv(self, length):
                data, self.buffer = self.buffer[:length], self.buffer[length:]
                return data
        stream = Stream()
        socks_connect.negotiate(stream, 'git.private.example', 22)
        self.assertEqual(stream.sent[0], b'\x05\x01\x00')
        self.assertIn(b'git.private.example', stream.sent[1])
        self.assertTrue(stream.sent[1].endswith(b'\x00\x16'))

    def test_proxy_command_refuses_non_loopback_proxy(self):
        stderr = io.StringIO()
        with patch('sys.stderr', stderr), patch.object(socks_connect.socket, 'create_connection') as connect:
            self.assertEqual(socks_connect.main(['192.0.2.20','1080','target.example','22']), 1)
        connect.assert_not_called()
        self.assertIn('loopback', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
