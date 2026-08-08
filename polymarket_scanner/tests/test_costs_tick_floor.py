"""Regression tests: round-trip cost must respect the absolute tick spread.

The cost model expressed friction purely as a percentage of price, which is
scale-invariant — but the order book's minimum spread is one tick ($0.001)
regardless of price.  At $0.55 a tick is 0.2% of price; at $0.006 it is 17%.
Modelling both as a flat 5% let sub-penny longshots clear a gate whose whole
job is rejecting -EV trades (observed live 2026-08-08: entries at $0.006,
$0.013 and $0.055 with books exactly one tick wide, i.e. -17%/-8%/-5%
mark-to-bid the instant they filled).
"""

from decimal import Decimal

import pytest

from polymarket_scanner.costs import (
    MIN_NET_EDGE,
    TICK_SIZE,
    covers_costs,
    net_edge,
    round_trip_cost,
)
from polymarket_scanner.risk_manager import RiskManager


class TestTickFloor:
    def test_penny_price_cost_reflects_the_tick(self):
        """At $0.006 one tick round trip is ~17%, not the flat 5%."""
        cost = round_trip_cost(Decimal("0.006"))
        assert cost > Decimal("0.16"), cost

    def test_normal_price_cost_is_unchanged(self):
        """At $0.55 a tick is negligible — the model must not move."""
        cost = round_trip_cost(Decimal("0.55"))
        assert cost == pytest.approx(Decimal("0.05"), abs=Decimal("0.005")), cost

    def test_cost_scales_inversely_with_price(self):
        assert (round_trip_cost(Decimal("0.006"))
                > round_trip_cost(Decimal("0.05"))
                > round_trip_cost(Decimal("0.50")))

    def test_observed_wider_book_beats_the_floor(self):
        """A live half-spread wider than a tick must still dominate."""
        wide = round_trip_cost(Decimal("0.50"), spread_frac=Decimal("0.10"))
        assert wide > round_trip_cost(Decimal("0.50"))

    def test_tick_size_matches_the_venue(self):
        assert TICK_SIZE == Decimal("0.001")


class TestGateOutcomes:
    def test_sub_tick_edge_on_a_longshot_is_rejected(self):
        """The Mark Cuban trade: a '7% edge' at $0.006 is under half a tick."""
        assert not covers_costs(Decimal("0.07"), Decimal("0.006"))

    def test_same_edge_at_a_normal_price_still_passes(self):
        """Proof this is scale correction, not a blanket tightening."""
        assert covers_costs(Decimal("0.10"), Decimal("0.50"))

    def test_net_edge_goes_negative_on_penny_longshots(self):
        assert net_edge(Decimal("0.10"), Decimal("0.006")) < Decimal("0")


class TestRiskManagerGate:
    """End-to-end through the gate, on an empty DB so position caps don't mask it."""

    @pytest.fixture
    def rm(self, tmp_path):
        return RiskManager(db_path=str(tmp_path / "t.db"))

    def test_risk_manager_rejects_the_penny_longshot(self, rm):
        allowed, size, reason = rm.check_trade(
            "CORRELATED", Decimal("1.00"), Decimal("25.00"),
            entry_price=Decimal("0.006"), gross_edge=Decimal("0.10"),
        )
        assert not allowed
        assert "net edge" in reason

    def test_risk_manager_still_allows_a_normal_priced_edge(self, rm):
        """Proof this is scale correction, not a blanket tightening.

        $0.20 rather than $0.50 because a $25 balance cannot reach $0.50 at
        all: the 5% cap ($1.25) over the 5-share minimum caps the tradable
        price at $0.25 — see test_affordable_ceiling.
        """
        allowed, size, reason = rm.check_trade(
            "CORRELATED", Decimal("1.00"), Decimal("25.00"),
            entry_price=Decimal("0.20"), gross_edge=Decimal("0.10"),
        )
        assert allowed, reason
        assert size > Decimal("0")

    def test_affordable_ceiling_is_the_binding_constraint_at_this_balance(self, rm):
        """The 5-share floor against the 5% cap bars everything above $0.25."""
        allowed, _, reason = rm.check_trade(
            "CORRELATED", Decimal("1.00"), Decimal("25.00"),
            entry_price=Decimal("0.50"), gross_edge=Decimal("0.50"),
        )
        assert not allowed
        assert "min order" in reason
