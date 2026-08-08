"""Regression tests: dashboard headline numbers come from the unified ledger.

The public page previously read the `trades` arb log (profits ~0), which
hid both the phantom gains and the real losses.  Balance and summary must
reflect clean-period trade_history, which quarantine corrections flow into.
"""

import sqlite3
from decimal import Decimal

import pytest

from polymarket_scanner.dashboard import DashboardData
from polymarket_scanner.trading_config import STARTING_BALANCE


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY, timestamp TIMESTAMP, trade_type TEXT,
            market_or_token TEXT, size DECIMAL, profit DECIMAL, mode TEXT
        );
        CREATE TABLE trade_history (
            id INTEGER PRIMARY KEY, timestamp TIMESTAMP, strategy TEXT,
            market_id TEXT, market_question TEXT, category TEXT, token_id TEXT,
            side TEXT, entry_price DECIMAL, size DECIMAL, status TEXT,
            exit_price DECIMAL, pnl DECIMAL, resolved_at TIMESTAMP
        );
        -- arb log says +99 (the old, misleading source)
        INSERT INTO trades (timestamp, size, profit, mode)
            VALUES ('2026-07-10 00:00:00', 1, 99.0, 'PAPER');
        -- unified ledger, clean period: one loss, one win, one pending
        INSERT INTO trade_history (timestamp, size, status, pnl) VALUES
            ('2026-07-05 00:00:00', 1.0, 'LOST', -1.0),
            ('2026-07-06 00:00:00', 1.0, 'WON', 2.5),
            ('2026-07-07 00:00:00', 1.0, 'PENDING', NULL);
        -- pre-clean rows must be excluded
        INSERT INTO trade_history (timestamp, size, status, pnl) VALUES
            ('2026-02-01 00:00:00', 1.0, 'WON', 500.0);
    """)
    conn.commit()
    conn.close()
    return path


class TestLedgerSourcedHeadline:
    def test_balance_is_starting_plus_clean_realized(self, db):
        assert DashboardData(db).get_current_balance() == STARTING_BALANCE + Decimal("1.5")

    def test_summary_counts_come_from_trade_history(self, db):
        s = DashboardData(db).get_trade_summary()
        assert s["total_trades"] == 3
        assert s["wins"] == 1
        assert s["losses"] == 1
        assert s["pending"] == 1
        assert s["total_profit"] == pytest.approx(1.5)
