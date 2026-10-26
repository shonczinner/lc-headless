#!/bin/sh
# Start one world's bots, waiting for the server's login window first. run.sh calls this detached.
#
#   sh start-world.sh <world> [delay-seconds]
set -e
cd "$(dirname "$0")"
mkdir -p logs

world="$1"
delay="${2:-0}"

# Seconds of quiet after the last login attempt anywhere, before a world may log in again.
# Set to 0 to disable the server-login-window wait (login pacing is handled by client_w<N>.jsonc loginIntervalSeconds).
QUIET_SECONDS=0

last_login_epoch() {
    newest=0
    for log in logs/world*.log; do
        [ -f "$log" ] || continue
        stamp=$(grep -E "Logging in as|Login attempt" "$log" | tail -1 | cut -d' ' -f1)
        [ -n "$stamp" ] || continue
        epoch=$(date -u -d "$stamp" +%s 2>/dev/null) || continue
        [ "$epoch" -gt "$newest" ] && newest=$epoch
    done
    [ "$newest" -gt 0 ] || return 1
    echo "$newest"
}

if [ "$delay" -gt 0 ]; then
    sleep "$delay"
fi

now=$(date -u +%s)
last=$(last_login_epoch) || last=0
need=$((QUIET_SECONDS - (now - last)))
if [ "$need" -gt 0 ]; then
    echo "world $world: waiting ${need}s for the server's login window to clear"
    sleep "$need"
fi

echo "world $world starting at $(date '+%H:%M:%S')"
exec ./build/parity/Rs2004Headless "client_w$world.jsonc" --raw-console \
    > "logs/world$world.log" 2>&1 < /dev/null
