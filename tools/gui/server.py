#!/usr/bin/env python3
"""A small local web page for reading a fleet's status and bank logs, and optionally controlling it.

    python3 tools/gui/server.py                       # view only, http://127.0.0.1:8765
    python3 tools/gui/server.py --clients client_w1.jsonc client_w2.jsonc
                                                      # ... and Start/Stop drive those clients

Nothing here runs unless you start it: the client never launches this server, it binds to 127.0.0.1
only, and Ctrl+C stops it. The view reads the status/<name>.json and bank/<account>.json files the
client writes (see docs/Tooling.md); it starts no process until Start is pressed, and only with
--clients. Stdlib only.
"""

import argparse
import glob
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
GUI_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_DIR = os.path.join(ROOT, 'status')
BANK_DIR = os.path.join(ROOT, 'bank')
LOGS_DIR = os.path.join(ROOT, 'logs')
PORT = int(os.environ.get('GUI_PORT', '8765'))

# Scripts offered first in the dropdown, with the settings the bot needs to be useful out of the box.
PRESETS = [
    ('Thiever — Men (suicide)', 'bots/thiever.py', {'Thiever': {'target': 'Man', 'suicide': True, 'banking': 'None', 'leash_radius': 10}}),
    ('WalkTo — Lumbridge', 'bots/walk_to.py', {'WalkTo': {'destination': 'Lumbridge', 'arrive_radius': 3}}),
    ('AutoFighter — Guards', 'bots/auto_fighter.py', {'AutoFighter': {'target': 'Guard', 'leash_radius': 8}}),
]

# Snapshots kept per account session: enough for a few days of 5-minute samples.
HISTORY_POINTS = 500
# A rate needs at least two periodic reads this far apart; the client writes every 5 minutes.
MIN_RATE_SECONDS = 4 * 60
# An XP/hr window: consecutive periodic reads, newest first, up to this many.
RATE_SAMPLES = 18


# ---- configuration the page reads ----


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


def account_files():
    """Every account file the configured client configs load, or accounts/*.jsonc without --clients."""
    folders = []
    for config_path in CLIENTS:
        try:
            with open(config_path) as handle:
                text = handle.read()
        except OSError:
            continue
        # client.jsonc has comments; the one key needed is quoted and on its own line.
        for line in text.splitlines():
            if '"accountsDirectory"' in line and ':' in line:
                value = line.split(':', 1)[1].strip().strip(',').strip().strip('"')
                folders.append(value)
    if not folders:
        folders = ['accounts']
    files = []
    for folder in folders:
        files.extend(sorted(glob.glob(os.path.join(ROOT, folder, '*.jsonc'))))
    return files


def apply_script(script_file, settings):
    updated = 0
    for path in account_files():
        try:
            with open(path) as handle:
                account = json.load(handle)
        except (OSError, ValueError):
            continue
        account['script'] = {'file': script_file, 'settings': settings}
        with open(path, 'w') as handle:
            handle.write(json.dumps(account) + '\n')
        updated += 1
    return updated


# ---- status readers ----


def _now_epoch():
    return time.time()


def _parse_time(text):
    try:
        return time.mktime(time.strptime(text, '%Y-%m-%dT%H:%M:%SZ')) - time.timezone
    except (TypeError, ValueError):
        return None


HISTORY_PATH = os.path.join(STATUS_DIR, 'history.json')


def _load_history():
    """{account: {'loginCount': n, 'points': [{'t', 'xp'}]}}."""
    try:
        with open(HISTORY_PATH) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: entry for name, entry in data.items() if isinstance(entry, dict) and 'points' in entry}


def _save_history(history):
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        temporary = HISTORY_PATH + '.tmp'
        with open(temporary, 'w') as handle:
            json.dump(history, handle)
        os.replace(temporary, HISTORY_PATH)
    except OSError:
        pass


def _record_history(history, account, stamp, periodic):
    """Appends one point per periodic snapshot; lifecycle writes and unscheduled polls are ignored,
    because extrapolating a 30-second gap to an hour is meaningless."""
    if not periodic:
        return
    name = account.get('name', '?')
    login_count = account.get('loginCount', 0)
    entry = history.get(name)
    # A relogin starts a new session: old points would read as a rate across the break.
    if entry is None or entry.get('loginCount') != login_count:
        entry = {'loginCount': login_count, 'points': []}
        history[name] = entry
    points = entry['points']
    if points and points[-1].get('t') == stamp:
        return
    points.append({'t': stamp, 'xp': {k: v.get('xp', 0) for k, v in (account.get('skills') or {}).items()}})
    if len(points) > HISTORY_POINTS:
        del points[:len(points) - HISTORY_POINTS]


