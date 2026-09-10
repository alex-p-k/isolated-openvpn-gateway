import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from test_gateway import PROFILE

ROOT = Path(__file__).resolve().parents[1]

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value

installer = module('test_installer', ROOT / 'install.py')
release = module('test_release', ROOT / 'tools/build_release.py')
gateway = installer.gateway_module(ROOT)
configlib = installer.config_module(ROOT)
CONFIG = configlib.load_config(ROOT/'gateway.example.toml')
CONFIG_BYTES = (ROOT/'gateway.example.toml').read_bytes()

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gateway-handoff-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.target_home = self.base / "Mac User 'quoted' $name"
        self.target_home.mkdir()
        self.profiles = {'tcp': PROFILE.encode(), 'udp': PROFILE.replace(
            'remote vpn.example.com 443 tcp-client', 'remote vpn.example.com 1194 udp4').replace(
            'http-proxy 192.0.2.80 443\n', '').encode()}

    def copy_package(self):
        package = self.base / 'source'
        for name in installer.PACKAGE_FILES:
            dest = package / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, dest)
        (package / 'MANIFEST.sha256').write_bytes(release.manifest_bytes(package))
        return package

    def test_install_private_files_and_generated_launchers(self):
        root = installer.install_files(ROOT, self.target_home, self.profiles, Path(sys.executable),
                                       CONFIG_BYTES, CONFIG, system='Darwin', environ={})
        for folder in (root, root/'config', root/'runtime'):
            self.assertTrue(folder.is_dir())
            if os.name != 'nt':
                self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual((root/'gateway.toml').read_bytes(), CONFIG_BYTES)
        if os.name != 'nt':
            self.assertEqual((root/'gateway.toml').stat().st_mode & 0o777, 0o600)
        for kind, data in self.profiles.items():
            copied = root/'config'/CONFIG['transports'][kind]['profile_file']
            self.assertEqual(copied.read_bytes(), data)
            if os.name != 'nt':
                self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list((root/'runtime').iterdir()), [])
        for name in ('vpn-gateway', 'vpn-browser'):
            link = self.target_home/'.local/bin'/name
            self.assertTrue(link.is_symlink())
            if os.name != 'nt':
                result = subprocess.run([str(link), '--help'], capture_output=True, text=True, timeout=8)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.target_home/'.local/share/isolated-openvpn-browser').exists())

    def test_existing_installation_not_overwritten(self):
        root = installer.install_files(ROOT, self.target_home, self.profiles, Path(sys.executable),
                                       CONFIG_BYTES, CONFIG, system='Darwin', environ={})
        before = (root/'installation.json').read_bytes()
        with self.assertRaises(installer.InstallError):
            installer.install_files(ROOT, self.target_home, self.profiles, Path(sys.executable),
                                    CONFIG_BYTES, CONFIG, system='Darwin', environ={})
        self.assertEqual((root/'installation.json').read_bytes(), before)

    def test_existing_command_is_preserved(self):
        path = self.target_home/'.local/bin/vpn-gateway'
        path.parent.mkdir(parents=True)
        path.write_text('owned by someone else')
        with self.assertRaises(installer.InstallError):
            installer.install_files(ROOT, self.target_home, self.profiles, Path(sys.executable),
                                    CONFIG_BYTES, CONFIG, system='Darwin', environ={})
        self.assertEqual(path.read_text(), 'owned by someone else')

    def test_failed_install_rolls_back_only_new_root(self):
        with patch.object(installer, 'write_private', side_effect=OSError('test failure')):
            with self.assertRaises(OSError):
                installer.install_files(ROOT, self.target_home, self.profiles, Path(sys.executable),
                                        CONFIG_BYTES, CONFIG, system='Darwin', environ={})
        self.assertFalse((self.target_home/'.local/share/isolated-openvpn-gateway').exists())

    def test_profiles_unchanged_and_proxy_is_required(self):
        paths = {}
        for kind, data in self.profiles.items():
            path = self.base/(kind+'.ovpn'); path.write_bytes(data); paths[kind] = path
        self.assertEqual(installer.load_profiles(paths, gateway, CONFIG), self.profiles)
        self.assertTrue(all(paths[k].read_bytes() == v for k, v in self.profiles.items()))
        paths['tcp'].write_bytes(self.profiles['tcp'].replace(b'http-proxy 192.0.2.80 443\n', b''))
        with self.assertRaises(installer.InstallError):
            installer.load_profiles(paths, gateway, CONFIG)

    def test_profile_script_directive_is_rejected(self):
        paths = {}
        for kind, data in self.profiles.items():
            path = self.base/(kind+'.ovpn'); path.write_bytes(data+b'up /not-allowed\n'); paths[kind] = path
        with self.assertRaises(installer.InstallError):
            installer.load_profiles(paths, gateway, CONFIG)

    def test_manifest_detects_modification(self):
        package = self.copy_package()
        installer.verify_manifest(package)
        (package/'scripts/browser.py').write_text('modified')
        with self.assertRaises(installer.InstallError):
            installer.verify_manifest(package)

    def test_archive_excludes_private_files(self):
        package = self.copy_package()
        marker = b'test-only-' + os.urandom(24).hex().encode()
        for name in ('runtime/auth', 'config/private.ovpn', 'validation/private.json',
                     'backups/private', 'gateway.toml'):
            path = package/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(marker)
        archive = self.base/'release.zip'
        release.build(package, archive)
        with zipfile.ZipFile(archive) as data:
            expected = {'isolated-openvpn-gateway/'+name for name in (*installer.PACKAGE_FILES, 'MANIFEST.sha256')}
            self.assertEqual(set(data.namelist()), expected)
            self.assertTrue(all(marker not in data.read(name) for name in data.namelist()))
            data.extractall(self.base/'extracted')
        installer.verify_manifest(self.base/'extracted/isolated-openvpn-gateway')

    def test_export_rejects_symlink(self):
        package = self.copy_package()
        path = package/'scripts/browser.py'; path.unlink(); path.symlink_to(ROOT/'scripts/browser.py')
        with self.assertRaises(release.installer.InstallError):
            release.build(package, self.base/'bad.zip')

