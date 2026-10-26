#!/bin/sh
# Summarise the Duel Arena bots: sh stats.sh
#
# Per world: whether it is running, how many accounts are in, the duels won, the deaths, the
# levels, and any script errors or rate limited logins. Then every account's total duels won
# and its current levels, and the fleet's CPU and memory.
cd "$(dirname "$0")"

printf 'report at %s\n\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
printf '%-6s %-9s %-8s %-6s %-6s %-6s %-7s %-8s\n' world up accounts duels deaths lvls errors limited
for w in 1 2 3; do
    log="logs/world$w.log"
    [ -f "$log" ] || continue
    if pgrep -f "[R]s2004Headless client_w$w.jsonc" > /dev/null 2>&1; then
        up=$(ps -o etime= -C Rs2004Headless | tr -d ' ' | head -1)
        running="yes"
    else
        running="no"
        up="-"
    fi
    accounts=$(grep -c 'Logged in' "$log")
    duels=$(grep -c 'duel won' "$log")
    deaths=$(grep -c 'you are dead' "$log")
    levels=$(grep -c 'advanced a' "$log")
    errors=$(grep -c 'Script error' "$log")
    limited=$(grep -c 'status 16' "$log")
    printf '%-6s %-9s %-8s %-6s %-6s %-6s %-7s %-8s\n' \
        "$w" "$running" "$accounts" "$duels" "$deaths" "$levels" "$errors" "$limited"
done

echo
echo "accounts: total duels won, and current levels"
for w in 1 2 3; do
    log="logs/world$w.log"
    [ -f "$log" ] || continue
    {
        grep 'duel won' "$log" \
            | sed -E 's/.*\[(s[0-9]+)\] duel won.*attack ([0-9]+) strength ([0-9]+) defence ([0-9]+).*/\1 W \2 \3 \4/'
        grep 'Progress: Duels:' "$log" \
            | sed -E 's/.*\[(s[0-9]+)\] Progress: Duels: [0-9]+, Attack: ([0-9]+), Strength: ([0-9]+), Defence: ([0-9]+).*/\1 L \2 \3 \4/'
        grep 'trainer starting' "$log" \
            | sed -E 's/.*\[(s[0-9]+)\] Duel Arena trainer starting.*combat level ([0-9]+).*/\1 C \2/'
    } | awk '
        { seen[$1] = 1 }
        $2 == "W" { wins[$1]++; atk[$1] = $3; str[$1] = $4; def[$1] = $5 }
        $2 == "L" { atk[$1] = $3; str[$1] = $4; def[$1] = $5 }
        $2 == "C" { clvl[$1] = $3 }
        END {
            for (k in seen) {
                if (k in atk) {
                    printf "  %-6s duels %-5s attack %-4s strength %-4s defence %-4s\n", k, wins[k] + 0, atk[k], str[k], def[k]
                } else {
                    printf "  %-6s duels %-5s combat %-4s (no levels yet)\n", k, wins[k] + 0, clvl[k]
                }
            }
        }' | sort
done

echo
echo "experience: the rate each account is earning it, from the duel results in the log"
python3 - <<'PY'
import re, glob, datetime

first, last = {}, {}
for path in glob.glob('logs/world?.log'):
    for line in open(path, encoding='utf-8', errors='replace'):
        match = re.search(r'^(\S+Z) INFO\s+\[(s\d+)\] duel won,.* xp (\d+)', line)
        if not match:
            continue
        stamp = datetime.datetime.strptime(match.group(1), '%Y-%m-%dT%H:%M:%S.%fZ')
        name, xp = match.group(2), int(match.group(3))
        first.setdefault(name, (stamp, xp))
        last[name] = (stamp, xp)

rates = []
for name in sorted(first):
    started, xp0 = first[name]
    ended, xp1 = last[name]
    hours = (ended - started).total_seconds() / 3600.0
    if hours > 0.01 and xp1 > xp0:
        rates.append(((xp1 - xp0) / hours, name, xp1 - xp0, hours))

rates.sort(reverse=True)
if rates:
    total = sum(rate for rate, _, _, _ in rates)
    print('  fleet %d xp/hour over %d accounts (window %.2f h)' % (
        total, len(rates), max(h for _, _, _, h in rates)))
    print('  per account: min %d  max %d  mean %d' % (
        min(r[0] for r in rates), max(r[0] for r in rates), total / len(rates)))
    for rate, name, gained, hours in rates[:5]:
        print('    %-6s %7d xp/hr  (+%d xp in %.2f h)' % (name, rate, gained, hours))
else:
    print('  not enough duel results yet')
PY

echo
ps -o pid,%cpu,%mem,rss,etime,comm -C Rs2004Headless 2>/dev/null
ps -o %cpu=,rss= -C Rs2004Headless 2>/dev/null \
    | awk '{c+=$1; r+=$2} END {printf "\nfleet: %.1f%% of one core (%.1f%% of the box) | %.0f MB resident\n", c, c/8, r/1024}'
free -m | sed -n 2p
