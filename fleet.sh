#!/bin/sh
# Restart the whole fleet: stop anything running, wait for server sessions to expire,
# then start worlds 1-3 one at a time, waiting for each world's 15 logins.
#
#   sh fleet.sh              # normal start
#   WAIT=60 sh fleet.sh      # shorter session-expiry wait
set -u
cd "$(dirname "$0")"

WAIT=${WAIT:-120}
LOGIN_WAIT=${LOGIN_WAIT:-300}

echo "== stopping running fleet =="
sh stop.sh >/dev/null 2>&1 || true
sleep 2
pkill -9 -f "[R]s2004Headless" 2>/dev/null || true
sleep 1
if pgrep -f "[R]s2004Headless" >/dev/null 2>&1; then
    echo "could not stop the client"
    exit 1
fi

echo "== waiting ${WAIT}s for server sessions to expire =="
sleep "$WAIT"

rm -f logs/world*.log logs/world*.start.log progress/*.txt rs2004-headless-w*.log

for w in 1 2 3; do
    echo "== world $w =="
    nohup sh start-world.sh "$w" 0 > "logs/world$w.start.log" 2>&1 &
    waited=0
    count=0
    while [ "$waited" -lt "$LOGIN_WAIT" ]; do
        sleep 5
        waited=$((waited + 5))
        count=$(grep -c 'Logged in' "logs/world$w.log" 2>/dev/null | head -1)
        [ -n "$count" ] || count=0
        echo "  ${waited}s: $count/15 logged in"
        [ "$count" -ge 15 ] && break
    done
    [ "$count" -ge 15 ] || echo "  world $w did not reach 15 logins; continuing"
done

echo "== fleet =="
ps -o pid,%cpu,rss,etime,args -C Rs2004Headless 2>/dev/null
echo "done. logs: logs/world{1,2,3}.log | stats: sh stats.sh"
