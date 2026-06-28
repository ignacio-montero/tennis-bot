#!/bin/bash
# Install + activate the launchd schedule (activity-booking jobs).
# Renders the plist templates (deploy/launchd/*.plist) by substituting the real
# project path, then loads them as user LaunchAgents. No sudo needed.
# Re-run any time to refresh.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$PROJECT_DIR/logs"
chmod +x "$PROJECT_DIR"/scripts/*.sh

for f in "$PROJECT_DIR"/deploy/launchd/com.tennisbot.*.plist; do
    [ -e "$f" ] || continue            # skips *.DISABLED
    label="$(basename "$f" .plist)"
    dest="$AGENTS/$label.plist"
    sed "s#__PROJECT_DIR__#$PROJECT_DIR#g" "$f" > "$dest"
    launchctl unload "$dest" 2>/dev/null || true
    launchctl load -w "$dest"
    echo "loaded $label"
done

echo
echo "Active tennisbot jobs:"
launchctl list | grep tennisbot || echo "  (none found — check errors above)"
echo
echo "NOTE: launchd fires only while the Mac is awake (else it runs at next wake)."
echo "Keep the Mac on/plugged in around the scheduled times, or migrate the trigger"
echo "to an always-on host (AWS) later — the booking logic won't change."
