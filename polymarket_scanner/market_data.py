"""Market-data capture — the missing foundation for a real backtest.

The `snapshots` / `orderbook_levels` tables existed but NOTHING ever wrote to
them, so there was no historical data to replay and "is this strategy
profitable?" could never be answered offline.

This module persists a compact price series for every token the bot actually
looks at, in a purpose-built, indexed table.  It is deliberately:
  • decoupled  — one function, its own table, no coupling to strategy code;
  • defensive  — every write is best-effort and swallows errors so data capture
                 can NEVER break live trading;
  • cheap      — a single INSERT per observation.

Once this runs for a while, `backtest.py` can load real price paths via
`load_series()` and score strategies out-of-sample with `metrics.compute_metrics`.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from .database import get_connection, DB_PATH

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id   TEXT NOT NULL,
    ts         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    bid        DECIMAL(18, 8),
    ask        DECIMAL(18, 8),
    mid        DECIMAL(18, 8),
    spread     DECIMAL(18, 8),
    volume_24h DECIMAL(18, 6)
)
"""
_INDEX = "CREATE INDEX IF NOT EXISTS idx_price_snap_token_ts ON price_snapshots(token_id, ts)"

_initialized: set[str] = set()


def _ensure_schema(db_path: str) -> None:
    if db_path in _initialized:
        return
    try:
        with get_connection(db_path) as conn:
            conn.execute(_SCHEMA)
            conn.execute(_INDEX)
            conn.commit()
        _initialized.add(db_path)
    except Exception as e:
        logger.debug(f"price_snapshots schema init failed: {e}")


def capture(
    token_id: str,
    bid: Optional[Decimal],
    ask: Optional[Decimal],
    volume_24h: Optional[Decimal] = None,
    db_path: str = DB_PATH,
) -> None:
    """Record one price observation.  Best-effort — never raises."""
    try:
        if not token_id or (bid is None and ask is None):
            return
        _ensure_schema(db_path)
        b = Decimal(str(bid)) if bid is not None else None
        a = Decimal(str(ask)) if ask is not None else None
        if b is not None and a is not None:
            mid = (b + a) / 2
            spread = a - b
        else:
            mid = b if b is not None else a
            spread = None
        with get_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO price_snapshots (token_id, bid, ask, mid, spread, volume_24h) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    token_id,
                    str(b) if b is not None else None,
                    str(a) if a is not None else None,
                    str(mid) if mid is not None else None,
                    str(spread) if spread is not None else None,
                    str(volume_24h) if volume_24h is not None else None,
                ),
            )
            conn.commit()
    except Exception as e:
        logger.debug(f"snapshot capture failed for {token_id[:16] if token_id else '?'}: {e}")


def load_series(token_id: str, db_path: str = DB_PATH) -> list[dict]:
    """Return the ordered price series for a token (oldest first)."""
    _ensure_schema(db_path)
    out: list[dict] = []
    try:
        with get_connection(db_path) as conn:
            cur = conn.cursor()
            for r in cur.execute(
                "SELECT ts, bid, ask, mid, spread, volume_24h FROM price_snapshots "
                "WHERE token_id = ? ORDER BY ts ASC",
                (token_id,),
            ):
                out.append({
                    "ts": r[0],
                    "bid": Decimal(str(r[1])) if r[1] is not None else None,
                    "ask": Decimal(str(r[2])) if r[2] is not None else None,
                    "mid": Decimal(str(r[3])) if r[3] is not None else None,
                    "spread": Decimal(str(r[4])) if r[4] is not None else None,
                    "volume_24h": Decimal(str(r[5])) if r[5] is not None else None,
                })
    except Exception as e:
        logger.debug(f"load_series failed for {token_id[:16]}: {e}")
    return out


def snapshot_count(db_path: str = DB_PATH) -> int:
    """How many observations captured so far (for progress reporting)."""
    _ensure_schema(db_path)
    try:
        with get_connection(db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
    except Exception:
        return 0
