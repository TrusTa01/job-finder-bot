#!/usr/bin/env bash
# Installs the scheduled jobs of one bot copy: the job pass every 15 minutes and the hidden-market search every hour.
# Run as root:  bash deploy/install_cron.sh <linux-user>   (for example: bash deploy/install_cron.sh bot2)
# Safe to run again: lines of this copy are replaced, other cron lines of the user are kept.
set -eu

USER_NAME="${1:?usage: install_cron.sh <linux-user>}"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
APP="$HOME_DIR/job-autofinder"
[ -x "$APP/deploy/run_pass.sh" ] || { echo "no $APP/deploy/run_pass.sh: copy the code first" >&2; exit 1; }

PASS="*/15 * * * * $APP/deploy/run_pass.sh"
OUTREACH="7 * * * * $APP/deploy/run_outreach.sh"

{
  crontab -u "$USER_NAME" -l 2>/dev/null | grep -v "$APP/deploy/run_pass.sh" | grep -v "$APP/deploy/run_outreach.sh" || true
  echo "$PASS"
  echo "$OUTREACH"
} | crontab -u "$USER_NAME" -

echo "installed for $USER_NAME:"
crontab -u "$USER_NAME" -l | grep "$APP/deploy/"
