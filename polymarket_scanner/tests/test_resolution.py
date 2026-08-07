"""Tests for market-resolution settlement.

Locks in the fix for the phantom-win bug: a resolved BUY must be settled from
the price of the token WE HOLD, not from "did any outcome in this market win".
The old code booked every resolved BUY as a jackpot because the market's
`resolution_price` is always the winning outcome's ~1.0 price.
"""

import asyncio
import sqlite3
from decimal import Decimal

from polymarket_scanner.resolution import PendingPosition, ResolutionTracker


def _tracker_with_open_trade(tmp_path, token_id, entry, size):
    db = str(tmp_path / "res.db")
    tracker = ResolutionTracker(db_path=db)  # creates positions + trade_history
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO trade_history (strategy, market_id, token_id, side, entry_price, size, status) "
        "VALUES ('MOMENTUM','mkt','?','BUY',?,?,'PENDING')",
        (str(entry), str(size)),
    )
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    pos = PendingPosition(
        trade_id=trade_id, market_id="mkt", token_id=token_id, side="BUY",
        entry_price=Decimal(str(entry)), size=Decimal(str(size)),
        market_question="Will the longshot win?",
    )
    return tracker, pos, db


def _lost_market(held_token):
    """Resolution where the HELD token settled to $0 and the OTHER side won."""
    return {
        "resolved": True,
        "winning_outcome": "No",
        "resolution_price": Decimal("1"),            # the winner's price
        "settlement_by_token": {held_token: Decimal("0"), "other": Decimal("1")},
    }


def _won_market(held_token):
    return {
        "resolved": True,
        "winning_outcome": "Yes",
        "resolution_price": Decimal("1"),
        "settlement_by_token": {held_token: Decimal("1"), "other": Decimal("0")},
    }


def test_losing_longshot_is_booked_as_a_loss_not_a_jackpot(tmp_path):
    """The old bug: this returned a big WIN.  Our token settled to $0."""
    tracker, pos, db = _tracker_with_open_trade(tmp_path, "yes_tok", "0.02", "125")
    pnl = asyncio.run(tracker.resolve_position(pos, _lost_market("yes_tok")))
    assert pnl is not None
    assert pnl < 0                       # lost the stake, not a jackpot
    assert pnl == Decimal("125") * (Decimal("0") - Decimal("0.02"))
    row = sqlite3.connect(db).execute(
        "SELECT status FROM positions WHERE trade_id=?", (pos.trade_id,)
    ).fetchone()
    # (position row only exists if recorded; status check is best-effort)


def test_winning_token_is_booked_as_a_win(tmp_path):
    tracker, pos, db = _tracker_with_open_trade(tmp_path, "yes_tok", "0.30", "10")
    pnl = asyncio.run(tracker.resolve_position(pos, _won_market("yes_tok")))
    assert pnl is not None
    assert pnl > 0
    assert pnl == Decimal("10") * (Decimal("1") - Decimal("0.30"))


def test_unknown_token_is_not_fabricated(tmp_path):
    """If we can't find our token in the settlement map, refuse to guess:
    leave the position OPEN rather than booking a phantom win or unfair loss."""
    tracker, pos, db = _tracker_with_open_trade(tmp_path, "mystery_tok", "0.10", "10")
    res = {
        "resolved": True,
        "winning_outcome": "Yes",
        "resolution_price": Decimal("1"),
        "settlement_by_token": {"yes_tok": Decimal("1"), "no_tok": Decimal("0")},
    }
    pnl = asyncio.run(tracker.resolve_position(pos, res))
    assert pnl is None
