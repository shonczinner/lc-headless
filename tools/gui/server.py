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


HISTORY_PATH = os.path.join(STATUS_DIR, 'history.json')
# Snapshot history kept per account: enough for a few days of 5-minute samples.
HISTORY_POINTS = 500
# An XP/hr window uses at most this many recent samples, so a stale gap doesn't skew it.
RATE_SAMPLES = 12


def _now_epoch():
    return time.time()


def _parse_time(text):
    try:
        return time.mktime(time.strptime(text, '%Y-%m-%dT%H:%M:%SZ')) - time.timezone
    except (TypeError, ValueError):
        return None


def _load_history():
    try:
        with open(HISTORY_PATH) as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_history(history):
    try:
        temporary = HISTORY_PATH + '.tmp'
        with open(temporary, 'w') as handle:
            json.dump(history, handle)
        os.replace(temporary, HISTORY_PATH)
    except OSError:
        pass


def _record_history(history, account, xp_by_skill, stamp):
    """Appends one snapshot per account per writtenAt, so repeated polls don't duplicate points."""
    name = account.get('name', '?')
    points = history.setdefault(name, [])
    if points and points[-1].get('t') == stamp:
        return
    points.append({'t': stamp, 'xp': xp_by_skill})
    if len(points) > HISTORY_POINTS:
        del points[:len(points) - HISTORY_POINTS]


def _xp_per_hour(points):
    """XP/hr per skill over the last RATE_SAMPLES samples, compared with the oldest in the window.

    Needs at least two samples; a fresh login starts again."""
    if len(points) < 2:
        return {}
    # Why: a relogin resets or rewinds XP (deaths, a different account state), so compare only the most
    # recent run of samples where every skill is non-decreasing: a monotonic tail per account.
    last = points[-1]
    end = last.get('xp', {})
    in_window = points[-RATE_SAMPLES:] if len(points) > RATE_SAMPLES else points
    first = None
    for candidate in reversed(in_window[:-1]):
        start = candidate.get('xp', {})
        monotonic = all(end.get(skill, 0) >= start.get(skill, 0) for skill in end)
        if monotonic:
            first = candidate
        else:
            break
    if first is None:
        return {}

    # Skip trailing samples that show no gain, so time spent logged out or idle before the current
    # burst doesn't water the rate down. The cut-off still has to be monotonic.
    baseline = first
    for candidate in in_window[:-1]:
        if candidate['t'] <= first['t']:
            continue
        gain = any(end.get(skill, 0) > candidate.get('xp', {}).get(skill, 0) for skill in end)
        if gain:
            baseline = candidate
            break
    first = baseline

    hours = (last['t'] - first['t']) / 3600.0
    if hours <= 10 / 3600.0:
        return {}

    rates = {}
    for skill, xp in end.items():
        gain = xp - first.get('xp', {}).get(skill, 0)
        if gain > 0:
            rates[skill] = round(gain / hours)
    return rates


def read_status():
    """Merge every status file into one account list, newest write per account winning.

    Also records the snapshot in status/history.json and adds each skill's xp/hr."""
    accounts = {}
    worlds = {}
    history = _load_history()
    recorded = False

    for path in sorted(glob.glob(os.path.join(STATUS_DIR, '*.json'))):
        if os.path.basename(path) == 'history.json':
            continue
        try:
            with open(path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        world = data.get('world', '?')
        written = data.get('writtenAt', '')
        stamp = _parse_time(written) or _now_epoch()
        worlds[os.path.basename(path)] = {'writtenAt': written, 'world': world, 'accounts': len(data.get('accounts', []))}
        for account in data.get('accounts', []):
            entry = dict(account)
            entry['world'] = world
            entry['writtenAt'] = written
            skills = entry.get('skills') or {}
            _record_history(history, entry, {name: skill.get('xp', 0) for name, skill in skills.items()}, stamp)
            recorded = True
            accounts[entry.get('name', '?')] = entry

    if recorded:
        _save_history(history)

    merged = []
    for name in sorted(accounts, key=lambda n: (len(n), n)):
        entry = accounts[name]
        entry['xpPerHour'] = _xp_per_hour(history.get(name, []))
        merged.append(entry)
    return {'accounts': merged, 'files': worlds}


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
