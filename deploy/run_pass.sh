#!/usr/bin/env bash
# Cron entry point (every 6 minutes). Decides whether to run now, adds jitter, prevents overlap.
# Day (06:00-21:59 Moscow): every run. Night: only the first run of each hour.
set -u
cd "$(dirname "$0")/.."

HOUR=$(TZ=Europe/Moscow date +%H)
MIN=$(TZ=Europe/Moscow date +%M)
if [ "$HOUR" -lt 6 ] || [ "$HOUR" -ge 22 ]; then
  [ "$((10#$MIN))" -lt 6 ] || exit 0
fi

sleep $((RANDOM % 120))   # random jitter so requests do not land on exact minutes

mkdir -p data
exec 9>data/run.lock
flock -n 9 || exit 0      # a previous pass is still running

{
  echo "--- $(date -u +%FT%TZ)"
  PYTHONUTF8=1 ./.venv/bin/python -m jobbot.run --limit 5
} >> data/run.log 2>&1
