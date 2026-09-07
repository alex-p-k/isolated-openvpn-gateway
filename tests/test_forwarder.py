"""Background bridge launches must preserve binary pipes without new consoles."""
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('test_loopback_forwarder', ROOT/'scripts/loopback_forwarder.py')
forwarder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(forwarder)


class ForwarderLaunchTests(unittest.TestCase):
    def test_windows_bridge_is_hidden_and_posix_flags_remain_zero(self):
        command = ['wsl.exe', '--distribution', 'IsolatedOpenVPNGateway', '--exec', '/path with spaces/bridge']
        for system, expected in (('nt', 0x08000000), ('posix', 0)):
            with self.subTest(system=system):
                client = Mock()
                with patch.object(forwarder.os, 'name', system), \
                     patch.object(forwarder.subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True), \
                     patch.object(forwarder.subprocess, 'Popen', side_effect=OSError('controlled launch failure')) as launch:
                    forwarder.relay(client, command)
                self.assertEqual(launch.call_args.args, (command,))
                self.assertEqual(launch.call_args.kwargs['creationflags'], expected)
                self.assertEqual(launch.call_args.kwargs['stdin'], subprocess.PIPE)
                self.assertEqual(launch.call_args.kwargs['stdout'], subprocess.PIPE)
                self.assertNotIn('shell', launch.call_args.kwargs)
                client.close.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'requires the Windows console API')
    def test_real_windows_child_has_no_console_and_relays_binary_payload(self):
        # Exercise the same pipe launch as wsl.exe without changing a WSL distro
        # or opening a corporate connection. This checks the actual console API.
        code = ('import ctypes,sys; '
                'ctypes.windll.kernel32.GetConsoleWindow.restype=ctypes.c_void_p; '
                'assert not ctypes.windll.kernel32.GetConsoleWindow(); '
                'data=sys.stdin.buffer.read(5); sys.stdout.buffer.write(data); sys.stdout.buffer.flush()')
        client, accepted = socket.socketpair()
        client.settimeout(10)
        worker = threading.Thread(target=forwarder.relay, args=(accepted, [sys.executable, '-c', code]), daemon=True)
        try:
            worker.start()
            payload = b'\x00\xff\r\nX'
            client.sendall(payload)
            result = bytearray()
            while len(result) < len(payload):
                part = client.recv(len(payload) - len(result))
                if not part:
                    break
                result.extend(part)
            self.assertEqual(bytes(result), payload)
            client.shutdown(socket.SHUT_WR)
        finally:
            client.close()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
