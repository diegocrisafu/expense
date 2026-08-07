"""The take-profit path must not book a phantom win at a near-$1 bid.

Historically the bot recorded "wins" like Switzerland-to-win-the-World-Cup
(bought at 2c) exiting at 99.9c, because the exit logic trusted a near-$1
orderbook bid on a market that had actually RESOLVED against the position.
The resolution guard verifies the real on-chain outcome and settles the token
we hold at its true value ($0 or $1).
"""

import asyncio
import sqlite3
from decimal import Decimal

from polymarket_scanner import position_manager as pm_mod
from polymarket_scanner.learning import LearningEngine
from polymarket_scanner.position_manager import PositionManager


def _pm_with_active_position(tmp_path, token_id, entry, size):
    db = str(tmp_path / "guard.db")
    LearningEngine(db_path=db)          # creates trade_history for the ledger sync
    pm = PositionManager(db_path=db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trade_history (id, strategy, market_id, token_id, side, entry_price, "
        "size, status) VALUES (1,'MOMENTUM','mkt',?,'BUY',?,?, 'PENDING')",
        (token_id, str(entry), str(size)),
    )
    conn.execute(
        "INSERT INTO managed_positions (trade_id, market_id, token_id, side, entry_price, "
        "size, cost_basis, current_price, high_water_mark, take_profit_price, stop_loss_price, "
        "trailing_stop_price, market_question, status, opened_at) "
        "VALUES (1,'mkt',?,'BUY',?,?,?,?,?,?,?,?,?, 'ACTIVE', '2026-07-09 00:00:00')",
        (token_id, str(entry), str(size), str(Decimal(entry) * Decimal(size)),
         str(entry), str(entry), str(Decimal(entry) * Decimal("1.4")),
         str(Decimal(entry) * Decimal("0.75")), str(entry),
         "Will Switzerland win the 2026 FIFA World Cup?"),
    )
    conn.commit()
    conn.close()
    return pm, db


def _closed_row(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT status, exit_reason, exit_price, exit_pnl FROM managed_positions WHERE trade_id=1"
    ).fetchone()
    conn.close()
    return r


def _patch(pm, monkeypatch, *, bid, settlement):
    async def fake_price(token_id):
        return (bid, bid)  # (mid, bid) both near $1
    async def fake_resolution(market_id):
        if settlement is None:
            return {"resolved": False}
        return {"resolved": True, "winning_outcome": "No",
                "settlement_by_token": settlement}
    monkeypatch.setattr(pm, "_fetch_live_price", fake_price)
    # position_manager imported the symbol, so patch it in that module namespace
    monkeypatch.setattr(pm_mod, "fetch_market_resolution", fake_resolution)


def test_losing_longshot_at_999_bid_is_booked_as_loss(tmp_path, monkeypatch):
    pm, db = _pm_with_active_position(tmp_path, "yes_tok", "0.02", "100")
    _patch(pm, monkeypatch, bid=Decimal("0.999"),
           settlement={"yes_tok": Decimal("0"), "no_tok": Decimal("1")})
    asyncio.run(pm.check_exits())
    row = _closed_row(db)
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "MARKET_RESOLVED"
    assert float(row["exit_price"]) == 0.0
    assert float(row["exit_pnl"]) < 0        # a LOSS, not a +$48 jackpot


def test_genuine_winner_still_books_the_real_win(tmp_path, monkeypatch):
    pm, db = _pm_with_active_position(tmp_path, "yes_tok", "0.30", "10")
    _patch(pm, monkeypatch, bid=Decimal("0.999"),
           settlement={"yes_tok": Decimal("1"), "no_tok": Decimal("0")})
    asyncio.run(pm.check_exits())
    row = _closed_row(db)
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "MARKET_RESOLVED"
    assert float(row["exit_pnl"]) > 0        # real win = 10 * (1 - 0.30)


def test_unconfirmed_high_bid_is_not_booked(tmp_path, monkeypatch):
    """If we can't confirm resolution and the bid is implausibly high, skip —
    don't book a suspicious fill."""
    pm, db = _pm_with_active_position(tmp_path, "yes_tok", "0.02", "100")
    _patch(pm, monkeypatch, bid=Decimal("0.999"), settlement=None)
    asyncio.run(pm.check_exits())
    row = _closed_row(db)
    assert row["status"] == "ACTIVE"          # left open, nothing fabricated
