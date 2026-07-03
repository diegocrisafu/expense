"""Unit tests for risk_manager module."""

import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock

from polymarket_scanner.risk_manager import (
    RiskManager,
    StrategyBudget,
    STRATEGY_PROFILES,
    order_cost,
)
from polymarket_scanner.trading_config import MAX_TRADE_FRACTION


class TestStrategyBudget:
    """Tests for StrategyBudget calculations."""

    def test_per_trade_limit(self):
        """Per-trade limit respects max_per_trade_pct."""
        budget = StrategyBudget(
            name="TEST",
            allocation_pct=Decimal("0.30"),
            max_per_trade_pct=Decimal("0.10"),
            max_open_positions=2,
        )
        # 10% of $20 = $2.00
        assert budget.per_trade_limit(Decimal("20.00")) == Decimal("2.00")

    def test_per_trade_limit_rounds_down(self):
        """Per-trade limit rounds DOWN to avoid overallocation."""
        budget = StrategyBudget(
            name="TEST",
            allocation_pct=Decimal("0.30"),
            max_per_trade_pct=Decimal("0.10"),
            max_open_positions=2,
        )
        # 10% of $3.33 = $0.333 → rounds down to $0.33
        assert budget.per_trade_limit(Decimal("3.33")) == Decimal("0.33")

    def test_total_budget(self):
        """Total budget is allocation_pct of available capital."""
        budget = StrategyBudget(
            name="TEST",
            allocation_pct=Decimal("0.40"),
            max_per_trade_pct=Decimal("0.05"),
            max_open_positions=2,
        )
        # 40% of $15 = $6.00
        assert budget.total_budget(Decimal("15.00")) == Decimal("6.00")


class TestRiskManager:
    """Tests for RiskManager.check_trade gatekeeper."""

    def _make_manager(self):
        """Create a RiskManager with a temp DB path."""
        return RiskManager(db_path=":memory:")

    @patch.object(RiskManager, "get_deployed_by_strategy", return_value=(Decimal("0"), 0))
    def test_allows_trade_within_budget(self, mock_deployed):
        """Trade within all limits is allowed."""
        mgr = self._make_manager()
        allowed, size, reason = mgr.check_trade(
            "MOMENTUM", Decimal("1.00"), Decimal("25.00")
        )
        assert allowed is True
        assert size > Decimal("0")

    @patch.object(RiskManager, "get_deployed_by_strategy", return_value=(Decimal("0"), 0))
    def test_blocks_when_balance_below_reserve(self, mock_deployed):
        """Trade blocked when balance <= stop-loss threshold ($5)."""
        mgr = self._make_manager()
        allowed, size, reason = mgr.check_trade(
            "MOMENTUM", Decimal("1.00"), Decimal("5.00")
        )
        assert allowed is False
        assert "No available capital" in reason

    @patch.object(RiskManager, "get_deployed_by_strategy", return_value=(Decimal("0"), 0))
    def test_enforces_5pct_absolute_cap(self, mock_deployed):
        """Per-trade size is capped at 5% of balance."""
        mgr = self._make_manager()
        allowed, size, reason = mgr.check_trade(
            "MOMENTUM", Decimal("10.00"), Decimal("20.00")
        )
        assert allowed is True
        # 5% of $20 = $1.00; trade should be capped at or below that
        assert size <= Decimal("1.00")

    @patch.object(RiskManager, "get_deployed_by_strategy")
    def test_blocks_when_max_positions_reached(self, mock_deployed):
        """Trade blocked when strategy has max open positions."""
        # MOMENTUM allows max 3 positions
        mock_deployed.return_value = (Decimal("3.00"), 3)
        mgr = self._make_manager()
        allowed, size, reason = mgr.check_trade(
            "MOMENTUM", Decimal("1.00"), Decimal("25.00")
        )
        assert allowed is False
        assert "max positions" in reason

    @patch.object(RiskManager, "get_deployed_by_strategy")
    def test_blocks_when_budget_exhausted(self, mock_deployed):
        """Trade blocked when strategy budget is fully deployed."""
        # Available capital = $25 - $5 = $20
        # MOMENTUM budget = 40% × $20 = $8.00
        # Already deployed $8.00 → budget exhausted
        mock_deployed.return_value = (Decimal("8.00"), 1)
        mgr = self._make_manager()
        allowed, size, reason = mgr.check_trade(
            "MOMENTUM", Decimal("1.00"), Decimal("25.00")
        )
        assert allowed is False
        assert "budget exhausted" in reason

    @patch.object(RiskManager, "get_deployed_by_strategy", return_value=(Decimal("0"), 0))
    def test_swing_strategy_is_enabled(self, mock_deployed):
        """SWING strategy is now enabled with 15% allocation."""
        mgr = self._make_manager()
        allowed, size, reason = mgr.check_trade(
            "SWING", Decimal("1.00"), Decimal("25.00")
        )
        assert allowed is True
        assert size > Decimal("0")

    @patch.object(RiskManager, "get_deployed_by_strategy", return_value=(Decimal("0"), 0))
    def test_rejects_tiny_trade(self, mock_deployed):
        """Trade too small after risk limits is rejected (< $0.10)."""
        mgr = self._make_manager()
        # Balance just above reserve ($5.05 available = $0.05)
        # 5% of $5.05 = $0.25, but ARB allocation is 5% × $0.05 = $0.00
        allowed, size, reason = mgr.check_trade(
            "ARB", Decimal("1.00"), Decimal("5.05")
        )
        assert allowed is False
        assert "too small" in reason.lower() or "budget exhausted" in reason.lower()

    def test_get_strategy_profile_known(self):
        """Known strategies return their profile."""
        mgr = self._make_manager()
        profile = mgr.get_strategy_profile("MOMENTUM")
        assert profile.name == "MOMENTUM"
        assert profile.allocation_pct == Decimal("0.25")

    def test_get_strategy_profile_unknown(self):
        """Unknown strategies get conservative defaults."""
        mgr = self._make_manager()
        profile = mgr.get_strategy_profile("NONEXISTENT")
        assert profile.max_per_trade_pct == Decimal("0.05")
        assert profile.max_open_positions == 1


