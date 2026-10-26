#!/usr/bin/env python3
"""Tiny web GUI for the fleet: start every account on a script, stop the fleet, and watch skill levels.

Reads status/<world>.json, which the C++ client writes every 5 minutes and on lifecycle changes.
Stdlib only: python3 tools/gui/server.py  ->  http://127.0.0.1:8765
"""

import glob
import json
import os
import shutil
import signal
import subprocess
import threading
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
# Snapshots kept per account session: enough for a few days of 5-minute samples.
HISTORY_POINTS = 500
# A rate needs at least two periodic reads this far apart; the client writes every 5 minutes.
MIN_RATE_SECONDS = 4 * 60
# An XP/hr window: consecutive scheduled reads, newest first, up to this many.
RATE_SAMPLES = 18


def _now_epoch():
    return time.time()


def _parse_time(text):
    try:
        return time.mktime(time.strptime(text, '%Y-%m-%dT%H:%M:%SZ')) - time.timezone
    except (TypeError, ValueError):
        return None


def _load_history():
    """History v2: {account: {'loginCount': n, 'points': [{'t', 'xp'}]}}."""
    try:
        with open(HISTORY_PATH) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Drop v1 entries ({account: [points]}), whose points came from forced writes.
    return {name: entry for name, entry in data.items() if isinstance(entry, dict) and 'points' in entry}


def _save_history(history):
    try:
        temporary = HISTORY_PATH + '.tmp'
        with open(temporary, 'w') as handle:
            json.dump(history, handle)
        os.replace(temporary, HISTORY_PATH)
    except OSError:
        pass


def _record_history(history, account, stamp, scheduled):
    """Appends one point per scheduled snapshot. Forced (lifecycle) writes and unscheduled polls are
    ignored, because extrapolating a 30-second gap to an hour is meaningless."""
    if not scheduled:
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
    """XP/hr per skill over the session's scheduled reads.

    Needs two scheduled reads at least MIN_RATE_SECONDS apart. The window runs from the first read of
    the current gain run to the newest, so an idle stretch or a relogin (a rewind) can't distort it.
    Empty until there's enough history."""
    points = (entry or {}).get('points') or []
    if len(points) < 2:
        return {}

    last = points[-1]
    skills = last.get('xp', {})
    min_span = MIN_RATE_SECONDS / 3600.0

    # A rewind breaks the run; anything after the last rewind is one continuous session.
    start_index = 0
    for index in range(len(points) - 1):
        before = points[index].get('xp', {})
        after = points[index + 1].get('xp', {})
        if any(after.get(skill, 0) < before.get(skill, 0) for skill in skills):
            start_index = index + 1

    window = points[start_index:][-RATE_SAMPLES:]
    if len(window) < 2:
        return {}

    # Start at the first read of the latest gain run: a read before a flat stretch that only resumes
    # later would average in the idle time.
    first = window[0]
    for index in range(len(window) - 1):
        before = window[index].get('xp', {})
        after = window[index + 1].get('xp', {})
        if any(after.get(skill, 0) > before.get(skill, 0) for skill in skills):
            first = window[index]
            break

    last = window[-1]
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
    """Merge every status file into one account list, newest write per account winning.

    Scheduled snapshots are recorded in status/history.json; each account gets xp/hr per skill once two
    scheduled reads exist."""
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
        # Only the periodic write feeds rates; lifecycle ('event') writes just refresh the table.
        scheduled = data.get('kind') == 'period' if 'kind' in data else bool(data.get('scheduled', False))
        stamp = _parse_time(written) or _now_epoch()
        worlds[os.path.basename(path)] = {'writtenAt': written, 'world': world, 'scheduled': scheduled,
                                          'accounts': len(data.get('accounts', []))}
        for account in data.get('accounts', []):
            entry = dict(account)
            entry['world'] = world
            entry['writtenAt'] = written
            _record_history(history, entry, stamp, scheduled)
            recorded = scheduled or recorded
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
    """Sums the per-account bank snapshots the client writes on every bank open."""
    bank_dir = os.path.join(ROOT, 'bank')
    items = {}
    snapshots = []
    for path in sorted(glob.glob(os.path.join(bank_dir, '*.json'))):
        if os.path.basename(path) == 'totals.json':
            continue
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


