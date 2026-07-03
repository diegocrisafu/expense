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
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Optional

from .database import get_connection, DB_PATH
from .trading_config import (
    STOP_LOSS_THRESHOLD,
    MAX_TRADE_FRACTION,
    MIN_ORDER_SHARES,
)

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_ZERO_PRICE = Decimal("0")


def order_cost(dollars: Decimal, price: Decimal) -> tuple[Decimal, Decimal]:
    """Convert a dollar budget into a real Polymarket order.

    Polymarket enforces a 5-share minimum per order.  Naively doing
    ``shares = max(dollars / price, 5)`` (the old code) SILENTLY inflates a
    small bet: at price $0.55 a $1.00 budget becomes 5 shares = $2.75, blowing
    straight past any dollar cap.  This helper is the single, honest converter:

    - shares are floored (ROUND_DOWN) so cost never drifts UP from rounding,
    - but never below the 5-share exchange minimum,
    - the returned cost is what the order will ACTUALLY spend (rounded up a cent
      to stay conservative).

    Callers must treat ``cost`` — not the input ``dollars`` — as the amount
    debited and risk-checked.  When ``cost > dollars`` it means the 5-share
    floor forced a larger order; the risk manager rejects that upstream.
    """
    if price <= _ZERO_PRICE:
        # Can't size without a price — return the exchange minimum defensively.
        return MIN_ORDER_SHARES, (MIN_ORDER_SHARES * Decimal("0.01"))
    raw_shares = (dollars / price).quantize(_CENT, rounding=ROUND_DOWN)
    shares = max(raw_shares, MIN_ORDER_SHARES)
    cost = (shares * price).quantize(_CENT, rounding=ROUND_UP)
    return shares, cost


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
# ALLOCATION RATIONALE — de-overfit from prior "P&L" numbers.
#   The old weights (MOMENTUM 40% "86% WR", CORRELATED 40% "100% WR") were fit
#   to a HANDFUL of paper trades on a $25 account — 100% WR means ~2-3 wins, not
#   an edge.  Allocations now start from a NEUTRAL prior and only tilt toward
#   strategies with structural (not sample-noise) justification.  As the metrics
#   harness accumulates a real sample, revisit these — don't hand-tune on <30
#   trades per strategy.
#
# INVARIANTS enforced here (asserted at import):
#   • allocation_pct across strategies sums to <= 1.00 (no phantom 110% capital)
#   • every max_per_trade_pct <= MAX_TRADE_FRACTION (5% hard ceiling)
#
# KEY STRUCTURAL INSIGHT retained: only buy cheap outcomes (<= MAX_ENTRY_PRICE);
# asymmetric upside with bounded downside is the one edge we can defend.
STRATEGY_PROFILES: dict[str, StrategyBudget] = {
    # Arbitrage: risk-free when found → highest confidence, but rare.
    "ARB": StrategyBudget(
        name="ARB",
        allocation_pct=Decimal("0.15"),
        max_per_trade_pct=Decimal("0.05"),
        max_open_positions=2,
        take_profit_pct=Decimal("0.05"),
        stop_loss_pct=Decimal("0.03"),
        trailing_stop_pct=Decimal("0.02"),
        max_hold_hours=24,
    ),
    # SWING: momentum-scalp on cheap entries.
    "SWING": StrategyBudget(
        name="SWING",
        allocation_pct=Decimal("0.20"),
        max_per_trade_pct=Decimal("0.05"),
        max_open_positions=4,
        take_profit_pct=Decimal("0.08"),
        stop_loss_pct=Decimal("0.05"),
        trailing_stop_pct=Decimal("0.04"),
        max_hold_hours=24,
    ),
    # MOMENTUM: trend continuation on cheap asymmetric bets.
    "MOMENTUM": StrategyBudget(
        name="MOMENTUM",
        allocation_pct=Decimal("0.25"),
        max_per_trade_pct=Decimal("0.05"),
        max_open_positions=3,
        take_profit_pct=Decimal("0.40"),
        stop_loss_pct=Decimal("0.25"),
        trailing_stop_pct=Decimal("0.12"),
        max_hold_hours=48,
    ),
    # CORRELATED: structural mispricings between related markets — edge-driven.
    "CORRELATED": StrategyBudget(
        name="CORRELATED",
        allocation_pct=Decimal("0.25"),
        max_per_trade_pct=Decimal("0.05"),
        max_open_positions=3,
        take_profit_pct=Decimal("0.40"),
        stop_loss_pct=Decimal("0.25"),
        trailing_stop_pct=Decimal("0.12"),
        max_hold_hours=48,
    ),
    # CONTRARIAN: mean-reversion — smallest allocation, least proven.
    "CONTRARIAN": StrategyBudget(
        name="CONTRARIAN",
        allocation_pct=Decimal("0.15"),
        max_per_trade_pct=Decimal("0.05"),
        max_open_positions=3,
        take_profit_pct=Decimal("0.35"),
        stop_loss_pct=Decimal("0.20"),
        trailing_stop_pct=Decimal("0.10"),
        max_hold_hours=48,
    ),
}