class TestOrderCost:
    """Tests for the share-floor aware dollar→order converter."""

    def test_no_inflation_when_shares_above_min(self):
        """When budget buys >= 5 shares, cost stays <= budget."""
        shares, cost = order_cost(Decimal("1.00"), Decimal("0.10"))
        assert shares >= Decimal("5")
        assert cost <= Decimal("1.00")

    def test_five_share_floor_inflates_small_bets(self):
        """A tiny budget is bumped to the 5-share minimum cost (honestly reported)."""
        # $1.00 at $0.55 buys only 1.8 shares → forced to 5 shares = $2.75
        shares, cost = order_cost(Decimal("1.00"), Decimal("0.55"))
        assert shares == Decimal("5")
        assert cost == Decimal("2.75")  # the REAL cost, not the $1.00 input

    def test_zero_price_is_safe(self):
        shares, cost = order_cost(Decimal("1.00"), Decimal("0"))
        assert shares == Decimal("5")
        assert cost > Decimal("0")


class TestFivePercentInvariant:
    """The hard rule: no accepted trade may cost > 5% of balance."""

    @patch.object(RiskManager, "get_deployed_by_strategy", return_value=(Decimal("0"), 0))
    @patch.object(RiskManager, "get_total_deployed", return_value=Decimal("0"))
    def test_rejects_when_share_floor_would_breach_cap(self, m_total, m_dep):
        """A high price where even 5 shares exceeds 5% is REJECTED, not inflated."""
        mgr = self._make_manager()
        # balance $25 → 5% cap = $1.25. At $0.50, 5 shares = $2.50 > cap → reject.
        allowed, size, reason = mgr.check_trade(
            "MOMENTUM", Decimal("1.00"), Decimal("25.00"), entry_price=Decimal("0.50")
        )
        assert allowed is False
        assert "cap" in reason.lower()

    @patch.object(RiskManager, "get_deployed_by_strategy", return_value=(Decimal("0"), 0))
    @patch.object(RiskManager, "get_total_deployed", return_value=Decimal("0"))
    def test_accepted_trade_never_exceeds_5pct_fuzz(self, m_total, m_dep):
        """Fuzz: across balances and prices, accepted cost <= 5% of balance."""
        import random
        from polymarket_scanner.risk_manager import order_cost as _oc

        mgr = self._make_manager()
        random.seed(42)
        for _ in range(2000):
            balance = Decimal(str(round(random.uniform(6, 500), 2)))
            price = Decimal(str(round(random.uniform(0.02, 0.55), 2)))
            strat = random.choice(["ARB", "SWING", "MOMENTUM", "CORRELATED", "CONTRARIAN"])
            allowed, size, _ = mgr.check_trade(
                strat, Decimal("999"), balance, entry_price=price
            )
            if allowed:
                _, actual_cost = _oc(size, price)
                cap = balance * MAX_TRADE_FRACTION
                assert actual_cost <= cap + Decimal("0.01"), (
                    f"BREACH: {strat} cost ${actual_cost} > 5% (${cap}) "
                    f"at balance ${balance}, price ${price}"
                )

    def _make_manager(self):
        return RiskManager(db_path=":memory:")


