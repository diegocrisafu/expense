"""Unit tests for the reconcile module."""

import sqlite3

import pytest

from polymarket_scanner.reconcile import (
    cancel_orphan_attempts,
    classify_open_positions,
    count_truly_open,
    repair_ghosts,
)


@pytest.fixture
def db(tmp_path):
    """Minimal DB with one truly-open, one ghost-in-th, one ghost-in-mp."""
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY, trade_id INTEGER, market_id TEXT,
            token_id TEXT, side TEXT, entry_price DECIMAL, size DECIMAL,
            status TEXT, resolution_price DECIMAL, pnl DECIMAL, resolved_at TIMESTAMP
        );
        CREATE TABLE trade_history (
            id INTEGER PRIMARY KEY, timestamp TIMESTAMP, strategy TEXT,
            market_id TEXT, market_question TEXT, category TEXT, token_id TEXT,
            side TEXT, entry_price DECIMAL, size DECIMAL, status TEXT,
            exit_price DECIMAL, pnl DECIMAL, resolved_at TIMESTAMP
        );
        CREATE TABLE managed_positions (
            id INTEGER PRIMARY KEY, trade_id INTEGER, market_id TEXT,
            status TEXT, exit_reason TEXT, closed_at TIMESTAMP
        );

        -- trade 1: genuinely open (PENDING everywhere)
        INSERT INTO positions VALUES (1, 1, 'm1', 't1', 'BUY', 0.16, 1.0, 'OPEN', NULL, NULL, NULL);
        INSERT INTO trade_history (id, timestamp, market_question, status)
            VALUES (1, '2026-07-15 20:03:32', 'open market', 'PENDING');

        -- trade 2: ghost — settled LOST in trade_history
        INSERT INTO positions VALUES (2, 2, 'm2', 't2', 'BUY', 0.02, 1.0, 'OPEN', NULL, NULL, NULL);
        INSERT INTO trade_history (id, timestamp, market_question, status)
            VALUES (2, '2026-07-03 17:22:42', 'settled market', 'LOST');

        -- trade 3: ghost — CLOSED by the position manager
        INSERT INTO positions VALUES (3, 3, 'm3', 't3', 'BUY', 0.05, 1.0, 'OPEN', NULL, NULL, NULL);
        INSERT INTO trade_history (id, timestamp, market_question, status)
            VALUES (3, '2026-07-05 10:00:00', 'managed market', 'PENDING');
        INSERT INTO managed_positions VALUES (30, 3, 'm3', 'CLOSED', 'MARKET_DEAD', '2026-07-06 00:00:00');

        -- trade 4: not OPEN in positions — must not appear at all
        INSERT INTO positions VALUES (4, 4, 'm4', 't4', 'BUY', 0.10, 1.0, 'LOST', 0, -0.1, '2026-07-08');
    """)
    conn.commit()
    conn.close()
    return path


class TestClassifyOpenPositions:
    def test_splits_truly_open_from_ghosts(self, db):
        truly_open, ghosts = classify_open_positions(db)
        assert [p["trade_id"] for p in truly_open] == [1]
        assert sorted(p["trade_id"] for p in ghosts) == [2, 3]

    def test_ghost_reports_where_it_was_closed(self, db):
        _, ghosts = classify_open_positions(db)
        by_id = {p["trade_id"]: p for p in ghosts}
        assert by_id[2]["trade_history_status"] == "LOST"
        assert by_id[3]["managed_status"] == "CLOSED"
        assert by_id[3]["exit_reason"] == "MARKET_DEAD"

    def test_count_truly_open(self, db):
        assert count_truly_open(db) == 1


class TestRepairGhosts:
    def test_repair_closes_ghosts_and_keeps_truly_open(self, db):
        assert repair_ghosts(db) == 2
        truly_open, ghosts = classify_open_positions(db)
        assert [p["trade_id"] for p in truly_open] == [1]
        assert ghosts == []
        conn = sqlite3.connect(db)
        statuses = dict(conn.execute(
            "SELECT trade_id, status FROM positions WHERE trade_id IN (2,3)"))
        conn.close()
        assert statuses == {2: "CLOSED_ELSEWHERE", 3: "CLOSED_ELSEWHERE"}

    def test_repair_is_idempotent(self, db):
        repair_ghosts(db)
        assert repair_ghosts(db) == 0


class TestCancelOrphanAttempts:
    def test_cancels_only_attempts_without_positions(self, db):
        conn = sqlite3.connect(db)
        # orphan: PENDING, never became a position (blocked at the risk gate)
        conn.execute("INSERT INTO trade_history (id, timestamp, market_question, status) "
                     "VALUES (5, '2026-08-01 00:00:00', 'blocked attempt', 'PENDING')")
        conn.commit()
        conn.close()
        assert cancel_orphan_attempts(db) == 1
        conn = sqlite3.connect(db)
        statuses = dict(conn.execute(
            "SELECT id, status FROM trade_history WHERE id IN (1, 5)"))
        conn.close()
        # the real open trade (has a positions row) must stay PENDING
        assert statuses == {1: "PENDING", 5: "CANCELLED"}

    def test_idempotent(self, db):
        cancel_orphan_attempts(db)
        assert cancel_orphan_attempts(db) == 0