# ── Fail-fast invariants: catch a bad edit before it ever sizes a real trade ──
_alloc_sum = sum(p.allocation_pct for p in STRATEGY_PROFILES.values())
assert _alloc_sum <= Decimal("1.00"), (
    f"Strategy allocations sum to {_alloc_sum} (>100% of capital)"
)
assert all(
    p.max_per_trade_pct <= MAX_TRADE_FRACTION for p in STRATEGY_PROFILES.values()
), f"A strategy exceeds the {MAX_TRADE_FRACTION:.0%} per-trade hard cap"


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
        entry_price: Optional[Decimal] = None,
        gross_edge: Optional[Decimal] = None,
    ) -> tuple[bool, Decimal, str]:
        """Central gatekeeper: should this trade happen?  If so, how much?

        This is the SINGLE enforcement point for the 5% rule.  The returned
        size, once converted to a real Polymarket order (5-share minimum), is
        guaranteed never to cost more than MAX_TRADE_FRACTION of `balance`.

        Args:
            strategy: Strategy name (ARB, SWING, MOMENTUM, etc.)
            proposed_size: The dollar amount the strategy wants to bet
            balance: Current USDC balance
            entry_price: Per-share price.  When supplied, the gate accounts for
                the 5-share exchange minimum and REJECTS trades whose smallest
                legal order would breach the 5% cap (instead of silently
                inflating them, which the old code did).
            gross_edge: The strategy's estimated edge (as a price fraction).
                When supplied with a price and the cost-edge gate is enabled,
                the trade is REJECTED unless its edge beats round-trip costs by
                MIN_NET_EDGE, and the bet is sized by quarter-Kelly on the NET
                (after-cost) edge — better edges get more capital, up to 5%.
                This is the principled anti-(-EV) filter: it stops the bot from
                paying fees to take coin-flips.

        Returns:
            (allowed, adjusted_size, reason) — adjusted_size is the ACTUAL
            dollar cost to debit (already share-floor aware when price given).
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

        # Check 3: Per-trade limit (per-strategy)
        per_trade_max = profile.per_trade_limit(balance)

        # Check 4: HARD 5% rule per trade — the non-negotiable risk ceiling.
        absolute_max = (balance * MAX_TRADE_FRACTION).quantize(_CENT, rounding=ROUND_DOWN)

        # Check 5: Portfolio reserve — total deployed must never eat the reserve.
        # (Per-strategy budgets can collectively exceed available; this stops it.)
        already_deployed = self.get_total_deployed()
        portfolio_room = max(Decimal("0"), available - already_deployed)

        # Check 6: COST-EDGE GATE + edge-based sizing.  A trade whose edge cannot
        # beat round-trip costs is -EV — reject it.  Otherwise size by
        # quarter-Kelly on the net edge so better trades get more (still <=5%).
        edge_cap = absolute_max  # no-op unless edge supplied
        if gross_edge is not None and entry_price is not None and entry_price > Decimal("0"):
            from .trading_config import ENFORCE_COST_EDGE_GATE
            from .costs import net_edge, MIN_NET_EDGE
            from .edge import kelly_fraction

            ne = net_edge(Decimal(str(gross_edge)), entry_price)
            if ENFORCE_COST_EDGE_GATE and ne < MIN_NET_EDGE:
                return False, Decimal("0"), (
                    f"{strategy} rejected: net edge {ne:+.1%} < min {MIN_NET_EDGE:.1%} "
                    f"after fees+slippage (gross {Decimal(str(gross_edge)):+.1%} @ ${entry_price:.3f})"
                )
            kelly = kelly_fraction(ne, entry_price)  # quarter-Kelly, already capped
            if kelly > Decimal("0"):
                edge_cap = (balance * kelly).quantize(_CENT, rounding=ROUND_DOWN)

        # Take the minimum of all dollar limits
        max_allowed = min(
            proposed_size,
            per_trade_max,
            absolute_max,
            remaining_budget,
            portfolio_room,
            edge_cap,
        )

        if max_allowed < Decimal("0.10"):
            return False, Decimal("0"), (
                f"Trade too small after risk limits (${max_allowed:.2f}); "
                f"portfolio room ${portfolio_room:.2f}"
            )

        # Check 6: Share-floor reality check.  Convert the dollar budget into a
        # real order and verify the ACTUAL cost still respects the 5% ceiling.
        actual_cost = max_allowed
        if entry_price is not None and entry_price > Decimal("0"):
            shares, actual_cost = order_cost(max_allowed, entry_price)
            if actual_cost > absolute_max:
                # The 5-share minimum would push this order past 5%. Reject —
                # do NOT inflate the bet past the risk ceiling.
                return False, Decimal("0"), (
                    f"{strategy} rejected: min order {shares} sh @ ${entry_price:.3f} "
                    f"= ${actual_cost:.2f} > 5% cap ${absolute_max:.2f}"
                )
            if actual_cost > portfolio_room:
                return False, Decimal("0"), (
                    f"{strategy} rejected: min order ${actual_cost:.2f} exceeds "
                    f"portfolio room ${portfolio_room:.2f}"
                )

        reason = (
            f"{strategy}: ${actual_cost:.2f} "
            f"(budget ${remaining_budget:.2f}/{budget:.2f}, "
            f"5% cap ${absolute_max:.2f}, "
            f"{open_count}/{profile.max_open_positions} positions)"
        )

        return True, actual_cost, reason

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
