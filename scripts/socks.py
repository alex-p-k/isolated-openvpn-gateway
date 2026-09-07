#!/usr/bin/env python3
"""Run unprivileged Dante only while the VPN supervisor reports a live tunnel."""
import json
import os
import pathlib
import signal
import subprocess
import time

running = True
child = None

def stop(*_):
    global running
    running = False

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
generation = None
state_path = pathlib.Path(os.environ.get('GATEWAY_STATE', '/state')) / 'status.json'
sockd_config = os.environ.get('GATEWAY_SOCKD_CONFIG', '/etc/sockd.conf')
sockd_binary = os.environ.get('GATEWAY_SOCKD_BINARY', '/usr/sbin/sockd')
try:
    while running:
        try:
            state = json.loads(state_path.read_text())
            addr = subprocess.run(['ip', '-4', '-o', 'addr', 'show', 'dev', 'tun0'], capture_output=True, text=True)
            live = state.get('ready') and state.get('connected_at') and addr.returncode == 0 and 'inet ' in addr.stdout
        except (ValueError, OSError):
            state, live = {}, False
        new_generation = state.get('connected_at') if live else None
        if child and (not live or generation != new_generation or child.poll() is not None):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()
            child = None
        if live and child is None:
            # No request/target logs or raw resolver errors are persisted.
            child = subprocess.Popen([sockd_binary, '-f', sockd_config],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            generation = new_generation
        time.sleep(0.5)
finally:
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait()
