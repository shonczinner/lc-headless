#!/usr/bin/env python3
"""Tiny web GUI for the fleet: start every account on a script, stop the fleet, and watch skill levels.

Reads status/<world>.json, which the C++ client writes every 5 minutes and on lifecycle changes.
Stdlib only: python3 tools/gui/server.py  ->  http://127.0.0.1:8765
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
GUI_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_DIR = os.path.join(ROOT, 'status')
LOGS_DIR = os.path.join(ROOT, 'logs')
PORT = int(os.environ.get('GUI_PORT', '8765'))

# Start presets: label -> (script file relative to scripts/, settings for the bot's namespace).
PRESETS = [
    ('Thiever — Men (suicide)', 'bots/thiever.py', {'Thiever': {'target': 'Man', 'suicide': True, 'banking': 'None', 'leash_radius': 10}}),
    ('WalkTo — Lumbridge', 'bots/walk_to.py', {'WalkTo': {'destination': 'Lumbridge', 'arrive_radius': 3}}),
    ('WanderPunch — combat', 'wander_punch.py', {}),
    ('AutoFighter — Guards', 'bots/auto_fighter.py', {'AutoFighter': {'target': 'Guard', 'leash_radius': 8}}),
]


def script_options():
    """Every preset, then each remaining script under scripts/ and scripts/bots/ with its defaults."""
    options = []
    seen = set()
    for label, path, settings in PRESETS:
        options.append({'label': label, 'file': path, 'settings': settings})
        seen.add(path)
    for pattern in ('scripts/bots/*.py', 'scripts/*.py'):
        for path in sorted(glob.glob(os.path.join(ROOT, pattern))):
            rel = os.path.relpath(path, os.path.join(ROOT, 'scripts')).replace(os.sep, '/')
            if os.path.basename(rel).startswith('_') or rel in seen or rel.startswith('typings/'):
                continue
            seen.add(rel)
            options.append({'label': rel, 'file': rel, 'settings': {}})
    return options


def read_status():
    """Merge every status file into one account list, newest write per account winning."""
    accounts = {}
    worlds = {}
    for path in sorted(glob.glob(os.path.join(STATUS_DIR, '*.json'))):
        try:
            with open(path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        world = data.get('world', '?')
        written = data.get('writtenAt', '')
        worlds[os.path.basename(path)] = {'writtenAt': written, 'world': world, 'accounts': len(data.get('accounts', []))}
        for account in data.get('accounts', []):
            entry = dict(account)
            entry['world'] = world
            entry['writtenAt'] = written
            accounts[entry.get('name', '?')] = entry
    return {'accounts': [accounts[name] for name in sorted(accounts, key=lambda n: (len(n), n))], 'files': worlds}


def fleet_state():
    """Running worlds, CPU and memory, from /proc."""
    pids = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open(f'/proc/{entry}/comm') as handle:
                if handle.read().strip() != 'Rs2004Headless':
                    continue
            with open(f'/proc/{entry}/cmdline', 'rb') as handle:
                cmd = handle.read().replace(b'\0', b' ').decode(errors='replace')
        except OSError:
            continue
        if 'Rs2004Headless' in cmd:
            pids.append(int(entry))
    cpu = 0.0
    rss = 0
    for pid in pids:
        try:
            with open(f'/proc/{pid}/stat') as handle:
                fields = handle.read().split()
            cpu += (int(fields[13]) + int(fields[14])) / os.sysconf('SC_CLK_TCK')
            with open(f'/proc/{pid}/status') as handle:
                for line in handle:
                    if line.startswith('VmRSS:'):
                        rss += int(line.split()[1])
        except (OSError, IndexError, ValueError):
            continue
    fleets = [p for p in subprocess.run(['pgrep', '-f', 'fleet.sh'], capture_output=True, text=True).stdout.split() if p]
    return {'running': bool(pids), 'worlds': len(pids), 'cpuSeconds': round(cpu, 1), 'rssMb': rss // 1024, 'starting': bool(fleets)}


def account_files():
    return sorted(glob.glob(os.path.join(ROOT, 'accounts_w*', '*.jsonc')))


def apply_script(script_file, settings):
    updated = 0
    for path in account_files():
        try:
            with open(path) as handle:
                account = json.load(handle)
        except (OSError, ValueError):
            continue
        account['script'] = {
            'file': script_file,
            'settings': settings,
            'progressReportMinutes': 5,
        }
        with open(path, 'w') as handle:
            handle.write(json.dumps(account) + '\n')
        updated += 1
    return updated


def run(cmd, log_name):
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = os.path.join(LOGS_DIR, log_name)
    with open(log_path, 'ab') as handle:
        # A fleet restart may kill our own process group, so detach into a new session.
        subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                         start_new_session=True)
    return log_path


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter console
        pass

    def _send(self, code, body, content_type='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            with open(os.path.join(GUI_DIR, 'index.html'), 'rb') as handle:
                self._send(200, handle.read(), 'text/html; charset=utf-8')
        elif path == '/api/status':
            self._send(200, read_status())
        elif path == '/api/scripts':
            self._send(200, {'options': script_options()})
        elif path == '/api/fleet':
            self._send(200, fleet_state())
        elif path == '/api/log':
            try:
                with open(os.path.join(LOGS_DIR, 'world1.log'), 'rb') as handle:
                    handle.seek(0, os.SEEK_END)
                    size = handle.tell()
                    handle.seek(max(0, size - 20000))
                    self._send(200, {'text': handle.read().decode(errors='replace')})
            except OSError:
                self._send(200, {'text': ''})
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get('Content-Length', '0'))
        body = json.loads(self.rfile.read(length) or b'{}')
        if path == '/api/start':
            script_file = body.get('file', '')
            settings = body.get('settings', {})
            if not script_file:
                self._send(400, {'error': 'no script'})
                return
            updated = apply_script(script_file, settings)
            run(['sh', 'stop.sh'], 'gui-fleet.log')
            time.sleep(2)
            log = run(['sh', 'fleet.sh'], 'gui-fleet.log')
            self._send(200, {'accounts': updated, 'script': script_file, 'log': log, 'state': 'starting'})
        elif path == '/api/stop':
            subprocess.run(['sh', 'stop.sh'], cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True)
            time.sleep(2)
            subprocess.run(['pkill', '-9', '-f', 'Rs2004Headless'], stdin=subprocess.DEVNULL, capture_output=True)
            self._send(200, {'state': 'stopped'})
        else:
            self._send(404, {'error': 'not found'})


def main():
    os.makedirs(STATUS_DIR, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'GUI on http://127.0.0.1:{PORT}  (fleet root {ROOT})')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
