"""Tests for the cost model and the performance measurement harness."""

from decimal import Decimal

from polymarket_scanner.costs import (
    round_trip_cost,
    net_edge,
    covers_costs,
    net_exit_value,
    half_spread,
)
from polymarket_scanner.metrics import ClosedTrade, compute_metrics, metrics_by_strategy


class TestCosts:
    def test_round_trip_cost_positive(self):
        # 2% fee + 0.5% slippage per leg, two legs → ~5%
        c = round_trip_cost(Decimal("0.30"))
        assert Decimal("0.04") < c < Decimal("0.06")

    def test_net_edge_subtracts_costs(self):
        gross = Decimal("0.10")
        assert net_edge(gross, Decimal("0.30")) < gross

    def test_covers_costs_rejects_thin_edge(self):
        # A 1% gross edge cannot survive ~5% round-trip cost.
        assert covers_costs(Decimal("0.01"), Decimal("0.30")) is False

    def test_covers_costs_accepts_fat_edge(self):
        assert covers_costs(Decimal("0.15"), Decimal("0.30")) is True

    def test_half_spread(self):
        # bid 0.28 / ask 0.32 → mid 0.30, half-spread 0.02 → 0.02/0.30
        hs = half_spread(Decimal("0.28"), Decimal("0.32"))
        assert abs(hs - (Decimal("0.02") / Decimal("0.30"))) < Decimal("0.0001")

    def test_net_exit_value_pays_fee(self):
        # 10 shares @ 0.40 bid = $4.00 gross, minus 2% fee = $3.92
        assert net_exit_value(Decimal("10"), Decimal("0.40")) == Decimal("3.92")


class TestMetrics:
    def _trades(self):
        return [
            ClosedTrade("MOMENTUM", Decimal("0.10"), Decimal("10"), Decimal("2.00")),
            ClosedTrade("MOMENTUM", Decimal("0.10"), Decimal("10"), Decimal("-1.00")),
            ClosedTrade("CORRELATED", Decimal("0.20"), Decimal("5"), Decimal("1.00")),
        ]

    def test_win_rate_and_counts(self):
        m = compute_metrics(self._trades())
        assert m.trades == 3
        assert m.wins == 2
        assert m.losses == 1
        assert abs(m.win_rate - 2 / 3) < 1e-9

    def test_profit_factor(self):
        m = compute_metrics(self._trades())
        # gross profit 3.00 / gross loss 1.00 = 3.0
        assert abs(m.profit_factor - 3.0) < 1e-9

    def test_net_pnl_and_expectancy(self):
        m = compute_metrics(self._trades())
        assert m.net_pnl == Decimal("2.00")
        assert abs(float(m.expectancy) - 2.0 / 3) < 1e-9

    def test_cost_adjustment_reduces_pnl(self):
        m = compute_metrics(self._trades())
        assert m.net_pnl_after_costs < m.net_pnl

    def test_max_drawdown(self):
        # equity curve: +2 (peak 2) → +1 (dd 1) → +2 → max dd = 1
        m = compute_metrics(self._trades())
        assert m.max_drawdown == Decimal("1.00")

    def test_all_losers_profit_factor_zero(self):
        losers = [ClosedTrade("X", Decimal("0.1"), Decimal("5"), Decimal("-1"))]
        m = compute_metrics(losers)
        assert m.profit_factor == 0.0

    def test_group_by_strategy(self):
        groups = metrics_by_strategy(self._trades())
        assert set(groups) == {"MOMENTUM", "CORRELATED"}
        assert groups["MOMENTUM"].trades == 2
