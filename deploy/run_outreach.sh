#!/usr/bin/env bash
# Cron entry point of the outreach ("hidden market") pass; cron runs it once an hour (see docs/deployment.md).
# Daytime only (06:00-21:59 Moscow), random delay, never two passes at once. The vacancy pass (run_pass.sh) is separate.
set -u
cd "$(dirname "$0")/.."

HOUR=$(TZ=Europe/Moscow date +%H)
if [ "$((10#$HOUR))" -lt 6 ] || [ "$((10#$HOUR))" -ge 22 ]; then
  exit 0
fi

sleep $((RANDOM % 300))   # random delay so requests do not land on exact minutes

mkdir -p data
exec 9>data/outreach.lock
flock -n 9 || exit 0      # a previous outreach pass is still running

{
  echo "--- $(date -u +%FT%TZ)"
  PYTHONUTF8=1 ./.venv/bin/python -m jobbot.outreach.run
} >> data/outreach.log 2>&1
