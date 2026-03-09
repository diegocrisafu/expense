"""Risk Manager — Per-Strategy Capital Allocation & Position Sizing.

PURPOSE:
Instead of betting $4 on everything (50% of an $8 balance!), this module
dynamically allocates capital by strategy with hard per-trade and per-strategy
limits.  No single trade should risk more than ~10% of the bankroll, and no
single strategy should consume more than 40%.

CAPITAL BUDGET (example on $8.03 balance):
┌────────────────────┬──────────┬───────────┬──────────────────┐
│ Strategy           │ Alloc %  │ Max $     │ Per-trade max    │
├────────────────────┼──────────┼───────────┼──────────────────┤
│ Arbitrage          │ 30%      │ $2.41     │ $1.20            │
│ Swing / Scalp      │ 30%      │ $2.41     │ $0.80            │
│ Momentum           │ 15%      │ $1.20     │ $0.60            │
│ Smart (MR/Corr/VS) │ 15%      │ $1.20     │ $0.60            │
│ Whale Follow       │ 10%      │ $0.80     │ $0.40            │
├────────────────────┼──────────┼───────────┼──────────────────┤
│ Reserve (untouched) │          │ $1.00     │ (stop-loss)      │
└────────────────────┴──────────┴───────────┴──────────────────┘

KEY RULES:
1. Never risk >10% of current balance on a single trade
2. Never allocate >40% of balance to one strategy
3. Keep $1.00 reserve (stop-loss floor)
4. Scale bet sizes UP as balance grows, DOWN as it shrinks
5. Strategy budgets refresh each cycle (freed capital gets re-pooled)
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from .database import get_connection, DB_PATH
from .trading_config import STOP_LOSS_THRESHOLD

logger = logging.getLogger(__name__)


# ─── Strategy budget configuration ───
@dataclass
class StrategyBudget:
    """How much capital a strategy is allowed to use."""
    name: str
    allocation_pct: Decimal     # % of available capital (after reserve)
    max_per_trade_pct: Decimal  # max % of balance per single trade
    max_open_positions: int     # max concurrent positions for this strategy
    # Tighter exits for scalp, wider for long-hold
    take_profit_pct: Decimal = Decimal("0.15")
    stop_loss_pct: Decimal = Decimal("0.08")
    trailing_stop_pct: Decimal = Decimal("0.06")
    max_hold_hours: int = 72

    def per_trade_limit(self, balance: Decimal) -> Decimal:
        """Max dollars for a single trade given current balance."""
        return (balance * self.max_per_trade_pct).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )

    def total_budget(self, available: Decimal) -> Decimal:
        """Total capital this strategy can deploy."""
        return (available * self.allocation_pct).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )


# ─── Pre-defined strategy profiles ───
# STRATEGY ALLOCATION RATIONALE (based on historical P&L data):
#   MOMENTUM:   +$25.82 net (86% WR) → Give it 40% of capital
#   CORRELATED: +$18.80 net (100% WR) → Give it 40% of capital
#   CONTRARIAN: untested but sound logic → Give it 15% of capital
#   SWING:      -$4.17 net (big losses on expensive entries) → DISABLED (0%)
#   ARB:        0 trades ever in thousands of cycles → DISABLED (0%)
#
# KEY INSIGHT: All big winners bought cheap (<$0.18) and rode up.
# Stop buying expensive outcomes that have tiny upside and large downside.
STRATEGY_PROFILES: dict[str, StrategyBudget] = {
    # Arbitrage: Keep alive — risk-free when found.
    "ARB": StrategyBudget(
        name="ARB",
        allocation_pct=Decimal("0.10"),
        max_per_trade_pct=Decimal("0.08"),
        max_open_positions=2,
        take_profit_pct=Decimal("0.05"),
        stop_loss_pct=Decimal("0.03"),
        trailing_stop_pct=Decimal("0.02"),
        max_hold_hours=24,
    ),
    # SWING: Opened up — MAX_ENTRY_PRICE now prevents expensive entries.
    "SWING": StrategyBudget(
        name="SWING",
        allocation_pct=Decimal("0.20"),
        max_per_trade_pct=Decimal("0.08"),
        max_open_positions=4,
        take_profit_pct=Decimal("0.08"),
        stop_loss_pct=Decimal("0.05"),
        trailing_stop_pct=Decimal("0.04"),
        max_hold_hours=24,
    ),
    # MOMENTUM: STAR PERFORMER — give it the most capital.
    "MOMENTUM": StrategyBudget(
        name="MOMENTUM",
        allocation_pct=Decimal("0.35"),
        max_per_trade_pct=Decimal("0.10"),
        max_open_positions=5,
        take_profit_pct=Decimal("0.40"),
        stop_loss_pct=Decimal("0.25"),
        trailing_stop_pct=Decimal("0.12"),
        max_hold_hours=48,
    ),
    # CORRELATED: 2nd BEST — structural mispricings are reliable.
    "CORRELATED": StrategyBudget(
        name="CORRELATED",
        allocation_pct=Decimal("0.30"),
        max_per_trade_pct=Decimal("0.10"),
        max_open_positions=5,
        take_profit_pct=Decimal("0.40"),
        stop_loss_pct=Decimal("0.25"),
        trailing_stop_pct=Decimal("0.12"),
        max_hold_hours=48,
    ),
    # CONTRARIAN: Opened up — give it more room.
    "CONTRARIAN": StrategyBudget(
        name="CONTRARIAN",
        allocation_pct=Decimal("0.15"),
        max_per_trade_pct=Decimal("0.08"),
        max_open_positions=3,
        take_profit_pct=Decimal("0.35"),
        stop_loss_pct=Decimal("0.20"),
        trailing_stop_pct=Decimal("0.10"),
        max_hold_hours=48,
    ),
}


class RiskManager:
    """Central risk management — decides if a trade is allowed and how big it should be."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH

    def get_available_capital(self, balance: Decimal) -> Decimal:
        """Capital available for trading = balance minus safety reserve."""
        available = balance - STOP_LOSS_THRESHOLD
        return max(Decimal("0"), available)

    def get_deployed_by_strategy(self, strategy: str) -> tuple[Decimal, int]:
        """How much capital is currently deployed in active positions for a strategy.

        Returns:
            (total_cost_basis, open_position_count)
        """
        try:
            with get_connection(self.db_path) as conn:
                cursor = conn.cursor()
                # Match strategy by checking trade_history JOIN managed_positions
                cursor.execute("""
                    SELECT COALESCE(SUM(CAST(mp.cost_basis AS FLOAT)), 0),
                           COUNT(*)
                    FROM managed_positions mp
                    JOIN trade_history th ON mp.trade_id = th.id
                    WHERE mp.status = 'ACTIVE'
                      AND UPPER(th.strategy) = UPPER(?)
                """, (strategy,))
                row = cursor.fetchone()
                return (Decimal(str(row[0])), row[1]) if row else (Decimal("0"), 0)
        except Exception as e:
            logger.debug(f"get_deployed_by_strategy error: {e}")
            return Decimal("0"), 0

    def get_total_deployed(self) -> Decimal:
        """Total capital in all active positions."""
        try:
            with get_connection(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT COALESCE(SUM(CAST(cost_basis AS FLOAT)), 0)
                    FROM managed_positions WHERE status = 'ACTIVE'
                """)
                row = cursor.fetchone()
                return Decimal(str(row[0])) if row else Decimal("0")
        except Exception:
            return Decimal("0")

    def check_trade(
        self,
        strategy: str,
        proposed_size: Decimal,
        balance: Decimal,
    ) -> tuple[bool, Decimal, str]:
        """Central gatekeeper: should this trade happen?  If so, how much?

        Args:
            strategy: Strategy name (ARB, SWING, MOMENTUM, etc.)
            proposed_size: The amount the strategy wants to bet
            balance: Current USDC balance

        Returns:
            (allowed, adjusted_size, reason)
        """
        profile = STRATEGY_PROFILES.get(strategy.upper())
        if not profile:
            # Unknown strategy — use conservative defaults
            profile = StrategyBudget(
                name=strategy,
                allocation_pct=Decimal("0.05"),
                max_per_trade_pct=Decimal("0.05"),
                max_open_positions=1,
            )

        available = self.get_available_capital(balance)
        if available <= Decimal("0"):
            return False, Decimal("0"), f"No available capital (balance ${balance}, reserve ${STOP_LOSS_THRESHOLD})"

        # Check 1: Strategy budget
        budget = profile.total_budget(available)
        deployed, open_count = self.get_deployed_by_strategy(strategy)
        remaining_budget = budget - deployed

        if remaining_budget <= Decimal("0"):
            return False, Decimal("0"), f"{strategy} budget exhausted (${deployed:.2f} / ${budget:.2f})"

        # Check 2: Max open positions for this strategy
        if open_count >= profile.max_open_positions:
            return False, Decimal("0"), f"{strategy} max positions reached ({open_count}/{profile.max_open_positions})"

        # Check 3: Per-trade limit
        per_trade_max = profile.per_trade_limit(balance)

        # Check 4: Hard 10% rule per trade (was 5% — too restrictive for small balances)
        absolute_max = (balance * Decimal("0.10")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Take the minimum of all limits
        max_allowed = min(
            proposed_size,
            per_trade_max,
            absolute_max,
            remaining_budget,
            available,
        )

        # Ensure minimum viable bet ($0.10 on Polymarket with 5 share minimum)
        if max_allowed < Decimal("0.10"):
            return False, Decimal("0"), f"Trade too small after risk limits (${max_allowed:.2f})"

        reason = (
            f"{strategy}: ${max_allowed:.2f} "
            f"(budget ${remaining_budget:.2f}/{budget:.2f}, "
            f"per-trade cap ${per_trade_max:.2f}, "
            f"{open_count}/{profile.max_open_positions} positions)"
        )

        return True, max_allowed, reason

    def get_strategy_profile(self, strategy: str) -> StrategyBudget:
        """Get the exit/risk profile for a strategy."""
        return STRATEGY_PROFILES.get(
            strategy.upper(),
            StrategyBudget(
                name=strategy,
                allocation_pct=Decimal("0.05"),
                max_per_trade_pct=Decimal("0.05"),
                max_open_positions=1,
            ),
        )

    def print_allocation_report(self, balance: Decimal):
        """Print a summary of how capital is allocated."""
        available = self.get_available_capital(balance)
        total_deployed = self.get_total_deployed()

        print(f"\n{'─' * 60}")
        print(f"💼 CAPITAL ALLOCATION (Balance: ${balance:.2f})")
        print(f"   Reserve: ${STOP_LOSS_THRESHOLD} | Available: ${available:.2f} | Deployed: ${total_deployed:.2f}")
        print(f"{'─' * 60}")
        print(f"   {'Strategy':<16} {'Budget':>8} {'Deployed':>10} {'Free':>8} {'Pos':>5} {'Max':>5}")
        print(f"   {'─'*16} {'─'*8} {'─'*10} {'─'*8} {'─'*5} {'─'*5}")

        for name, profile in STRATEGY_PROFILES.items():
            budget = profile.total_budget(available)
            deployed, count = self.get_deployed_by_strategy(name)
            free = max(Decimal("0"), budget - deployed)
            print(
                f"   {name:<16} ${budget:>6.2f}  ${deployed:>8.2f}  ${free:>6.2f}  {count:>3}  /{profile.max_open_positions}"
            )

        idle = available - total_deployed
        print(f"\n   Idle capital: ${max(Decimal('0'), idle):.2f}")
        print(f"{'─' * 60}")
