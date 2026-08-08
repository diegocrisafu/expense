"""Launch the Roger paper-trading bot under launchd.

launchd cannot chdir into ~/Documents or open a log file there: that
directory is TCC-protected and background agents get no grant (first
supervised start died with EX_CONFIG/78 for exactly this reason, while
the Python-based deadman ran fine).  So the plist points straight at this
script with no WorkingDirectory and no Standard*Path, and the work that
needs Documents access happens here, inside the interpreter that does
hold the grant: chdir, log rotation, and stdout/stderr redirection.

PAPER MODE ONLY — this launcher never passes --live.
"""

import os
import runpy
import sys

PROJECT_DIR = "/Users/diegocrisafulli/Documents/expense"
LOGFILE = os.path.join(PROJECT_DIR, "roger_paper.log")
MAX_LOG_BYTES = 5 * 1024 * 1024
SCAN_INTERVAL = "60"


def rotate_logs() -> None:
    """Keep roger_paper.log bounded: .log -> .log.1 -> .log.2 -> .log.3."""
    try:
        if os.path.getsize(LOGFILE) <= MAX_LOG_BYTES:
            return
    except OSError:
        return
    for n in (3, 2, 1):
        older = f"{LOGFILE}.{n}"
        newer = f"{LOGFILE}.{n - 1}" if n > 1 else LOGFILE
        if os.path.exists(newer):
            os.replace(newer, older)  # overwrites .3, dropping the oldest


def main() -> None:
    os.chdir(PROJECT_DIR)
    if PROJECT_DIR not in sys.path:
        sys.path.insert(0, PROJECT_DIR)

    rotate_logs()
    fd = os.open(LOGFILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, sys.stdout.fileno())
    os.dup2(fd, sys.stderr.fileno())
    if fd > 2:
        os.close(fd)

    sys.argv = ["polymarket_scanner.trading_bot", "--interval", SCAN_INTERVAL]
    runpy.run_module("polymarket_scanner.trading_bot", run_name="__main__")


if __name__ == "__main__":
    main()
