"""Smart Exit Engine — Dynamic Position Reassessment.

Every cycle, this engine re-evaluates each open position by asking:
  1. Has the edge we entered on disappeared or flipped?
  2. Is momentum reversing against us?
  3. Is volume drying up (liquidity risk)?
  4. Is the spread widening (market becoming unreliable)?
  5. Are we in profit but the trend is weakening?

Each factor produces a score (0–1), and the composite "position health"
drives the exit decision.  This replaces the old rigid TP/SL with
*calculated, market-aware* exits grounded in live data.

EXIT DECISIONS (from most to least urgent):
  EDGE_GONE       — Our original thesis is invalid.  Edge flipped negative.
  MOMENTUM_EXIT   — We're in profit but momentum just reversed.  Cash out.
  SMART_STOP      — Position health is critical and we're losing.  Cut now.
  SMART_TAKE      — Small profit + deteriorating conditions.  Take the win.
  LIQUIDITY_EXIT  — Volume/spread degrading.  Exit while we still can.
  TIGHTEN_TRAIL   — Not exiting yet, but tighten the trailing stop.

All decisions are pure computation — no API calls.  The caller supplies
the market snapshot and this module returns a verdict.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Market snapshot — everything we know about a position right now
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class MarketSnapshot:
    """Live market data for a position's token, fetched each cycle."""
    bid: Decimal              # best bid (what we'd actually get selling)
    ask: Decimal              # best ask
    mid: Decimal              # (bid + ask) / 2
    spread: Decimal           # ask - bid
    spread_pct: Decimal       # spread / mid

    volume_24h: Decimal       # current 24h volume (USD)
    momentum_1h: Decimal      # 1-hour price change as decimal (-0.05 = -5%)

    book_depth_bid: Decimal   # total USD on bid side of book
    book_depth_ask: Decimal   # total USD on ask side of book

    # Edge re-analysis (from edge.py)
    current_edge: Decimal     # edge on our side RIGHT NOW (can be negative)
    edge_at_entry: Decimal    # edge when we entered (for comparison)


@dataclass
class PositionContext:
    """Everything about the position itself (from position_manager)."""
    entry_price: Decimal
    current_price: Decimal
    high_water_mark: Decimal
    size: Decimal             # shares
    cost_basis: Decimal
    hold_hours: float
    side: str                 # YES / NO
    strategy: str             # MOMENTUM / SWING / CORRELATED / etc.

    @property
    def pnl_pct(self) -> float:
        """Unrealized P&L as a percentage."""
        if self.entry_price == 0:
            return 0.0
        return float((self.current_price - self.entry_price) / self.entry_price)

    @property
    def pnl_usd(self) -> Decimal:
        return (self.current_price - self.entry_price) * self.size

    @property
    def drawdown_from_peak(self) -> float:
        """How far price has fallen from the high water mark (0 = at peak)."""
        if self.high_water_mark == 0:
            return 0.0
        return float((self.high_water_mark - self.current_price) / self.high_water_mark)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Smart exit verdict
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class SmartExitVerdict:
    """The engine's decision for one position."""
    should_exit: bool
    reason: str               # EDGE_GONE, MOMENTUM_EXIT, SMART_STOP, SMART_TAKE, LIQUIDITY_EXIT, HOLD
    urgency: int              # 1=low, 2=medium, 3=critical
    health_score: float       # composite 0–1 (1 = healthy, 0 = critical)

    # Sub-scores for debugging/logging
    edge_score: float         # 0–1: is our edge intact?
    momentum_score: float     # 0–1: is momentum with us?
    volume_score: float       # 0–1: is there liquidity?
    spread_score: float       # 0–1: is spread reasonable?
    profit_trend_score: float # 0–1: is our profit growing or shrinking?

    # If should_exit=False, should we tighten the trailing stop?
    new_trailing_pct: Optional[Decimal] = None

    explanation: str = ""     # human-readable reasoning


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Score calculators — each evaluates one dimension
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _score_edge(snapshot: MarketSnapshot) -> float:
    """How healthy is our edge?

    1.0 = edge as good or better than entry
    0.5 = edge halved
    0.0 = edge gone or negative
    """
    entry_edge = float(snapshot.edge_at_entry)
    current_edge = float(snapshot.current_edge)

    if entry_edge <= 0:
        # We shouldn't have entered, but if current edge is positive, that's ok
        return min(1.0, max(0.0, current_edge * 10))

    if current_edge <= 0:
        # Edge has flipped — this is bad
        return 0.0

    # Ratio of current to entry edge
    ratio = current_edge / entry_edge
    return min(1.0, max(0.0, ratio))


