#!/bin/bash
# Court-drop run: launched a few minutes BEFORE the drop; pre-warms a session
# then spin-waits to the exact drop instant (config: targets.yaml drop.local_time)
# and books. Same portable entry-point pattern as scheduled_run.sh.
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1
mkdir -p logs
LOG="logs/court-drop-$(date +%Y%m%d).log"
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') court-drop start =====" >> "$LOG"
PYTHONPATH=src ".venv/bin/python" -m tennisbot drop --centre paddington --live \
    >> "$LOG" 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') court-drop end (exit $?) =====" >> "$LOG"
