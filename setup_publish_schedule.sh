#!/bin/bash
# One-time setup: schedule publish_tracker.sh to run twice a day (09:00 & 21:00)
# via macOS launchd, so the GitHub Pages tracker stays current automatically.
#
# Run once:   ./setup_publish_schedule.sh
# Undo with:  launchctl unload ~/Library/LaunchAgents/com.roger.publish-tracker.plist \
#             && rm ~/Library/LaunchAgents/com.roger.publish-tracker.plist

set -e
cd "$(dirname "$0")"

PLIST_SRC="com.roger.publish-tracker.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.roger.publish-tracker.plist"

mkdir -p "$HOME/Library/LaunchAgents"
launchctl unload "$PLIST_DST" 2>/dev/null || true
cp "$PLIST_SRC" "$PLIST_DST"
launchctl load "$PLIST_DST"

echo "Scheduled: tracker publishes daily at 09:00 and 21:00."
echo "Logs: /tmp/publish_tracker.log"
echo "Publish immediately any time with: ./publish_tracker.sh"
