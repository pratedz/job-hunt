#!/bin/sh
# Remove the daily LaunchAgent. Logs are kept.
set -eu

LABEL="com.jobhunt.daily"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST_DST"
echo "Removed $LABEL. Logs are kept in $HOME/Library/Logs/job-hunt and data/logs/."
