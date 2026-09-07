"""PID reuse safety, including real disposable Linux process controls."""
import importlib.util
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('wsl_process_manager',
    Path(__file__).resolve().parents[1]/'scripts'/'wsl_manager.py')
manager = importlib.util.module_from_spec(spec)
if os.name == 'nt':
    with patch.dict(sys.modules, {'pwd': SimpleNamespace()}):
        spec.loader.exec_module(manager)
else:
    spec.loader.exec_module(manager)


class ProcessOwnershipTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='gateway-pid-test-')
        self.addCleanup(directory.cleanup)
        self.pid = Path(directory.name)/'netns.pid'
        self.pid.write_text('12345\n')
        target = patch.object(manager, 'NETNS_PID', self.pid)
        target.start()
        self.addCleanup(target.stop)
        sigkill = patch.object(signal, 'SIGKILL', 9, create=True)
        sigkill.start()
        self.addCleanup(sigkill.stop)

    def test_nonpositive_init_malformed_and_unknown_pid_files_are_rejected(self):
        with patch.object(os, 'kill') as kill:
            for value in ('0', '-1', '-123', '1', 'not-a-pid', ''):
                self.pid.write_text(value)
                self.assertIsNone(manager.pid_alive(self.pid))
            self.assertFalse(manager.process_matches(self.pid.parent/'other.pid', 12345))
        kill.assert_not_called()

    def test_each_role_requires_exact_command_root_owner_and_namespace(self):
        commands = {
            self.pid: ['/usr/bin/sleep', 'infinity'],
            manager.SLIRP_PID: ['/usr/bin/slirp4netns', '--configure', '--mtu=65520',
                '--disable-host-loopback', '--cidr=10.0.2.0/24', '--netns-type=path',
                str(manager.NETNS_PATH), 'eth0'],
            manager.VPN_PID: ['/usr/bin/python3', '-u', str(manager.BASE/'vpn.py')],
            manager.SOCKS_PID: ['/usr/sbin/runuser', '-u', 'proxyuser', '--',
                '/usr/bin/python3', '-u', str(manager.BASE/'socks.py')],
        }
        for path, args in commands.items():
            for command_ok, uid, same_ns in ((True, 0, True), (False, 0, True),
                                            (True, 1000, True), (True, 0, False)):
                data = b'\x00'.join(os.fsencode(arg) for arg in args) + b'\x00'
                if not command_ok:
                    data += b'unexpected-argument\x00'
                with self.subTest(role=path.name, command_ok=command_ok, uid=uid, same_ns=same_ns), \
                     patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=uid)), \
                     patch.object(Path, 'open', return_value=io.BytesIO(data)), \
                     patch.object(Path, 'samefile', return_value=same_ns):
                    self.assertEqual(manager.process_matches(path, 12345),
                                     command_ok and uid == 0 and same_ns)

    def test_missing_process_metadata_is_not_ownership(self):
        with patch.object(Path, 'stat', side_effect=PermissionError):
            self.assertFalse(manager.process_matches(self.pid, 12345))

    def test_ownership_rechecked_after_pidfd_open_before_any_signal(self):
        with patch.object(manager, 'pid_alive', return_value=12345), \
             patch.object(manager, 'process_matches', return_value=False), \
             patch.object(os, 'pidfd_open', return_value=77, create=True) as opened, \
             patch.object(signal, 'pidfd_send_signal', create=True) as send, \
             patch.object(os, 'close') as close, patch.object(os, 'kill') as kill:
            manager.kill_pid(self.pid)
        opened.assert_called_once_with(12345, 0)
        send.assert_not_called()
        kill.assert_not_called()
        close.assert_called_once_with(77)
        self.assertFalse(self.pid.exists())

    def test_term_and_timeout_kill_use_the_same_pinned_process(self):
        with patch.object(manager, 'pid_alive', return_value=12345), \
             patch.object(manager, 'process_matches', return_value=True), \
             patch.object(os, 'pidfd_open', return_value=77, create=True), \
             patch.object(signal, 'pidfd_send_signal', create=True) as send, \
             patch.object(manager.select, 'select', side_effect=[([], [], []), ([77], [], [])]), \
             patch.object(os, 'close') as close, patch.object(os, 'kill') as kill:
            manager.kill_pid(self.pid, keep_file=True)
        self.assertEqual([call.args for call in send.call_args_list],
                         [(77, signal.SIGTERM), (77, signal.SIGKILL)])
        kill.assert_not_called()
        close.assert_called_once_with(77)
        self.assertTrue(self.pid.exists())

    def test_already_exited_process_is_safe_and_descriptor_closed(self):
        with patch.object(manager, 'pid_alive', return_value=12345), \
             patch.object(manager, 'process_matches', return_value=True), \
             patch.object(os, 'pidfd_open', return_value=77, create=True), \
             patch.object(signal, 'pidfd_send_signal', side_effect=ProcessLookupError, create=True), \
             patch.object(os, 'close') as close:
            manager.kill_pid(self.pid)
        close.assert_called_once_with(77)
        self.assertFalse(self.pid.exists())

    def test_pidfd_failure_never_falls_back_to_numeric_kill(self):
        with patch.object(manager, 'pid_alive', return_value=12345), \
             patch.object(os, 'pidfd_open', side_effect=PermissionError, create=True), \
             patch.object(signal, 'pidfd_send_signal', create=True) as send, \
             patch.object(os, 'kill') as kill:
            with self.assertRaises(PermissionError):
                manager.kill_pid(self.pid)
        send.assert_not_called()
        kill.assert_not_called()
        self.assertTrue(self.pid.exists())

    def test_cleanup_failure_still_removes_auth_unless_explicitly_preserved(self):
        auth = self.pid.parent/'auth'
        for keep in (False, True):
            auth.write_text('synthetic-only')
            with patch.object(manager, 'AUTH', auth), patch.object(manager, 'require_root'), \
                 patch.object(manager, 'systemd_available', return_value=False), \
                 patch.object(manager, 'kill_pid', side_effect=RuntimeError('cleanup unavailable')):
                with self.assertRaises(RuntimeError):
                    manager.stop(keep_auth=keep)
            self.assertEqual(auth.exists(), keep)

    @unittest.skipUnless(sys.platform == 'linux' and hasattr(os, 'pidfd_open')
                         and getattr(os, 'geteuid', lambda: -1)() == 0,
                         'Real disposable process test requires Linux root and pidfd')
    def test_real_linux_positive_stop_and_stale_pid_negative_control(self):
        # Only these two children can be targeted. Never use production PID files.
        owned = subprocess.Popen(['/usr/bin/sleep', 'infinity'], start_new_session=True)
        unrelated = subprocess.Popen(['/usr/bin/sleep', '30'], start_new_session=True)
        try:
            with patch.object(manager, 'NETNS_PATH', Path('/proc/self/ns/net')):
                self.pid.write_text(str(unrelated.pid))
                self.assertIsNone(manager.pid_alive(self.pid))
                manager.kill_pid(self.pid)
                self.assertIsNone(unrelated.poll(), 'stale PID must not stop unrelated child')
                self.pid.write_text(str(owned.pid))
                deadline = time.monotonic() + 2
                while manager.pid_alive(self.pid) is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(manager.pid_alive(self.pid), owned.pid)
                manager.kill_pid(self.pid)
                self.assertEqual(owned.wait(timeout=2), -signal.SIGTERM)
                self.assertIsNone(unrelated.poll(), 'positive stop must remain scoped')
        finally:
            for child in (owned, unrelated):
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
