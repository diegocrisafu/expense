"""Regression tests: exit fills must be plausible before gains are booked.

Every settlement-priced TAKE_PROFIT in this bot's history (six fills, all
verified against CLOB winner flags on 2026-08-07) was booked on the LOSING
side of a settled market — the book endpoint served the winning complement's
quote.  These tests pin the guard that blocks such fills.
"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from polymarket_scanner.position_manager import ManagedPosition, PositionManager


def make_pos(entry="0.02", question="Will Switzerland win the 2026 FIFA World Cup?"):
    entry = Decimal(entry)
    return ManagedPosition(
        position_id=1, trade_id=2761, market_id="0xswiss", token_id="tok_yes",
        side="BUY", entry_price=entry, size=Decimal("50"),
        cost_basis=Decimal("1.00"), current_price=entry, high_water_mark=entry,
        opened_at=datetime.utcnow() - timedelta(hours=5),
        market_question=question,
    )


@pytest.fixture
def pm(tmp_path):
    return PositionManager(executor=None, db_path=str(tmp_path / "t.db"))


def run(coro):
    return asyncio.run(coro)


def patch_prices(monkeypatch, pm, mid, bid):
    async def fake_price(token_id):
        return (Decimal(mid), Decimal(bid))
    monkeypatch.setattr(pm, "_fetch_live_price", fake_price)


def patch_market(monkeypatch, pm, closed, our_winner, our_price=None):
    async def fake_market(market_id):
        return {
            "closed": closed,
            "tokens": [
                {"token_id": "tok_yes", "winner": our_winner, "price": our_price},
                {"token_id": "tok_no", "winner": (not our_winner), "price": None},
            ],
        }
    monkeypatch.setattr(pm, "_fetch_clob_market", fake_market, raising=False)


class TestPhantomFillGuard:
    def test_settlement_bid_on_losing_token_blocks_exit(self, pm, monkeypatch):
        """A 0.999 bid for a token the market says LOST must not book a gain."""
        pos = make_pos()
        monkeypatch.setattr(pm, "_load_active_positions", lambda: [pos])
        patch_prices(monkeypatch, pm, "0.999", "0.999")
        patch_market(monkeypatch, pm, closed=True, our_winner=False)
        assert run(pm.check_exits()) == []

    def test_settlement_bid_on_winning_token_allows_exit(self, pm, monkeypatch):
        """A genuine redemption (our token actually won) still books."""
        pos = make_pos()
        monkeypatch.setattr(pm, "_load_active_positions", lambda: [pos])
        patch_prices(monkeypatch, pm, "0.999", "0.999")
        patch_market(monkeypatch, pm, closed=True, our_winner=True)
        assert [s.reason for s in run(pm.check_exits())] == ["TAKE_PROFIT"]

    def test_unverifiable_suspect_bid_blocks_exit(self, pm, monkeypatch):
        """If the market record can't be fetched, don't book — retry next cycle."""
        pos = make_pos()
        monkeypatch.setattr(pm, "_load_active_positions", lambda: [pos])
        patch_prices(monkeypatch, pm, "0.999", "0.999")

        async def fake_market(market_id):
            return None
        monkeypatch.setattr(pm, "_fetch_clob_market", fake_market, raising=False)
        assert run(pm.check_exits()) == []

    def test_ordinary_gain_needs_no_verification(self, pm, monkeypatch):
        """A normal-sized TP (no settlement price, < 4x entry) makes no extra call."""
        pos = make_pos(entry="0.35", question="ordinary market")
        monkeypatch.setattr(pm, "_load_active_positions", lambda: [pos])
        patch_prices(monkeypatch, pm, "0.50", "0.50")

        async def boom(market_id):
            raise AssertionError("verification must not be called for ordinary gains")
        monkeypatch.setattr(pm, "_fetch_clob_market", boom, raising=False)
        assert [s.reason for s in run(pm.check_exits())] == ["TAKE_PROFIT"]
