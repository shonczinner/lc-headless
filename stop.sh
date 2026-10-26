#!/bin/sh
# Stop the Duel Arena bots. The client logs every account out cleanly; give it a moment.
#
#   sh stop.sh        stop all three
#   sh stop.sh 2      stop world 2 only
set -e
cd "$(dirname "$0")"

for world in "$@"; do
    pkill -f "[R]s2004Headless client_w$world.jsonc" && echo "world $world stopping" || echo "world $world was not running"
done

if [ "$#" -eq 0 ]; then
    pkill -f "[R]s2004Headless" && echo "stopping all worlds" || echo "nothing was running"
fi