def _score_momentum(snapshot: MarketSnapshot, pos: PositionContext) -> float:
    """Is momentum working for or against us?

    For a BUY position (YES or NO):
      - Positive momentum on our side → 1.0
      - Flat → 0.5
      - Momentum reversing against us → 0.0

    We care about the *direction* relative to our position.
    """
    mom = float(snapshot.momentum_1h)

    # For YES positions, positive momentum is good
    # For NO positions, negative momentum is good (YES dropping = NO rising)
    if pos.side == "NO":
        mom = -mom

    # Map momentum to score
    # Strong positive (>3%) → 1.0
    # Flat (0%) → 0.5
    # Strong negative (<-3%) → 0.0
    score = 0.5 + (mom / 0.06)  # ±3% maps to 0–1
    return min(1.0, max(0.0, score))


def _score_volume(snapshot: MarketSnapshot) -> float:
    """Is there enough volume/liquidity to exit?

    Low volume means:
    1. Wide spreads (bad fill price)
    2. Can't exit at shown price (slippage)
    3. Market might be dying/resolved

    Score:
    1.0 = healthy volume (>$5k)
    0.5 = moderate ($1k-$5k)
    0.0 = dead market (<$100)
    """
    vol = float(snapshot.volume_24h)
    if vol <= 0:
        return 0.0

    # Log scale: $100→0.0, $1k→0.4, $5k→0.7, $20k→1.0
    score = math.log10(max(1, vol)) / math.log10(20000)
    return min(1.0, max(0.0, score))


def _score_spread(snapshot: MarketSnapshot) -> float:
    """Is the spread reasonable?

    Wide spread = market maker left, price unreliable, bad fills.
    Spread > 10% is dangerous.  < 2% is great.
    """
    spread_pct = float(snapshot.spread_pct)
    if spread_pct <= 0:
        return 1.0

    # 0% → 1.0, 5% → 0.5, 10% → 0.0
    score = 1.0 - (spread_pct / 0.10)
    return min(1.0, max(0.0, score))


def _score_profit_trend(pos: PositionContext) -> float:
    """Is our profit growing or shrinking?

    If we're profitable but price is falling from high_water_mark, the trend
    is weakening.  If we're at/near the high, we're strong.

    Score:
    1.0 = at high water mark (profit growing)
    0.5 = ~5% below high water mark
    0.0 = >10% below high water mark (profit evaporating)
    """
    drawdown = pos.drawdown_from_peak
    # 0% drawdown → 1.0, 5% → 0.5, 10%+ → 0.0
    score = 1.0 - (drawdown / 0.10)
    return min(1.0, max(0.0, score))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Composite health & decision logic
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Weights for composite health score (must sum to 1.0)
WEIGHTS = {
    "edge":         0.30,  # Edge is the most important signal
    "momentum":     0.25,  # Momentum reversal is a strong exit signal
    "volume":       0.15,  # Liquidity matters for execution
    "spread":       0.15,  # Spread quality affects real P&L
    "profit_trend": 0.15,  # Trend weakening = time to reassess
}

# Strategy-specific adjustments — some strategies care more about certain factors
STRATEGY_WEIGHT_OVERRIDES: dict[str, dict[str, float]] = {
    "SWING": {
        "momentum": 0.35,     # Swing trades live and die by momentum
        "edge": 0.20,
        "spread": 0.20,
        "volume": 0.15,
        "profit_trend": 0.10,
    },
    "MOMENTUM": {
        "momentum": 0.35,
        "profit_trend": 0.20,
        "edge": 0.20,
        "spread": 0.15,
        "volume": 0.10,
    },
    "CORRELATED": {
        "edge": 0.40,         # Correlation trades are edge-driven
        "spread": 0.20,
        "volume": 0.15,
        "momentum": 0.15,
        "profit_trend": 0.10,
    },
}

# Hold-time decay: the longer we hold, the more aggressively we exit.
# After MAX hours, health penalty maxes out.
HOLD_DECAY_MAX_HOURS = 36.0
HOLD_DECAY_PENALTY = 0.15     # Max 15% penalty at max hold time


