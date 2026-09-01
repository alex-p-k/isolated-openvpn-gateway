#!/usr/bin/env python3
"""Minimal cross-platform SOCKS5 stdio bridge for OpenSSH ProxyCommand."""
import ipaddress
import socket
import struct
import sys
import threading


class ProxyError(OSError):
    pass


def receive(sock, length):
    data = b''
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ProxyError('SOCKS5 proxy closed the connection.')
        data += chunk
    return data


def address_bytes(hostname):
    try:
        address = ipaddress.ip_address(hostname)
        return (b'\x01' + address.packed) if address.version == 4 else (b'\x04' + address.packed)
    except ValueError:
        value = hostname.encode('idna')
        if not value or len(value) > 255 or b'\x00' in value:
            raise ProxyError('Invalid target hostname.') from None
        return b'\x03' + bytes([len(value)]) + value


def negotiate(sock, hostname, port):
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ProxyError('Invalid target port.')
    sock.sendall(b'\x05\x01\x00')
    if receive(sock, 2) != b'\x05\x00':
        raise ProxyError('SOCKS5 no-authentication method was rejected.')
    sock.sendall(b'\x05\x01\x00' + address_bytes(hostname) + struct.pack('!H', port))
    header = receive(sock, 4)
    if header[:3] != b'\x05\x00\x00':
        code = header[1] if len(header) > 1 else 255
        raise ProxyError('SOCKS5 CONNECT failed with code %d.' % code)
    sizes = {1: 4, 4: 16}
    size = sizes.get(header[3])
    if header[3] == 3:
        size = receive(sock, 1)[0]
    if size is None:
        raise ProxyError('SOCKS5 returned an invalid address type.')
    receive(sock, size + 2)


def relay(sock, source, destination):
    errors = []

    def upload():
        try:
            while True:
                data = source.read(65536)
                if not data:
                    break
                sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
        except (OSError, ValueError) as exc:
            errors.append(exc)

    thread = threading.Thread(target=upload, daemon=True)
    thread.start()
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            destination.write(data)
            destination.flush()
    finally:
        thread.join(timeout=2)
    if errors:
        raise ProxyError('SOCKS5 upload failed.') from errors[0]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 4:
        print('Usage: socks_connect.py PROXY_HOST PROXY_PORT TARGET_HOST TARGET_PORT', file=sys.stderr)
        return 2
    proxy_host, proxy_port, target_host, target_port = argv
    try:
        proxy_address = ipaddress.ip_address(proxy_host)
        if not proxy_address.is_loopback:
            raise ProxyError('Only a loopback SOCKS5 proxy is accepted.')
        with socket.create_connection((str(proxy_address), int(proxy_port)), timeout=12) as sock:
            sock.settimeout(None)
            negotiate(sock, target_host, int(target_port))
            relay(sock, sys.stdin.buffer, sys.stdout.buffer)
        return 0
    except (OSError, ValueError) as exc:
        print('SOCKS5 ProxyCommand failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
