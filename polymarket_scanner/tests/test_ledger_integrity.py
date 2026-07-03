"""Tests for the P&L ledger integrity fixes.

These lock in the fix for the "0% win rate" corruption:
  1. A managed exit (position_manager._close_position) writes the REAL outcome
     to trade_history — a profitable exit is booked as WON, not LOST.
  2. resolution.py never re-books a position already closed by the manager.
  3. learning.resolve_trade never clobbers an already-settled trade.
"""

import sqlite3
from decimal import Decimal

import pytest

from polymarket_scanner.learning import LearningEngine
from polymarket_scanner.position_manager import PositionManager


def _make_db(tmp_path):
    """A DB with the trade_history + managed_positions schema and one open trade."""
    db = str(tmp_path / "ledger.db")
    # Creating the engines runs their CREATE TABLE IF NOT EXISTS migrations.
    learn = LearningEngine(db_path=db)
    pm = PositionManager(db_path=db)
    return db, learn, pm


def _insert_open_trade(db, entry, size_dollars):
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO trade_history (strategy, market_id, token_id, side, entry_price, size, status) "
        "VALUES ('MOMENTUM','m1','t1','BUY',?,?,'PENDING')",
        (str(entry), str(size_dollars)),
    )
    trade_id = cur.lastrowid
    cur.execute(
        "INSERT INTO managed_positions (trade_id, market_id, token_id, side, entry_price, size, "
        "cost_basis, current_price, high_water_mark, status) "
        "VALUES (?,?,?,?,?,?,?,?,?, 'ACTIVE')",
        (trade_id, "m1", "t1", "BUY", str(entry), "5", str(entry * 5), str(entry), str(entry)),
    )
    conn.commit()
    conn.close()
    return trade_id


def _trade_row(db, trade_id):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT status, exit_price, pnl FROM trade_history WHERE id=?", (trade_id,)).fetchone()
    conn.close()
    return r


class TestManagedExitSyncsLedger:
    def test_profitable_exit_books_as_won(self, tmp_path):
        db, learn, pm = _make_db(tmp_path)
        tid = _insert_open_trade(db, Decimal("0.18"), Decimal("1"))
        # Manager sells at 0.50 (real bid) → +$1.60 on 5 shares.
        pnl = (Decimal("0.50") - Decimal("0.18")) * Decimal("5")
        # position_id is the managed_positions row id (1 here)
        pm._close_position(1, "TAKE_PROFIT", Decimal("0.50"), pnl)

        row = _trade_row(db, tid)
        assert row["status"] == "WON"                 # was booked LOST before the fix
        assert Decimal(row["exit_price"]) == Decimal("0.50")
        assert Decimal(row["pnl"]) > 0

    def test_losing_exit_books_as_lost(self, tmp_path):
        db, learn, pm = _make_db(tmp_path)
        tid = _insert_open_trade(db, Decimal("0.30"), Decimal("1"))
        pnl = (Decimal("0.10") - Decimal("0.30")) * Decimal("5")
        pm._close_position(1, "STOP_LOSS", Decimal("0.10"), pnl)
        row = _trade_row(db, tid)
        assert row["status"] == "LOST"
        assert Decimal(row["pnl"]) < 0


class TestNoDoubleResolution:
    def test_learning_does_not_clobber_settled_trade(self, tmp_path):
        db, learn, pm = _make_db(tmp_path)
        tid = _insert_open_trade(db, Decimal("0.18"), Decimal("1"))
        # Manager books it WON first.
        pm._close_position(1, "TAKE_PROFIT", Decimal("0.50"), Decimal("1.60"))
        # Later, market-resolution tries to book it as a worthless-expiry LOSS.
        learn.resolve_trade(tid, exit_price=Decimal("0"), won=False)
        row = _trade_row(db, tid)
        # The real managed win must survive — NOT be overwritten to LOST.
        assert row["status"] == "WON"
        assert Decimal(row["pnl"]) > 0
