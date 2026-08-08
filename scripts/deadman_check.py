"""Roger deadman check — alerts when the bot looks silent.

Conditions:
  1. the bot log hasn't been written in LOG_STALL_MIN minutes
  2. no trade recorded in TRADE_STALL_HOURS hours (clean-period ledger)

Runs under the project venv's Python via com.roger.deadman.plist every 15
minutes.  Python (not bash) so that a single Full Disk Access grant to the
interpreter covers both launchd agents — ~/Documents is TCC-protected and
background agents can't prompt for access.  Read-only: never starts,
stops, or restarts anything.  Notifies at most once per condition per
ALERT_COOLDOWN_HOURS; every event still lands in deadman.log.
"""

import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

DIR = Path("/Users/diegocrisafulli/Documents/expense")
LOGFILE = DIR / "roger_paper.log"
DB = DIR / "polymarket_scanner.db"
STATE_LOG = DIR / "deadman.log"

LOG_STALL_MIN = 30
TRADE_STALL_HOURS = 48
ALERT_COOLDOWN_HOURS = 6
CLEAN_DATA_SINCE = "2026-07-03"


def note(line: str) -> None:
    with STATE_LOG.open("a") as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {line}\n")


def alert(key: str, msg: str) -> None:
    note(f"ALERT: {msg}")
    marker = DIR / f".deadman_last_{key}"
    now = time.time()
    try:
        last = float(marker.read_text().strip())
    except (FileNotFoundError, ValueError):
        last = 0.0
    if now - last < ALERT_COOLDOWN_HOURS * 3600:
        return
    marker.write_text(str(now))
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{msg}" with title "Roger deadman" sound name "Basso"'],
        capture_output=True,
    )


def main() -> None:
    # 1) Log freshness
    if LOGFILE.exists():
        age_min = int((time.time() - LOGFILE.stat().st_mtime) / 60)
        if age_min >= LOG_STALL_MIN:
            alert("log", f"Bot log silent for {age_min}m (threshold {LOG_STALL_MIN}m)")
    else:
        alert("log", f"Bot log missing: {LOGFILE}")

    # 2) Trade freshness
    if DB.exists():
        try:
            conn = sqlite3.connect(str(DB))
            row = conn.execute(
                "SELECT MAX(timestamp) FROM trade_history WHERE timestamp >= ?",
                (CLEAN_DATA_SINCE,),
            ).fetchone()
            conn.close()
            if row and row[0]:
                last = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
                age_h = int((datetime.now(timezone.utc) - last).total_seconds() / 3600)
                if age_h >= TRADE_STALL_HOURS:
                    alert("trade", f"No trade recorded in {age_h}h (threshold {TRADE_STALL_HOURS}h)")
        except Exception as e:
            note(f"trade check failed: {e}")

    note("check ran")


if __name__ == "__main__":
    main()
