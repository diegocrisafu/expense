"""Regression test: exit outcomes must keep their strategy attribution.

The learner recorded outcomes via _load_active_positions(), but execute_exit
closes the row first — so by recording time the position was gone and every
outcome fell back to strategy "UNKNOWN" (engine state: 4 outcomes under
UNKNOWN, 0 under CORRELATED, despite 16 settled CORRELATED trades).
position_record() must recover attribution for open AND closed rows.
"""

import sqlite3
from decimal import Decimal

import pytest

from polymarket_scanner.position_manager import position_record


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE managed_positions (
            id INTEGER PRIMARY KEY, trade_id INTEGER, market_id TEXT,
            token_id TEXT, side TEXT, entry_price DECIMAL, size DECIMAL,
            cost_basis DECIMAL, take_profit_price DECIMAL, status TEXT,
            exit_reason TEXT, closed_at TIMESTAMP
        );
        CREATE TABLE trade_history (
            id INTEGER PRIMARY KEY, strategy TEXT, status TEXT
        );
        INSERT INTO trade_history (id, strategy, status) VALUES (7, 'CORRELATED', 'PENDING');
        -- the position is already CLOSED when the learner runs
        INSERT INTO managed_positions
            (id, trade_id, side, entry_price, take_profit_price, status, exit_reason)
            VALUES (42, 7, 'BUY', 0.16, 0.224, 'CLOSED', 'TAKE_PROFIT');
    """)
    conn.commit()
    conn.close()
    return path


class TestPositionRecord:
    def test_closed_position_keeps_strategy(self, db):
        rec = position_record(42, db_path=db)
        assert rec is not None
        assert rec["strategy"] == "CORRELATED"
        assert rec["trade_id"] == 7
        assert rec["side"] == "BUY"
        assert rec["entry_price"] == Decimal("0.16")

    def test_unknown_position_returns_none(self, db):
        assert position_record(999, db_path=db) is None

    def test_missing_trade_history_degrades_to_unknown(self, db):
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO managed_positions (id, trade_id, side, entry_price, take_profit_price, status) "
                     "VALUES (43, 8, 'BUY', 0.2, 0.28, 'CLOSED')")
        conn.commit()
        conn.close()
        rec = position_record(43, db_path=db)
        assert rec["strategy"] == "UNKNOWN"
