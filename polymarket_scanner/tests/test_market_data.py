"""Tests for the market-data capture pipeline and its backtest bridge."""

from decimal import Decimal

from polymarket_scanner.market_data import capture, load_series, snapshot_count
from polymarket_scanner.backtest import trade_from_capture, simulate_trade


def test_capture_and_load_roundtrip(tmp_path):
    db = str(tmp_path / "md.db")
    capture("tokA", Decimal("0.20"), Decimal("0.22"), Decimal("5000"), db_path=db)
    capture("tokA", Decimal("0.24"), Decimal("0.26"), Decimal("5100"), db_path=db)
    series = load_series("tokA", db_path=db)
    assert len(series) == 2
    assert series[0]["bid"] == Decimal("0.20")
    assert series[0]["mid"] == Decimal("0.21")   # (0.20+0.22)/2
    assert series[1]["bid"] == Decimal("0.24")


def test_capture_is_defensive_on_bad_input(tmp_path):
    db = str(tmp_path / "md.db")
    # No prices at all → silently ignored, never raises.
    capture("tokX", None, None, db_path=db)
    assert snapshot_count(db_path=db) == 0


def test_capture_never_raises(tmp_path):
    # A totally bogus db path must not raise (capture must never break trading).
    capture("t", Decimal("0.1"), Decimal("0.2"), db_path="/nonexistent/dir/x.db")


def test_backtest_bridge_from_captured_series(tmp_path):
    db = str(tmp_path / "md.db")
    # Rising price path for a token we "entered" at 0.20.
    for b in ("0.20", "0.24", "0.30"):
        capture("tokB", Decimal(b), Decimal(str(float(b) + 0.02)), db_path=db)
    trade = trade_from_capture("tokB", "MOMENTUM", Decimal("0.20"), Decimal("25"), db_path=db)
    assert trade is not None
    r = simulate_trade(trade)
    assert r.entered
    assert r.reason == "TAKE_PROFIT"   # rose through +40% TP


def test_bridge_returns_none_without_enough_data(tmp_path):
    db = str(tmp_path / "md.db")
    capture("tokC", Decimal("0.20"), Decimal("0.22"), db_path=db)  # only 1 obs
    assert trade_from_capture("tokC", "MOMENTUM", Decimal("0.20"), Decimal("25"), db_path=db) is None


def test_get_orderbook_captures_the_scanned_universe(monkeypatch):
    """Every CLOB orderbook fetch must record a snapshot (not just held tokens)."""
    import asyncio
    from unittest.mock import AsyncMock
    from polymarket_scanner.ingestion.clob import CLOBAPIClient
    import polymarket_scanner.market_data as md

    calls = []
    monkeypatch.setattr(md, "capture", lambda *a, **k: calls.append((a, k)))

    client = CLOBAPIClient()
    client._get = AsyncMock(return_value={
        "bids": [{"price": "0.30", "size": "100"}],
        "asks": [{"price": "0.34", "size": "100"}],
    })
    book = asyncio.run(client.get_orderbook("tokZ"))
    assert book is not None
    assert len(calls) == 1                     # capture was invoked
    args = calls[0][0]
    assert args[0] == "tokZ"                    # token id
    assert args[1] == Decimal("0.30")           # best bid
    assert args[2] == Decimal("0.34")           # best ask
