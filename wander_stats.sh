#!/bin/sh
# Summarise WanderPunch bots: sh wander_stats.sh
cd "$(dirname "$0")"

python3 - <<'PY'
import glob, re, os, datetime

accounts = []
for path in sorted(glob.glob('progress/*.txt')):
    name = os.path.basename(path).replace('.txt','')
    with open(path) as f:
        text = f.read()
    blocks = text.split('Progress at ')
    if len(blocks) < 2:
        continue
    # parse all blocks for timestamp + combat xp
    history = []
    for b in blocks[1:]:
        m = re.match(r'(\S+Z)', b)
        if not m: continue
        ts = m.group(1)
        try:
            dt = datetime.datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ')
        except:
            try:
                dt = datetime.datetime.strptime(ts, '%Y-%m-%dT%H:%M:%S.%fZ')
            except:
                continue
        mm = re.search(r'\|\s*Combat XP\s*\|\s*(\S+)\s*\|', b)
        if not mm: continue
        try:
            xp = int(mm.group(1))
        except:
            continue
        history.append((dt, xp))
    if not history:
        continue
    last_dt, last_xp = history[-1]
    # use only last 20 minutes for exp/hr (avoids stale data from previous runs)
    cutoff = last_dt - datetime.timedelta(minutes=20)
    recent = [(dt, xp) for dt, xp in history if dt >= cutoff]
    if len(recent) < 2:
        rate = 0
    else:
        first_dt, first_xp = recent[0]
        hours = (recent[-1][0] - first_dt).total_seconds() / 3600.0
        if hours > 0.01:
            rate = (recent[-1][1] - first_xp) / hours
        else:
            rate = 0
    # last block fields
    last_block = blocks[-1]
    def get(field):
        mm = re.search(r'\|\s*'+re.escape(field)+r'\s*\|\s*(\S+)\s*\|', last_block)
        return mm.group(1) if mm else '?'
    kills = get('Kills')
    hp = get('HP%')
    atk = get('Attack')
    strn = get('Strength')
    defe = get('Defence')
    accounts.append((name, kills, last_xp, hp, atk, strn, defe, rate, last_dt))

print(f"{'Account':<8} {'Kills':>6} {'CombatXP':>9} {'XP/hr':>8} {'HP%':>5} {'Atk':>4} {'Str':>4} {'Def':>4}  {'LastReport'}")
print('-'*80)
total_kills = 0
total_xp = 0
total_rate = 0
for name, kills, xp, hp, atk, strn, defe, rate, ts in accounts:
    try: total_kills += int(kills)
    except: pass
    total_xp += xp
    total_rate += rate
    print(f"{name:<8} {kills:>6} {xp:>9} {rate:>8.0f} {hp:>5} {atk:>4} {strn:>4} {defe:>4}  {ts.strftime('%m-%d %H:%M')}")
print('-'*80)
n = len(accounts)
if n > 0:
    print(f"{'AVG':<8} {total_kills/n:>6.1f} {total_xp/n:>9.0f} {total_rate/n:>8.0f}")
print(f"{'TOTAL':<8} {total_kills:>6} {total_xp:>9} {total_rate:>8.0f}")
PY