def _xp_per_hour(entry):
    """XP/hr per skill over the session's periodic reads. Empty until two reads at least
    MIN_RATE_SECONDS apart exist, and after a rewind (a relogin with lower XP) it starts again."""
    points = (entry or {}).get('points') or []
    if len(points) < 2:
        return {}

    last = points[-1]
    skills = last.get('xp', {})
    min_span = MIN_RATE_SECONDS / 3600.0

    start_index = 0
    for index in range(len(points) - 1):
        before = points[index].get('xp', {})
        after = points[index + 1].get('xp', {})
        if any(after.get(skill, 0) < before.get(skill, 0) for skill in skills):
            start_index = index + 1

    window = points[start_index:][-RATE_SAMPLES:]
    if len(window) < 2:
        return {}

    # Start at the first read of the latest gain run, so idle time before it isn't averaged in.
    first = window[0]
    for index in range(len(window) - 1):
        before = window[index].get('xp', {})
        after = window[index + 1].get('xp', {})
        if any(after.get(skill, 0) > before.get(skill, 0) for skill in skills):
            first = window[index]
            break

    hours = (last['t'] - first['t']) / 3600.0
    if hours < min_span:
        return {}

    rates = {}
    for skill, xp in skills.items():
        gain = xp - first.get('xp', {}).get(skill, 0)
        if gain > 0:
            rates[skill] = round(gain / hours)
    return rates


def read_status():
    """Merge every status file into one account list, newest write per account winning, with xp/hr."""
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
        periodic = data.get('kind') == 'period'
        stamp = _parse_time(written) or _now_epoch()
        worlds[os.path.basename(path)] = {'writtenAt': written, 'world': world, 'periodic': periodic,
                                          'accounts': len(data.get('accounts', []))}
        for account in data.get('accounts', []):
            entry = dict(account)
            entry['world'] = world
            entry['writtenAt'] = written
            _record_history(history, entry, stamp, periodic)
            recorded = periodic or recorded
            accounts[entry.get('name', '?')] = entry

    if recorded:
        _save_history(history)

    merged = []
    for name in sorted(accounts, key=lambda n: (len(n), n)):
        entry = accounts[name]
        session = history.get(name)
        if session is not None and session.get('loginCount') != entry.get('loginCount', 0):
            session = None
        entry['xpPerHour'] = _xp_per_hour(session)
        entry['ratePoints'] = len((session or {}).get('points') or [])
        merged.append(entry)
    return {'accounts': merged, 'files': worlds}