def evaluate_position(
    snapshot: MarketSnapshot,
    pos: PositionContext,
) -> SmartExitVerdict:
    """Core decision function: should we exit this position?

    Combines all sub-scores into a composite health score, then applies
    strategy-specific and time-decay adjustments to make a final decision.
    """
    # 1. Calculate sub-scores
    edge_sc = _score_edge(snapshot)
    momentum_sc = _score_momentum(snapshot, pos)
    volume_sc = _score_volume(snapshot)
    spread_sc = _score_spread(snapshot)
    profit_sc = _score_profit_trend(pos)

    # 2. Get strategy-specific weights
    weights = STRATEGY_WEIGHT_OVERRIDES.get(pos.strategy.upper(), WEIGHTS)

    # 3. Composite health score
    health = (
        edge_sc * weights["edge"]
        + momentum_sc * weights["momentum"]
        + volume_sc * weights["volume"]
        + spread_sc * weights["spread"]
        + profit_sc * weights["profit_trend"]
    )

    # 4. Hold-time decay — the longer we hold, the lower health gets
    # This creates urgency to exit stale positions
    hold_decay = min(1.0, pos.hold_hours / HOLD_DECAY_MAX_HOURS) * HOLD_DECAY_PENALTY
    health -= hold_decay

    health = max(0.0, min(1.0, health))

    # 5. Decision logic — grounded in calculations, not guesses
    pnl_pct = pos.pnl_pct
    in_profit = pnl_pct > 0.005   # >0.5% to account for fees
    in_loss = pnl_pct < -0.005

    reason = "HOLD"
    should_exit = False
    urgency = 1
    new_trailing: Optional[Decimal] = None
    explanations: list[str] = []

    # ── CRITICAL: Edge has vanished ──
    if edge_sc == 0.0 and float(snapshot.current_edge) < -0.01:
        should_exit = True
        reason = "EDGE_GONE"
        urgency = 3
        explanations.append(
            f"Edge flipped negative ({float(snapshot.current_edge):+.1%}). "
            f"Original thesis invalid."
        )

    # ── CRITICAL: Health is very low and we're losing ──
    elif health < 0.25 and in_loss:
        should_exit = True
        reason = "SMART_STOP"
        urgency = 3
        explanations.append(
            f"Position health critical ({health:.2f}), losing {pnl_pct:+.1%}. "
            f"Cut losses before they deepen."
        )

    # ── MOMENTUM REVERSAL while in profit ──
    elif momentum_sc < 0.25 and in_profit and pnl_pct > 0.02:
        should_exit = True
        reason = "MOMENTUM_EXIT"
        urgency = 2
        explanations.append(
            f"Momentum reversed (score={momentum_sc:.2f}) while up {pnl_pct:+.1%}. "
            f"Locking in profit before reversal completes."
        )

    # ── SMART TAKE: small profit + deteriorating conditions ──
    elif health < 0.40 and in_profit:
        should_exit = True
        reason = "SMART_TAKE"
        urgency = 2
        explanations.append(
            f"Health declining ({health:.2f}) with {pnl_pct:+.1%} profit. "
            f"Taking the win — conditions are degrading."
        )

    # ── LIQUIDITY EXIT: volume and spread both bad ──
    elif volume_sc < 0.3 and spread_sc < 0.3 and pos.hold_hours > 2:
        should_exit = True
        reason = "LIQUIDITY_EXIT"
        urgency = 2
        explanations.append(
            f"Liquidity drying up (vol={volume_sc:.2f}, spread={spread_sc:.2f}). "
            f"Exit while we still can get filled."
        )

    # ── EDGE DETERIORATING: not gone, but much weaker than entry ──
    elif edge_sc < 0.3 and in_loss and pos.hold_hours > 4:
        should_exit = True
        reason = "SMART_STOP"
        urgency = 2
        explanations.append(
            f"Edge nearly gone (score={edge_sc:.2f}), losing {pnl_pct:+.1%} after {pos.hold_hours:.0f}h. "
            f"No reason to hold — original edge evaporated."
        )

    # ── TIGHTEN TRAILING STOP (don't exit, but protect gains) ──
    elif in_profit and health < 0.55:
        # Adaptive trailing: tighter when health is low
        # health=0.55 → 8% trail, health=0.30 → 3% trail
        adaptive_trail = 0.03 + (health - 0.30) * 0.20
        adaptive_trail = max(0.02, min(0.12, adaptive_trail))
        new_trailing = Decimal(str(round(adaptive_trail, 4)))
        explanations.append(
            f"Tightening trail to {adaptive_trail:.1%} (health={health:.2f}). "
            f"Protecting {pnl_pct:+.1%} profit."
        )

    # ── EXTENDED HOLD with weak health: gradually force action ──
    elif pos.hold_hours > 24 and health < 0.45:
        if in_profit:
            should_exit = True
            reason = "SMART_TAKE"
            urgency = 1
            explanations.append(
                f"Held {pos.hold_hours:.0f}h with declining health ({health:.2f}). "
                f"Taking {pnl_pct:+.1%} profit to free capital."
            )
        elif in_loss and health < 0.30:
            should_exit = True
            reason = "SMART_STOP"
            urgency = 2
            explanations.append(
                f"Held {pos.hold_hours:.0f}h, health={health:.2f}, losing {pnl_pct:+.1%}. "
                f"Cutting dead weight to redeploy capital."
            )

    if not explanations:
        explanations.append(f"Position healthy ({health:.2f}). Holding.")

    return SmartExitVerdict(
        should_exit=should_exit,
        reason=reason,
        urgency=urgency,
        health_score=health,
        edge_score=edge_sc,
        momentum_score=momentum_sc,
        volume_score=volume_sc,
        spread_score=spread_sc,
        profit_trend_score=profit_sc,
        new_trailing_pct=new_trailing,
        explanation=" | ".join(explanations),
    )
