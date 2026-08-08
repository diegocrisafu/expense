"""Regression test: an arbitrage win must not be booked as a loss.

resolve_trade branched on `side == "BUY"` and sent everything else to the
SELL formula, so BUY_BOTH — where both legs are bought and one pays $1 —
had its P&L sign flipped.  Observed live 2026-08-08: the Spain arb settled
WON and was written to trade_history as status='WON' with pnl=-0.04252,
disagreeing with the positions ledger's +0.04252.
"""

import sqlite3
from decimal import Decimal

import pytest

from polymarket_scanner.learning import LearningEngine


@pytest.fixture
def engine(tmp_path):
    db = str(tmp_path / "t.db")
    eng = LearningEngine(db_path=db)
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE IF NOT EXISTS trade_history (
        id INTEGER PRIMARY KEY, timestamp TIMESTAMP, strategy TEXT,
        market_id TEXT, market_question TEXT, category TEXT, token_id TEXT,
        side TEXT, entry_price DECIMAL, size DECIMAL, status TEXT,
        exit_price DECIMAL, pnl DECIMAL, resolved_at TIMESTAMP)""")
    for tid, side, entry in ((1, "BUY_BOTH", "0.95748"), (2, "BUY", "0.20"), (3, "SELL", "0.20")):
        conn.execute(
            "INSERT INTO trade_history (id, strategy, category, side, entry_price, size, status) "
            "VALUES (?,?,?,?,?,?,'PENDING')",
            (tid, "ARB" if side == "BUY_BOTH" else "CORRELATED", "test", side, entry, 1),
        )
    conn.commit()
    conn.close()
    return eng


def pnl_of(engine, trade_id):
    conn = sqlite3.connect(engine.db_path)
    row = conn.execute(
        "SELECT status, pnl FROM trade_history WHERE id=?", (trade_id,)
    ).fetchone()
    conn.close()
    return row[0], Decimal(str(row[1]))


class TestArbPnlSign:
    def test_arb_win_is_booked_positive(self, engine):
        engine.resolve_trade(1, exit_price=Decimal("1"), won=True)
        status, pnl = pnl_of(engine, 1)
        assert status == "WON"
        assert pnl > 0, f"arb win booked as {pnl}"
        assert pnl == pytest.approx(Decimal("0.04252"), abs=Decimal("0.0001"))

    def test_status_and_sign_never_disagree(self, engine):
        engine.resolve_trade(1, exit_price=Decimal("1"), won=True)
        status, pnl = pnl_of(engine, 1)
        assert (status == "WON") == (pnl > 0)

    def test_plain_buy_unchanged(self, engine):
        engine.resolve_trade(2, exit_price=Decimal("1"), won=True)
        _, pnl = pnl_of(engine, 2)
        assert pnl == pytest.approx(Decimal("0.80"), abs=Decimal("0.0001"))

    def test_sell_side_unchanged(self, engine):
        engine.resolve_trade(3, exit_price=Decimal("0"), won=True)
        _, pnl = pnl_of(engine, 3)
        assert pnl == pytest.approx(Decimal("0.20"), abs=Decimal("0.0001"))