def bank_totals():
    """Sums the latest bank snapshot per account, by item id."""
    items = {}
    snapshots = []
    for path in sorted(glob.glob(os.path.join(BANK_DIR, '*.json'))):
        try:
            with open(path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        account = data.get('account') or os.path.splitext(os.path.basename(path))[0]
        snapshots.append({'account': account, 'time': data.get('time', ''), 'world': data.get('world', '')})
        for item in data.get('items', []):
            ident = int(item.get('id', -1))
            entry = items.setdefault(ident, {'id': ident, 'name': item.get('name') or '', 'qty': 0, 'accounts': []})
            entry['qty'] += int(item.get('qty', 0))
            if not entry['name'] and item.get('name'):
                entry['name'] = item['name']
            entry['accounts'].append(account)
    ordered = sorted(items.values(), key=lambda e: (-e['qty'], e['id']))
    for entry in ordered:
        entry['accounts'] = sorted(set(entry['accounts']))
        entry['accountCount'] = len(entry['accounts'])
    return {'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'accounts': len(snapshots), 'items': ordered, 'snapshots': snapshots}


# ---- the fleet: read-only unless --clients ----


def _clients():
    result = subprocess.run(['pgrep', '-f', 'Rs2004Headless'], capture_output=True, text=True)
    return [int(p) for p in result.stdout.split() if p.isdigit()]


def fleet_state():
    pids = _clients()
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
    return {'running': bool(pids), 'worlds': len(pids), 'cpuSeconds': round(cpu, 1),
            'rssMb': rss // 1024, 'control': bool(CLIENTS)}


STATE_LOCK = threading.Lock()
STATE = {'status': 'idle', 'since': time.time(), 'startPid': None, 'detail': ''}
PROCESSES = []


def _set_state(status, detail=''):
    STATE['status'] = status
    STATE['since'] = time.time()
    STATE['detail'] = detail


def _wait_for_client(deadline):
    """True once every configured client is running."""
    while time.time() < deadline:
        if len(_clients()) >= len(CLIENTS):
            return True
        time.sleep(0.25)
    return len(_clients()) >= len(CLIENTS)


def _start(script_file, settings):
    if not CLIENTS:
        return 403, {'error': 'read-only: start with --clients <client.jsonc> ... to control the fleet'}
    updated = apply_script(script_file, settings)
    with STATE_LOCK:
        if STATE['status'] in ('starting', 'stopping'):
            return 409, {'error': f"busy: {STATE['status']}; stop first", 'state': STATE['status']}
        _set_state('starting', f'{script_file} on {updated} accounts')
        # One process per client config, each in its own group, so Stop can end them all.
        for config_path in CLIENTS:
            PROCESSES.append(subprocess.Popen(['Rs2004Headless', os.path.basename(config_path), '--raw-console'],
                                              cwd=ROOT, stdin=subprocess.DEVNULL,
                                              stdout=open(os.path.join(LOGS_DIR, os.path.basename(config_path) + '.log'), 'ab'),
                                              stderr=subprocess.STDOUT, start_new_session=True))
        threading.Thread(target=_watch_start, daemon=True).start()
    return 200, {'accounts': updated, 'script': script_file, 'state': 'starting', 'clients': len(CLIENTS)}


def _watch_start():
    if _wait_for_client(time.time() + 300):
        with STATE_LOCK:
            if STATE['status'] == 'starting':
                _set_state('running', f'{len(CLIENTS)} clients')
    else:
        with STATE_LOCK:
            if STATE['status'] == 'starting':
                _set_state('idle', 'clients did not all start')


def _stop():
    if not CLIENTS:
        return 403, {'error': 'read-only: start with --clients <client.jsonc> ... to control the fleet'}
    with STATE_LOCK:
        _set_state('stopping', 'stop requested')
        for process in PROCESSES:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        PROCESSES.clear()
    deadline = time.time() + 35
    while time.time() < deadline and _clients():
        time.sleep(0.5)
    for pid in _clients():
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    with STATE_LOCK:
        _set_state('idle' if not _clients() else 'stopping', 'stopped')
    return 200, {'state': 'idle' if not _clients() else 'stopping', 'remaining': len(_clients())}


# ---- HTTP ----


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
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
            self._send(200, {'options': script_options(), 'control': bool(CLIENTS)})
        elif path == '/api/fleet':
            state = fleet_state()
            state['state'] = STATE['status']
            state['detail'] = STATE['detail']
            self._send(200, state)
        elif path == '/api/bank':
            self._send(200, bank_totals())
        elif path == '/api/log':
            log = os.path.join(LOGS_DIR, (os.path.basename(CLIENTS[0]) + '.log') if CLIENTS else 'world1.log')
            try:
                with open(log, 'rb') as handle:
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
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            body = {}
        if path == '/api/start':
            script_file = body.get('file', '')
            if not script_file:
                self._send(400, {'error': 'no script'})
                return
            code, payload = _start(script_file, body.get('settings', {}))
            self._send(code, payload)
        elif path == '/api/stop':
            code, payload = _stop()
            self._send(code, payload)
        else:
            self._send(404, {'error': 'not found'})


def parse_args():
    parser = argparse.ArgumentParser(description='Local status and bank view for lc-headless, with optional control.')
    parser.add_argument('--clients', nargs='+', metavar='CONFIG', default=[],
                        help='client.jsonc files Start runs and Stop ends; without them the page is read-only')
    parser.add_argument('--port', type=int, default=int(os.environ.get('GUI_PORT', '8765')))
    return parser.parse_args()


CLIENTS = []


def main():
    global CLIENTS, PORT
    args = parse_args()
    CLIENTS = [os.path.abspath(path) for path in args.clients]
    PORT = args.port
    os.makedirs(STATUS_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    mode = f'control: {", ".join(os.path.basename(c) for c in CLIENTS)}' if CLIENTS else 'read-only'
    print(f'GUI on http://127.0.0.1:{PORT}  ({mode})')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    sys.exit(main())
