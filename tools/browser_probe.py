#!/usr/bin/env python3
"""Opt-in browser socket probe using only temporary profiles and loopback servers.

No corporate data or live gateway is used. This is NOT full DNS-leak/tun-loss
acceptance: it proves a real browser positive control, SOCKS DOMAIN requests and
no direct fallback after a SOCKS rejection. Run the remaining UX_ACCEPTANCE checks.
"""
import argparse
import contextlib
import http.server
import json
import os
from pathlib import Path
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import browser
import host
from product import ProductError, error_text


def receive(stream, count):
    data = b''
    while len(data) < count:
        chunk = stream.recv(count-len(data))
        if not chunk:
            raise OSError('probe connection closed')
        data += chunk
    return data


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


class Control(http.server.BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass  # A browser can cancel speculative/keep-alive control connections.

    def do_GET(self):
        self.server.hits.append(self.path)
        self.server.seen.set()
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'SYNTHETIC-DIRECT-CONTROL')

    def log_message(self, *_args):
        pass


class Socks(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(8)
            version, methods = receive(self.request, 2)
            if version != 5 or b'\x00' not in receive(self.request, methods):
                return
            self.request.sendall(b'\x05\x00')
            version, command, reserved, kind = receive(self.request, 4)
            if version != 5 or command != 1 or reserved:
                return
            length = receive(self.request, 1)[0] if kind == 3 else {1:4,4:16}.get(kind)
            if length is None:
                return
            address = receive(self.request, length)
            port = struct.unpack('!H', receive(self.request, 2))[0]
            name = address.decode('ascii') if kind == 3 else ''
            self.server.requests.append((kind, name, port))
            if name != 'gateway-probe.invalid' or port != 80:
                self.server.denied += 1
                self.request.sendall(b'\x05\x02\x00\x01'+b'\x00'*6)
                return
            self.request.sendall(b'\x05\x00\x00\x01'+b'\x00'*6)
            request = b''
            while b'\r\n\r\n' not in request and len(request) < 16384:
                request += receive(self.request, 1)
            path = request.split(b' ', 2)[1]
            if path.startswith(b'/result?'):
                self.server.result = path == b'/result?blocked=2'
                self.server.done.set()
                body = b'probe complete'
            else:
                self.server.positive = True
                body = ('''<!doctype html><meta charset="utf-8"><title>Gateway synthetic probe</title>
<script>
(async () => {
 let blocked = 0;
 for (const url of ["http://127.0.0.1:PORT/negative", "http://blocked-probe.invalid/negative"]) {
   try { await fetch(url, {cache: "no-store", mode: "no-cors"}); }
   catch (_) { blocked++; }
 }
 await fetch("/result?blocked=" + blocked, {cache: "no-store"});
})();
</script>'''.replace('PORT', str(self.server.control_port))).encode()
            self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nCache-Control: no-store\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
        except (OSError, ValueError, IndexError):
            pass


@contextlib.contextmanager
def serve(server):
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)


@contextlib.contextmanager
def launch(executable, profile, kind, url, proxy=None):
    host.private_directory(profile)
    options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env=dict(os.environ, MOZ_CRASHREPORTER_DISABLE='1'))
    if host.IS_WINDOWS:
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    if kind == 'firefox':
        preferences = browser.firefox_preferences(proxy or {'socks_host':'127.0.0.1','socks_port':9})
        if proxy is None:
            preferences['network.proxy.type'] = 0  # Deliberate synthetic positive control, never a user profile.
        host.private_write(profile/'user.js', ''.join('user_pref('+json.dumps(k)+', '+json.dumps(v)+');\n' for k,v in preferences.items()))
        args = [executable, '--headless', '-no-remote', '-profile', str(profile), url]
    else:
        flags = browser.browser_flags(proxy) if proxy else ['--no-proxy-server','--no-first-run','--no-default-browser-check']
        args = [executable, '--headless', '--user-data-dir='+str(profile), *flags, url]
    process = subprocess.Popen(args, **options)
    try:
        yield process
    finally:
        # Only the process handle created here. Never kill an existing browser
        # or enumerate user browser PIDs; both profiles are disposable and unique.
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def probe(kind):
    candidates = host.firefox_candidates() if kind == 'firefox' else host.browser_candidates()
    executable = next((str(p) for p in candidates if p.is_file()), None)
    if not executable:
        raise ProductError('BROWSER_MISSING', 'Selected probe browser is not installed.')
    version = browser.firefox_version(executable) if kind == 'firefox' else 'record installed Chromium version separately'
    with tempfile.TemporaryDirectory(prefix='gateway-browser-probe-') as directory:
        root = Path(directory)
        host.private_directory(root, exist_ok=True)
        control = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Control)
        control.hits, control.seen = [], threading.Event()
        with serve(control):
            port = control.server_address[1]
            with launch(executable, root/'direct-control', kind, f'http://127.0.0.1:{port}/positive'):
                positive = control.seen.wait(20)
            control.hits.clear()
            proxy = Server(('127.0.0.1', 0), Socks)
            proxy.requests, proxy.denied, proxy.positive = [], 0, False
            proxy.done, proxy.result, proxy.control_port = threading.Event(), False, port
            with serve(proxy):
                cfg = {'socks_host':'127.0.0.1','socks_port':proxy.server_address[1]}
                with launch(executable, root/'socks-control', kind, 'http://gateway-probe.invalid/', cfg) as process:
                    completed = proxy.done.wait(25)
                    alive = process.poll() is None
            result = dict(browser=kind, version=version, direct_positive_control=positive,
                          socks_payload_positive=proxy.positive,
                          domain_request_observed=any(k == 3 and n == 'gateway-probe.invalid' for k,n,_ in proxy.requests),
                          socks_rejection_observed=proxy.denied >= 2,
                          browser_reported_network_errors=completed and proxy.result and alive,
                          no_direct_control_hits=not any(x.startswith('/negative') for x in control.hits),
                          local_dns_observer='NOT_RUN', real_tun_loss='NOT_RUN', acceptance_complete=False)
            result['socket_probe_passed'] = all(result[key] for key in (
                'direct_positive_control','socks_payload_positive','domain_request_observed',
                'socks_rejection_observed','browser_reported_network_errors','no_direct_control_hits'))
        # Let owned child processes finish closing profile handles before temp cleanup.
        time.sleep(1)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', choices=('firefox','chromium'), required=True)
    args = parser.parse_args()
    try:
        result = probe(args.browser)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result['socket_probe_passed'] else 1)
    except (ProductError, OSError, subprocess.TimeoutExpired) as exc:
        print(error_text(exc), file=sys.stderr)
        sys.exit(1)
