"""Regression tests: resolution must be side-aware and query the real market.

Two pinned defects:
1. Side-blind settlement: any resolution_price > 0.99 booked a WIN for BUY
   positions even when OUR token lost.
2. The market lookup used a gamma query (?conditionId=) whose filter the API
   ignores, so an arbitrary unrelated market was inspected instead of ours.
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


def seed(tracker, trade_id, side="BUY"):
    conn = sqlite3.connect(tracker.db_path)
    conn.execute(
        "INSERT OR REPLACE INTO trade_history "
        "(id, status, market_question, strategy, category, entry_price, size, side) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (trade_id, "PENDING", "q", "CORRELATED", "test", 0.02, 50, side),
    )
    conn.commit()
    conn.close()
    tracker.record_position(trade_id=trade_id, market_id="0xm", token_id="tok_yes",
                            side=side, entry_price=Decimal("0.02"), size=Decimal("50"))


def pos(trade_id, side="BUY"):
    return PendingPosition(trade_id, "0xm", "tok_yes", side,
                           Decimal("0.02"), Decimal("50"), "q")


def resolution(winner):
    return {"resolved": True, "winner_token_id": winner,
            "winning_outcome": "?", "resolution_price": Decimal("1")}


class TestSideAwareSettlement:
    def test_books_loss_when_our_token_lost(self, tracker):
        seed(tracker, 1)
        pnl = asyncio.run(tracker.resolve_position(pos(1), resolution("tok_no")))
        assert pnl is not None and pnl < 0

    def test_books_win_when_our_token_won(self, tracker):
        seed(tracker, 2)
        pnl = asyncio.run(tracker.resolve_position(pos(2), resolution("tok_yes")))
        assert pnl == Decimal("50") * (Decimal("1") - Decimal("0.02"))

    def test_buy_both_wins_either_way(self, tracker):
        seed(tracker, 3, side="BUY_BOTH")
        pnl = asyncio.run(tracker.resolve_position(pos(3, "BUY_BOTH"), resolution("tok_no")))
        assert pnl == Decimal("50") * (Decimal("1") - Decimal("0.02"))

    def test_missing_winner_information_does_not_resolve(self, tracker):
        seed(tracker, 4)
        res = {"resolved": True, "resolution_price": Decimal("1")}
        assert asyncio.run(tracker.resolve_position(pos(4), res)) is None


class FakeResp:
    def __init__(self, code, payload):
        self.status_code = code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class TestMarketLookup:
    def test_resolution_comes_from_the_requested_market(self, tracker, monkeypatch):
        """A listing that ignores our filter must never be mistaken for our market."""
        target = {"condition_id": "0xtarget", "closed": True, "question": "target?",
                  "tokens": [{"token_id": "tok_yes", "outcome": "Yes", "winner": False},
                             {"token_id": "tok_no", "outcome": "No", "winner": True}]}
        unrelated = [{"conditionId": "0xother", "closed": False, "question": "other?",
                      "outcomes": ["Yes", "No"], "outcomePrices": '["0.5","0.5"]'}]

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kw):
                if url.rstrip("/").endswith("/markets/0xtarget"):
                    return FakeResp(200, target)
                return FakeResp(200, unrelated)

        monkeypatch.setattr("polymarket_scanner.resolution.httpx.AsyncClient",
                            lambda *a, **k: FakeClient())
        out = asyncio.run(tracker.check_market_resolution("0xtarget"))
        assert out is not None
        assert out.get("resolved") is True
        assert out.get("winner_token_id") == "tok_no"
