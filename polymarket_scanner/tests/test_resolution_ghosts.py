"""Regression test: resolve_position must not leave ghost OPEN rows.

A position settled elsewhere (exit manager or P&L ledger) stayed OPEN in
`positions` forever, so it was re-checked against the API every 5 cycles
for weeks and inflated the open-position count.
"""

import asyncio
import sqlite3
from decimal import Decimal

import pytest

from polymarket_scanner.resolution import PendingPosition, ResolutionTracker


@pytest.fixture
def tracker(tmp_path):
    db = str(tmp_path / "t.db")
    t = ResolutionTracker(db_path=db)
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE IF NOT EXISTS trade_history (
        id INTEGER PRIMARY KEY, timestamp TIMESTAMP, strategy TEXT,
        market_id TEXT, market_question TEXT, category TEXT, token_id TEXT,
        side TEXT, entry_price DECIMAL, size DECIMAL, status TEXT,
        exit_price DECIMAL, pnl DECIMAL, resolved_at TIMESTAMP)""")
    conn.commit()
    conn.close()
    return t


def seed(tracker, trade_id, th_status):
    conn = sqlite3.connect(tracker.db_path)
    conn.execute(
        "INSERT OR REPLACE INTO trade_history "
        "(id, status, market_question, strategy, category, entry_price, size, side) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (trade_id, th_status, "q", "CORRELATED", "test", 0.02, 50, "BUY"),
    )
    conn.commit()
    conn.close()
    tracker.record_position(trade_id=trade_id, market_id="0xm", token_id="tok_yes",
                            side="BUY", entry_price=Decimal("0.02"), size=Decimal("50"))


def pos(trade_id):
    return PendingPosition(trade_id, "0xm", "tok_yes", "BUY",
                           Decimal("0.02"), Decimal("50"), "q")


def row_status(tracker, trade_id):
    conn = sqlite3.connect(tracker.db_path)
    status = conn.execute(
        "SELECT status FROM positions WHERE trade_id=?", (trade_id,)
    ).fetchone()[0]
    conn.close()
    return status


class TestGhostRows:
    def test_already_settled_trade_marks_positions_row_closed(self, tracker):
        """No P&L is double-booked, but the OPEN row must be closed out."""
        seed(tracker, 1, "WON")
        res = {"resolved": True, "winner_token_id": "tok_yes",
               "winning_outcome": "Yes", "resolution_price": Decimal("1")}
        out = asyncio.run(tracker.resolve_position(pos(1), res))
        assert out is None
        assert row_status(tracker, 1) != "OPEN"

    def test_pending_trade_row_is_not_touched_by_the_guard(self, tracker):
        """A genuinely pending trade must still resolve normally."""
        seed(tracker, 2, "PENDING")
        res = {"resolved": True, "winner_token_id": "tok_yes",
               "winning_outcome": "Yes", "resolution_price": Decimal("1")}
        out = asyncio.run(tracker.resolve_position(pos(2), res))
        assert out is not None
        assert row_status(tracker, 2) == "WON"
