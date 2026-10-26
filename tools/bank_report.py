#!/usr/bin/env python3
"""Sum the bank snapshots the client writes on every bank open.

    python3 tools/bank_report.py            # write bank/totals.json and bank/totals.log once
    python3 tools/bank_report.py --watch    # refresh every 60 s

The client writes bank/<account>.json whenever a bank is opened, so these totals are "as last seen
with the bank open", not live while an account is banking.
"""

import argparse
import glob
import json
import os
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
BANK_DIR = os.path.join(ROOT, 'bank')


def read_snapshots():
    snapshots = []
    for path in sorted(glob.glob(os.path.join(BANK_DIR, '*.json'))):
        if os.path.basename(path) in ('totals.json',):
            continue
        try:
            with open(path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        account = data.get('account') or os.path.splitext(os.path.basename(path))[0]
        snapshots.append({
            'account': account,
            'world': data.get('world', ''),
            'time': data.get('time', ''),
            'items': data.get('items', []),
        })
    return snapshots


def build_totals(snapshots):
    items = {}
    for snapshot in snapshots:
        for item in snapshot.get('items', []):
            ident = int(item.get('id', -1))
            entry = items.setdefault(ident, {'id': ident, 'name': item.get('name') or '', 'qty': 0, 'accounts': []})
            entry['qty'] += int(item.get('qty', 0))
            if not entry['name'] and item.get('name'):
                entry['name'] = item['name']
            entry['accounts'].append(snapshot['account'])
    ordered = sorted(items.values(), key=lambda e: (-e['qty'], e['id']))
    for entry in ordered:
        entry['accounts'] = sorted(set(entry['accounts']))
        entry['accountCount'] = len(entry['accounts'])
    return {
        'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'accounts': len(snapshots),
        'accountsWithBank': sorted(s['account'] for s in snapshots),
        'items': ordered,
        'snapshots': snapshots,
    }


def write_totals(totals):
    os.makedirs(BANK_DIR, exist_ok=True)
    path = os.path.join(BANK_DIR, 'totals.json')
    temporary = path + '.tmp'
    with open(temporary, 'w') as handle:
        json.dump(totals, handle, indent=1)
    os.replace(temporary, path)

    top = ', '.join(f"{i['name'] or i['id']} x{i['qty']}" for i in totals['items'][:8]) or 'no items'
    line = f"{totals['updatedAt']}  {totals['accounts']} accounts  {len(totals['items'])} item kinds  {top}\n"
    with open(os.path.join(BANK_DIR, 'totals.log'), 'a') as handle:
        handle.write(line)
    return path, line


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', type=int, metavar='SECONDS', nargs='?', const=60, default=0,
                        help='refresh every SECONDS (default 60)')
    args = parser.parse_args()

    while True:
        totals = build_totals(read_snapshots())
        path, line = write_totals(totals)
        print(line.strip(), flush=True)
        if not args.watch:
            print(f'wrote {path}')
            return
        time.sleep(args.watch)


if __name__ == '__main__':
    main()
