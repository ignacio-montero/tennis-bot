#!/bin/bash
# Stop + remove the launchd schedule.
set -uo pipefail
AGENTS="$HOME/Library/LaunchAgents"
for label in \
    com.tennisbot.activity-sun-primary \
    com.tennisbot.activity-sun-backup \
    com.tennisbot.activity-wed-primary \
    com.tennisbot.activity-wed-backup; do
    launchctl unload "$AGENTS/$label.plist" 2>/dev/null && echo "unloaded $label" || true
    rm -f "$AGENTS/$label.plist"
done
echo "done. (To also remove wake schedule: sudo pmset repeat cancel)"
