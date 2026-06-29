#!/bin/bash
# Run the court-drop watcher (read-only) across the drop window. Scheduled nightly
# at 21:25 to capture when slots first appear (settles 21:45 vs 22:00).
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1
mkdir -p logs
LOG="logs/dropwatch-$(date +%Y%m%d).log"
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') watch start =====" >> "$LOG"
PYTHONPATH=src ".venv/bin/python" -m tennisbot watch --poll 5 --until 21:45 \
    >> "$LOG" 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') watch end =====" >> "$LOG"
