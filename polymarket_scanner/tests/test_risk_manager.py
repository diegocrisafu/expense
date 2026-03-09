"""Unit tests for risk_manager module."""

import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock

from polymarket_scanner.risk_manager import (
    RiskManager,
    StrategyBudget,
    STRATEGY_PROFILES,
)


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
        assert profile.allocation_pct == Decimal("0.35")

    def test_get_strategy_profile_unknown(self):
        """Unknown strategies get conservative defaults."""
        mgr = self._make_manager()
        profile = mgr.get_strategy_profile("NONEXISTENT")
        assert profile.max_per_trade_pct == Decimal("0.05")
        assert profile.max_open_positions == 1
