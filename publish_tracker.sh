#!/bin/bash
# Publish the bot's latest data to the GitHub Pages tracker.
#
# Exports a data.json snapshot (stats + news) from the local database and
# pushes it to main, where GitHub Pages serves it to the public dashboard.
#
# Run manually, or schedule it (e.g. every 3h via cron):
#   0 */3 * * * /Users/diegocrisafulli/Documents/expense/publish_tracker.sh >> /tmp/publish_tracker.log 2>&1

set -e
cd "$(dirname "$0")"

.venv/bin/python -m polymarket_scanner.dashboard --export data.json

git add data.json
if git diff --cached --quiet; then
    echo "No data changes to publish."
    exit 0
fi

git commit -m "chore: publish tracker snapshot $(date -u +%Y-%m-%dT%H:%M)Z"
git push origin main
echo "Published to https://diegocrisafu.github.io/expense/"