class WindowsShellInstructionTests(unittest.TestCase):
    def test_resolved_root_and_call_operator_for_each_backend(self):
        root = Path('C:/Users/Test User/Packages/App/LocalCache/Local/IsolatedOpenVPNGateway')
        for backend in ('docker', 'wsl'):
            with self.subTest(backend=backend):
                lines = installer.windows_shell_instructions(root, backend)
                command = "& '" + str(root/'bin'/'vpn-gateway.cmd') + "'"
                self.assertIn(command + ' start --backend ' + backend, lines)
                self.assertEqual(command + ' install --backend wsl' in lines, backend == 'wsl')
                self.assertIn("$gatewayBin = '" + str(root/'bin') + "'", lines)
                self.assertNotIn('LOCALAPPDATA', '\n'.join(lines))
                self.assertNotIn('setx', '\n'.join(lines).lower())
                self.assertIn("$env:Path = $gatewayBin + ';' + $env:Path", lines)

    def test_quotes_and_dollar_signs_are_literal_data(self):
        root = Path("C:/Users/O'Brien $name/IsolatedOpenVPNGateway")
        lines = installer.windows_shell_instructions(root, 'wsl')
        quoted = "'" + str(root/'bin').replace("'", "''") + "'"
        self.assertIn('$gatewayBin = ' + quoted, lines)
        self.assertIn("O''Brien $name", '\n'.join(lines))

    def test_invalid_backend_and_control_characters_are_rejected(self):
        with self.assertRaises(installer.InstallError):
            installer.windows_shell_instructions(Path('gateway'), 'unknown')
        for character in ('\n', '\r', '\x00'):
            with self.subTest(character=repr(character)), self.assertRaises(installer.InstallError):
                installer.windows_shell_instructions('gateway'+character+'path', 'wsl')

    @unittest.skipUnless(os.name == 'nt', 'Actual Windows PowerShell launcher test')
    def test_actual_powershell_resolves_short_command_after_session_only_setup(self):
        with tempfile.TemporaryDirectory(prefix='gateway-shell-test-') as temporary:
            root = Path(temporary)/"Test O'Brien $name"/'IsolatedOpenVPNGateway'
            (root/'bin').mkdir(parents=True)
            launcher = root/'bin'/'vpn-gateway.cmd'
            launcher.write_bytes(b'@echo off\r\necho SYNTHETIC-LAUNCHER-OK %1\r\n')
            lines = installer.windows_shell_instructions(root, 'docker')
            setup = [line for line in lines if line.startswith(('$gatewayBin =', '$env:Path ='))]
            full_path_status = next(line for line in lines if line.startswith('& ')).rsplit(' start --backend ', 1)[0] + ' status'
            # Execute only the fake status launcher, never install/start or real WSL.
            script = '\n'.join([
                "$ErrorActionPreference='Stop'",
                "$beforeUser=[Environment]::GetEnvironmentVariable('Path','User')",
                "$beforeMachine=[Environment]::GetEnvironmentVariable('Path','Machine')",
                full_path_status,
                'if ($LASTEXITCODE -ne 0) { exit 1 }',
                *setup,
                'vpn-gateway status',
                'if ($LASTEXITCODE -ne 0) { exit 1 }',
                "if ([Environment]::GetEnvironmentVariable('Path','User') -cne $beforeUser) { exit 2 }",
                "if ([Environment]::GetEnvironmentVariable('Path','Machine') -cne $beforeMachine) { exit 3 }",
            ])
            output = subprocess.run([gateway.POWERSHELL, '-NoLogo', '-NoProfile',
                '-NonInteractive', '-Command', script], capture_output=True, text=True,
                timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(output.returncode, 0, output.stderr)
            self.assertEqual(output.stdout.count('SYNTHETIC-LAUNCHER-OK status'), 2)


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gateway-git-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base/'repo with spaces'
        env = dict(gateway.ENV, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        self.env_patch = patch.object(gateway, 'ENV', env)
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        self.root_patch = patch.object(gateway, 'ROOT', self.base/'private gateway')
        self.root_patch.start(); self.addCleanup(self.root_patch.stop)
        self.config_patch = patch.object(gateway, 'configuration', return_value=CONFIG)
        self.config_patch.start(); self.addCleanup(self.config_patch.stop)
        gateway.run(['git', 'init', '-q', self.repo])

    def add_remote(self, url):
        gateway.run(['git', '-C', self.repo, 'remote', 'add', 'origin', url])

    def test_https_is_scoped_to_selected_repository_and_url(self):
        url = 'https://corp.example.invalid/team/repo.git'
        self.add_remote(url)
        with patch('git_integration.confirm', return_value=True):
            gateway.configure_git(self.repo)
        value = gateway.run(['git', '-C', self.repo, 'config', '--local', '--get', 'http.'+url+'.proxy'])
        self.assertEqual(value.stdout.strip(), 'socks5h://127.0.0.1:1080')
        other = gateway.run(['git', '-C', self.repo, 'config', '--get-urlmatch',
                             'http.proxy', 'https://other.example.invalid/repo.git'], check=False)
        self.assertNotEqual(other.returncode, 0)

    def test_existing_ssh_override_is_not_overwritten(self):
        self.add_remote('git@corp.example.invalid:team/repo.git')
        gateway.run(['git', '-C', self.repo, 'config', '--local', 'core.sshCommand', 'ssh -v'])
        with patch('git_integration.confirm', return_value=False), patch.dict(os.environ, {'GIT_SSH':'', 'GIT_SSH_COMMAND':''}):
            gateway.configure_git(self.repo)
        self.assertFalse((gateway.ROOT/'config').exists())

    def test_ssh_config_preserves_other_host_and_blocks_master_reuse(self):
        if os.name == 'nt':
            self.skipTest('POSIX OpenSSH Include permission semantics are covered on macOS/Linux')
        user_config = self.base/'user ssh.conf'
        user_config.write_text('Host personal.example.invalid\n    HostName 203.0.113.44\n    Port 2222\n')
        system_config = self.base/'system.conf'
        system_config.write_text('')
        config = self.base/'generated.conf'
        config.write_text(gateway.repository_ssh_config('corp.example.invalid', user_config, system_config))
        def settings(host):
            ssh = shutil.which('ssh')
            if not ssh:
                self.skipTest('OpenSSH client is unavailable')
            output = gateway.run([ssh, '-G', '-F', config, host]).stdout
            return dict(line.split(' ', 1) for line in output.splitlines() if ' ' in line)
        personal = settings('personal.example.invalid')
        corporate = settings('corp.example.invalid')
        self.assertEqual(personal['hostname'], '203.0.113.44')
        self.assertEqual(personal['port'], '2222')
        self.assertNotIn('127.0.0.1:1080', personal.get('proxycommand', ''))
        self.assertIn('127.0.0.1:1080', corporate['proxycommand'])
        self.assertEqual(corporate.get('controlpath', 'none'), 'none')
        self.assertEqual(corporate['canonicalizehostname'], 'false')

    def test_uninstalled_source_cannot_stop_existing_gateway(self):
        with patch.object(sys, 'argv', ['vpn-gateway', 'stop']), patch.object(gateway, 'compose') as compose:
            with self.assertRaises(gateway.ProductError):
                gateway.main()
            compose.assert_not_called()

if __name__ == '__main__':
    unittest.main()