def test_allocations_sum_within_capital():
    """Strategy allocations must not exceed 100% of capital (no phantom 110%)."""
    total = sum(p.allocation_pct for p in STRATEGY_PROFILES.values())
    assert total <= Decimal("1.00"), f"Allocations sum to {total}"


def test_every_strategy_respects_5pct_per_trade():
    """No strategy profile may allow > 5% per trade."""
    assert all(
        p.max_per_trade_pct <= MAX_TRADE_FRACTION
        for p in STRATEGY_PROFILES.values()
    )


class TestCostEdgeGate:
    """The gate that stops the bot paying fees to take coin-flips."""

    def _mgr(self):
        return RiskManager(db_path=":memory:")

    @patch.object(RiskManager, "get_deployed_by_strategy", return_value=(Decimal("0"), 0))
    @patch.object(RiskManager, "get_total_deployed", return_value=Decimal("0"))
    def test_rejects_edge_below_costs(self, m_total, m_dep):
        """A thin edge that can't beat round-trip costs is rejected."""
        mgr = self._mgr()
        # 1% gross edge vs ~5% round-trip cost → net negative → reject.
        allowed, size, reason = mgr.check_trade(
            "SWING", Decimal("1.00"), Decimal("100.00"),
            entry_price=Decimal("0.30"), gross_edge=Decimal("0.01"),
        )
        assert allowed is False
        assert "net edge" in reason.lower()

    @patch.object(RiskManager, "get_deployed_by_strategy", return_value=(Decimal("0"), 0))
    @patch.object(RiskManager, "get_total_deployed", return_value=Decimal("0"))
    def test_accepts_fat_edge(self, m_total, m_dep):
        """A healthy edge that clears costs is allowed."""
        mgr = self._mgr()
        allowed, size, reason = mgr.check_trade(
            "SWING", Decimal("5.00"), Decimal("100.00"),
            entry_price=Decimal("0.30"), gross_edge=Decimal("0.20"),
        )
        assert allowed is True
        assert size > Decimal("0")

    @patch.object(RiskManager, "get_deployed_by_strategy", return_value=(Decimal("0"), 0))
    @patch.object(RiskManager, "get_total_deployed", return_value=Decimal("0"))
    def test_bigger_edge_sizes_bigger(self, m_total, m_dep):
        """More edge → more capital (both still <= 5%)."""
        mgr = self._mgr()
        _, small, _ = mgr.check_trade(
            "SWING", Decimal("99"), Decimal("1000"),
            entry_price=Decimal("0.30"), gross_edge=Decimal("0.08"),
        )
        _, big, _ = mgr.check_trade(
            "SWING", Decimal("99"), Decimal("1000"),
            entry_price=Decimal("0.30"), gross_edge=Decimal("0.30"),
        )
        assert big > small
        # Both respect the 5% ceiling.
        assert big <= Decimal("1000") * MAX_TRADE_FRACTION

    @patch.object(RiskManager, "get_deployed_by_strategy", return_value=(Decimal("0"), 0))
    @patch.object(RiskManager, "get_total_deployed", return_value=Decimal("0"))
    def test_edge_sizing_never_breaches_5pct(self, m_total, m_dep):
        """Even a huge edge cannot push the bet past 5%."""
        mgr = self._mgr()
        _, size, _ = mgr.check_trade(
            "MOMENTUM", Decimal("99"), Decimal("100"),
            entry_price=Decimal("0.10"), gross_edge=Decimal("0.90"),
        )
        from polymarket_scanner.risk_manager import order_cost
        _, cost = order_cost(size, Decimal("0.10"))
        assert cost <= Decimal("100") * MAX_TRADE_FRACTION + Decimal("0.01")
