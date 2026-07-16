#!/bin/bash
# Publish the bot's latest data to the GitHub Pages tracker.
#
# Exports a data.json snapshot (stats + news) from the local database and
# pushes it to main, where GitHub Pages serves it to the public dashboard.
#
# Runs automatically twice a day via launchd (see
# ~/Library/LaunchAgents/com.roger.publish-tracker.plist), or run it
# manually any time you want the public page refreshed immediately.

set -e
cd "$(dirname "$0")"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Publishing tracker snapshot..."

# Only publish from main — never commit onto a feature branch mid-work.
branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$branch" != "main" ]; then
    echo "On branch '$branch', not main — skipping publish."
    exit 0
fi

.venv/bin/python -m polymarket_scanner.dashboard --export data.json

if git diff --quiet -- data.json; then
    echo "No data changes to publish."
    exit 0
fi

# Commit ONLY data.json — anything else staged or modified is left untouched.
git commit -m "chore: publish tracker snapshot $(date -u +%Y-%m-%dT%H:%M)Z" -- data.json

# Push; if the remote moved ahead, rebase our snapshot commit on top and retry.
git push origin main || { git pull --rebase --autostash origin main && git push origin main; }

echo "Published to https://diegocrisafu.github.io/expense/"