START_LOG = os.path.join(LOGS_DIR, 'gui-fleet.log')

# idle -> starting -> running; stopping from any state. The lock keeps one transition at a time.
STATE_LOCK = threading.Lock()
STATE = {'status': 'idle', 'since': time.time(), 'startPid': None, 'detail': ''}


def _set_state(status, detail=''):
    STATE['status'] = status
    STATE['since'] = time.time()
    STATE['detail'] = detail


def _kill_group(pid, timeout=5.0):
    """SIGTERM a process group, then SIGKILL whatever survives; returns True when nothing is left."""
    if not pid:
        return True
    for sig, wait in ((signal.SIGTERM, timeout), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
    return False


def _clients():
    result = subprocess.run(['pgrep', '-f', 'Rs2004Headless'], capture_output=True, text=True)
    return [int(p) for p in result.stdout.split() if p.isdigit()]


def _start_fleet(script_file, settings):
    os.makedirs(LOGS_DIR, exist_ok=True)
    updated = apply_script(script_file, settings)
    with STATE_LOCK:
        if STATE['status'] in ('starting', 'stopping'):
            return 409, {'error': f"busy: {STATE['status']}; stop first", 'state': STATE['status']}
        _set_state('starting', f'{script_file} on {updated} accounts')
        with open(START_LOG, 'ab') as handle:
            # start_new_session: fleet.sh, its start-world.sh children and the clients share one group,
            # so Stop can end the whole start at once.
            process = subprocess.Popen(['sh', 'fleet.sh'], cwd=ROOT, stdin=subprocess.DEVNULL,
                                       stdout=handle, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        STATE['startPid'] = os.getpgid(process.pid)
        threading.Thread(target=_watch_start, args=(process,), daemon=True).start()
    return 200, {'accounts': updated, 'script': script_file, 'state': 'starting', 'pid': process.pid}


def _watch_start(process):
    process.wait()
    with STATE_LOCK:
        if STATE['status'] == 'starting':
            _set_state('running' if _clients() else 'idle', f'fleet.sh exited {process.returncode}')


def _stop_fleet():
    with STATE_LOCK:
        was = STATE['status']
        _set_state('stopping', f'was {was}')
        # Kill the start process group first: fleet.sh, start-world.sh and anything they spawned.
        start_pid = STATE['startPid']
        STATE['startPid'] = None
        _kill_group(start_pid, timeout=3.0)
        # Fleet scripts are launched with start_new_session, so their own groups need hunting down.
        subprocess.run(['pkill', '-TERM', '-f', r'[s]h fleet\.sh'], capture_output=True)
        subprocess.run(['pkill', '-TERM', '-f', r'[s]tart-world\.sh'], capture_output=True)
        subprocess.run(['sh', 'stop.sh'], cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True)
    time.sleep(2)
    # Whatever is left after the graceful logout.
    for _ in range(3):
        remaining = _clients()
        if not remaining:
            break
        subprocess.run(['pkill', '-KILL', '-f', 'Rs2004Headless'], capture_output=True)
        time.sleep(1)
    with STATE_LOCK:
        _set_state('idle' if not _clients() else 'stopping', 'stop requested')
    return 200, {'state': 'idle' if not _clients() else 'stopping', 'remaining': len(_clients())}


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
            state = fleet_state()
            state['state'] = STATE['status']
            state['detail'] = STATE['detail']
            self._send(200, state)
        elif path == '/api/bank':
            self._send(200, bank_totals())
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
            code, payload = _start_fleet(script_file, settings)
            self._send(code, payload)
        elif path == '/api/stop':
            code, payload = _stop_fleet()
            self._send(code, payload)
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
