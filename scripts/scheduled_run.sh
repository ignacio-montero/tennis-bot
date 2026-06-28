#!/bin/bash
# Single entry point for scheduled activity bookings.
# Called by launchd now; a future AWS trigger (EventBridge/cron) can call the
# very same script — only the trigger layer changes, not the booking logic.
#
# Books the activity 7 days ahead, LIVE. The activity is auto-selected by the
# current weekday (Wed -> "Tennis (adv) Wed 1900", Sun -> "... Sun 1300"), and
# the idempotency check skips if the session is already held/paid.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

mkdir -p logs
LOG="logs/activity-$(date +%Y%m%d).log"

echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') scheduled_run start =====" >> "$LOG"

PYTHONPATH=src ".venv/bin/python" -m tennisbot run-now \
    --mode activity --centre paddington --live --days-ahead 7 \
    >> "$LOG" 2>&1
code=$?

echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') scheduled_run end (exit $code) =====" >> "$LOG"
exit $code
