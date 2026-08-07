#!/bin/bash
# ─── Roger deadman check ───
# Alerts (macOS notification + log line) when the bot looks silent:
#   1. the log hasn't been written in LOG_STALL_MIN minutes, or
#   2. no trade has been recorded in TRADE_STALL_HOURS hours.
# Run by com.roger.deadman.plist every 15 minutes.  Read-only: it never
# starts, stops, or restarts anything.

DIR="/Users/diegocrisafulli/Documents/expense"
LOGFILE="$DIR/roger_paper.log"
DB="$DIR/polymarket_scanner.db"
STATE_LOG="$DIR/deadman.log"

LOG_STALL_MIN=30
TRADE_STALL_HOURS=48
ALERT_COOLDOWN_HOURS=6   # per condition: notify at most once per this window

alert() {
    local key="$1" msg="$2"
    local marker="$DIR/.deadman_last_${key}"
    local now epoch_last
    now=$(date +%s)
    epoch_last=$(cat "$marker" 2>/dev/null || echo 0)
    echo "$(date '+%Y-%m-%d %H:%M:%S') ALERT: $msg" >> "$STATE_LOG"
    if [ $(( now - epoch_last )) -ge $(( ALERT_COOLDOWN_HOURS * 3600 )) ]; then
        echo "$now" > "$marker"
        osascript -e "display notification \"$msg\" with title \"Roger deadman\" sound name \"Basso\"" 2>/dev/null
    fi
}

# 1) Log freshness
if [ -f "$LOGFILE" ]; then
    log_age_min=$(( ( $(date +%s) - $(stat -f %m "$LOGFILE") ) / 60 ))
    if [ "$log_age_min" -ge "$LOG_STALL_MIN" ]; then
        alert log "Bot log silent for ${log_age_min}m (threshold ${LOG_STALL_MIN}m)"
    fi
else
    alert log "Bot log missing: $LOGFILE"
fi

# 2) Trade freshness (clean-period ledger)
if [ -f "$DB" ]; then
    last_trade=$(sqlite3 "$DB" "SELECT MAX(timestamp) FROM trade_history WHERE timestamp >= '2026-07-03';" 2>/dev/null)
    if [ -n "$last_trade" ]; then
        last_epoch=$(date -j -u -f "%Y-%m-%d %H:%M:%S" "$last_trade" +%s 2>/dev/null)
        if [ -n "$last_epoch" ]; then
            trade_age_h=$(( ( $(date +%s) - last_epoch ) / 3600 ))
            if [ "$trade_age_h" -ge "$TRADE_STALL_HOURS" ]; then
                alert trade "No trade recorded in ${trade_age_h}h (threshold ${TRADE_STALL_HOURS}h)"
            fi
        fi
    fi
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') check ran" >> "$STATE_LOG"
