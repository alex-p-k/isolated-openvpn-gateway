#!/usr/bin/env python3
"""Loopback-only Windows TCP forwarder into a managed WSL stdio bridge."""
from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading


running = True


def stop(*_):
    global running
    running = False


def relay(client, command):
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, bufsize=0)

        def copy_socket_to_process():
            try:
                while True:
                    data = client.recv(65536)
                    if not data:
                        break
                    process.stdin.write(data)
                    process.stdin.flush()
            except (OSError, BrokenPipeError):
                pass
            finally:
                with contextlib.suppress(OSError, BrokenPipeError):
                    process.stdin.close()

        sender = threading.Thread(target=copy_socket_to_process, daemon=True)
        sender.start()
        while True:
            data = process.stdout.read(65536)
            if not data:
                break
            client.sendall(data)
        sender.join(timeout=1)
    except (OSError, BrokenPipeError):
        pass
    finally:
        with contextlib.suppress(OSError):
            client.shutdown(socket.SHUT_RDWR)
        client.close()
        if process and process.poll() is None:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
            if process.poll() is None:
                process.kill()


def serve(port, distro, pid_file):
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    command = ['wsl.exe', '--distribution', distro, '--user', 'root', '--exec',
               '/usr/bin/python3', '/opt/isolated-openvpn-gateway/wsl_manager.py', 'bridge']
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == 'nt':
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', port))
    listener.listen(64)
    listener.settimeout(1)
    Path(pid_file).write_text(str(os.getpid()), encoding='ascii')
    try:
        while running:
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=relay, args=(client, command), daemon=True).start()
    finally:
        listener.close()
        Path(pid_file).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--distro', required=True)
    parser.add_argument('--pid-file', required=True, type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or args.distro != 'IsolatedOpenVPNGateway':
        return 2
    serve(args.port, args.distro, args.pid_file)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
