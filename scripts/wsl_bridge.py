#!/usr/bin/python3
"""Relay one Windows forwarder stream to loopback-only Dante inside WSL."""
import os
import select
import socket
import sys


PORT = 11080


def main():
    try:
        target = socket.create_connection(('127.0.0.1', PORT), timeout=8)
        target.settimeout(None)
        source_fd = sys.stdin.buffer.fileno()
        output_fd = sys.stdout.buffer.fileno()
        os.set_blocking(source_fd, False)
        while True:
            readable, _, _ = select.select([source_fd, target], [], [], 30)
            if not readable:
                continue
            if source_fd in readable:
                data = os.read(source_fd, 65536)
                if not data:
                    with target:
                        target.shutdown(socket.SHUT_WR)
                    break
                target.sendall(data)
            if target in readable:
                data = target.recv(65536)
                if not data:
                    break
                os.write(output_fd, data)
        return 0
    except OSError:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
