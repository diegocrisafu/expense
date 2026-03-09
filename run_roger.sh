#!/bin/bash
# ─── Roger — Polymarket Bot Background Launcher ───
# Usage:
#   ./run_roger.sh start   — launch bot in background
#   ./run_roger.sh stop    — stop the bot
#   ./run_roger.sh status  — check if running
#   ./run_roger.sh logs    — tail live logs
#   ./run_roger.sh restart — stop + start

DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/.roger.pid"
LOGFILE="$DIR/roger.log"
PYTHON="$DIR/.venv/bin/python"
MAX_LOG_SIZE=5242880  # 5MB

# Rotate logs if they exceed MAX_LOG_SIZE
rotate_logs() {
    if [ -f "$LOGFILE" ]; then
        SIZE=$(stat -f%z "$LOGFILE" 2>/dev/null || stat -c%s "$LOGFILE" 2>/dev/null || echo 0)
        if [ "$SIZE" -gt "$MAX_LOG_SIZE" ] 2>/dev/null; then
            [ -f "${LOGFILE}.3" ] && rm "${LOGFILE}.3"
            [ -f "${LOGFILE}.2" ] && mv "${LOGFILE}.2" "${LOGFILE}.3"
            [ -f "${LOGFILE}.1" ] && mv "${LOGFILE}.1" "${LOGFILE}.2"
            mv "$LOGFILE" "${LOGFILE}.1"
            echo "📦 Rotated log (was $((SIZE / 1024 / 1024))MB)"
        fi
    fi
}

start_bot() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "⚠️  Roger is already running (PID $(cat "$PIDFILE"))"
        echo "   Use: ./run_roger.sh logs  to see output"
        return 1
    fi

    echo "🤖 Starting Roger..."
    cd "$DIR"

    # Auto-rotate logs before starting
    rotate_logs

    # nohup keeps it alive after terminal closes
    nohup "$PYTHON" -m polymarket_scanner.trading_bot --live --interval 60 \
        >> "$LOGFILE" 2>&1 &

    echo $! > "$PIDFILE"
    sleep 1

    if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "✅ Roger is running (PID $(cat "$PIDFILE"))"
        echo "   Logs:      $LOGFILE"
        echo "   Dashboard: http://localhost:8080"
        echo "   Stop:      ./run_roger.sh stop"
        echo "   Tail logs: ./run_roger.sh logs"
    else
        echo "❌ Roger failed to start. Check $LOGFILE"
        rm -f "$PIDFILE"
        return 1
    fi
}

stop_bot() {
    if [ ! -f "$PIDFILE" ]; then
        echo "Roger is not running (no PID file)"
        return 0
    fi

    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping Roger (PID $PID)..."
        kill "$PID"
        sleep 2
        # Force kill if still alive
        if kill -0 "$PID" 2>/dev/null; then
            kill -9 "$PID"
        fi
        echo "✅ Roger stopped"
    else
        echo "Roger was not running (stale PID)"
    fi
    rm -f "$PIDFILE"
}

status_bot() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        PID=$(cat "$PIDFILE")
        UPTIME=$(ps -p "$PID" -o etime= 2>/dev/null | xargs)
        echo "🟢 Roger is RUNNING"
        echo "   PID:       $PID"
        echo "   Uptime:    $UPTIME"
        echo "   Logs:      $LOGFILE"
        echo "   Dashboard: http://localhost:8080"
    else
        echo "🔴 Roger is NOT running"
        rm -f "$PIDFILE" 2>/dev/null
    fi
}

tail_logs() {
    if [ ! -f "$LOGFILE" ]; then
        echo "No log file yet. Start Roger first."
        return 1
    fi
    echo "📋 Tailing roger.log (Ctrl+C to stop watching)..."
    echo ""
    tail -f "$LOGFILE"
}

case "${1:-start}" in
    start)   start_bot ;;
    stop)    stop_bot ;;
    status)  status_bot ;;
    logs)    tail_logs ;;
    restart) stop_bot; sleep 1; start_bot ;;
    *)
        echo "Usage: $0 {start|stop|status|logs|restart}"
        exit 1
        ;;
esac
