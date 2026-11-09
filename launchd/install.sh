#!/bin/sh
# Install the daily macOS LaunchAgent for Job Hunt. Run this yourself; the app never runs it.
# If it is already loaded, it is unloaded first, so running this twice is safe.
set -eu

LABEL="com.jobhunt.daily"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$PROJECT/launchd/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/job-hunt"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
chmod 755 "$LOG_DIR"

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "Already loaded; unloading it first."
    launchctl bootout "gui/$(id -u)/$LABEL" || true
fi

sed -e "s|__PROJECT_DIR__|$PROJECT|g" -e "s|__HOME__|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null || true

echo "Installed $LABEL."
echo "Next run: every day at 07:00 (a run missed while the Mac sleeps happens on wake)."
echo "Test it now:  launchctl kickstart gui/$(id -u)/$LABEL"
echo "Logs:         $LOG_DIR  and  $PROJECT/data/logs/"
echo "Remove it:    $PROJECT/launchd/uninstall.sh"
