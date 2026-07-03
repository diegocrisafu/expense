"""Tests for the backtest engine — proves the exit/risk LOGIC behaves."""

from decimal import Decimal

from polymarket_scanner.backtest import (
    PriceStep, BacktestTrade, simulate_trade, run_backtest,
)


def _path(bids):
    return [PriceStep(Decimal(str(b)), float(i)) for i, b in enumerate(bids)]


def test_take_profit_exit_is_a_win():
    # MOMENTUM TP is +40%: entry 0.20 → TP at 0.28. Price rises through it.
    t = BacktestTrade("MOMENTUM", Decimal("0.20"), Decimal("25"), _path([0.22, 0.26, 0.30]))
    r = simulate_trade(t)
    assert r.entered
    assert r.reason == "TAKE_PROFIT"
    assert r.net_pnl > 0


def test_stop_loss_exit_is_a_loss():
    # MOMENTUM SL is -25%: entry 0.20 → SL at 0.15. Price falls through it.
    t = BacktestTrade("MOMENTUM", Decimal("0.20"), Decimal("25"), _path([0.18, 0.14]))
    r = simulate_trade(t)
    assert r.entered
    assert r.reason == "STOP_LOSS"
    assert r.net_pnl < 0


def test_rejects_when_5pct_cap_breached():
    # Tiny balance: 5 shares @ 0.50 = $2.50 > 5% of $10 ($0.50) → rejected.
    t = BacktestTrade("MOMENTUM", Decimal("0.50"), Decimal("10"), _path([0.6]))
    r = simulate_trade(t)
    assert not r.entered
    assert "CAP" in r.reason


def test_costs_make_marginal_move_a_net_loss():
    # Exit barely above entry: fees on the way out flip it negative.
    t = BacktestTrade("MOMENTUM", Decimal("0.20"), Decimal("25"), _path([0.201]))
    r = simulate_trade(t)
    assert r.entered
    assert r.net_pnl < 0  # gross ~flat, but exit fee makes it a real loss


def test_run_backtest_scores_batch():
    trades = [
        BacktestTrade("MOMENTUM", Decimal("0.20"), Decimal("25"), _path([0.30])),   # win
        BacktestTrade("MOMENTUM", Decimal("0.20"), Decimal("25"), _path([0.14])),   # loss
    ]
    metrics, results = run_backtest(trades)
    assert metrics.trades == 2
    assert metrics.wins == 1
    assert metrics.losses == 1
