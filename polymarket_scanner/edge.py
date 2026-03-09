"""Probability & Edge Engine — Compare YES vs NO, find real edge.

CORE INSIGHT (the user's question):
  A YES at $0.99 does NOT mean NO is truly 1%.
  After spread, depth, and calibration, the true NO probability
  could be 2-4× higher than the listed price implies.

  Example — Ricky Martin halftime show:
    YES ask = $0.86  →  market says 86% chance
    YES bid = $0.84  →  you'd get $0.84 selling
    NO  ask = $0.16  (≈ 1 - bid)
    Spread  = $0.02  (the market's "vig")

    Raw midpoint   = 0.85  (halfway between ask and bid)
    Calibrated     = 0.84  (markets over-price favorites by ~3%)
    True YES prob  ≈ 0.84
    True NO  prob  ≈ 0.16

    YES edge = 0.84 - 0.86 = -0.02  (NEGATIVE — you'd overpay)
    NO  edge = 0.16 - 0.16 =  0.00  (break-even)
    → PASS.  Neither side has edge in a fairly-priced market.

  But if momentum is -5% (price dropping):
    Adjusted prob  ≈ 0.83
    YES edge = 0.83 - 0.86 = -0.03  (worse)
    NO  edge = 0.17 - 0.16 = +0.01  (slight edge on NO)
    → Still marginal, but now we KNOW NO is the better side.

This module:
  1. Computes executable implied probabilities (from ask/bid, not last trade)
  2. Validates complement logic (YES + NO should be ~1.0 after spread)
  3. Calibrates for favorite-longshot bias (99% ≠ 99% true prob)
  4. Compares YES vs NO edge → picks the profitable side
  5. Handles multi-outcome events (exclusive vs non-exclusive)
  6. Returns net edge after spread cost
  7. Provides Kelly fraction for sizing

All functions are pure computation — no API calls, no side effects.
Complexity: O(1) per market, O(n) per event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Minimum edge to justify a trade (after spread).
# 3% allows more opportunities through while still filtering noise.
MIN_EDGE = Decimal("0.03")

# Favorite-longshot shrinkage.  Markets systematically
# over-price favourites and under-price longshots.
# 3 % linear shrinkage toward 50 % corrects this.
CALIBRATION_SHRINK = Decimal("0.03")

# Maximum spread for swing/scalp trades.
# Raised from 4% to 6% — allows more scalp opportunities.
MAX_SPREAD_SCALP = Decimal("0.06")

# Maximum spread for any trade.
MAX_SPREAD_ANY = Decimal("0.20")

# Zero constant
_ZERO = Decimal("0")
_ONE = Decimal("1")
_HALF = Decimal("0.5")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data structures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass(frozen=True)
class MarketEdge:
    """Complete edge analysis for one binary market."""

    # ── Prices ──
    yes_ask: Decimal
    yes_bid: Decimal
    no_ask: Decimal          # ≈ 1 − yes_bid  (cost to buy NO)
    no_bid: Decimal          # ≈ 1 − yes_ask  (what you'd get selling NO)
    spread: Decimal          # yes_ask − yes_bid

    # ── Probability estimates ──
    raw_mid: Decimal         # (ask + bid) / 2
    calibrated_prob: Decimal # midpoint adjusted for fav-longshot bias
    true_prob: Decimal       # final estimate (calibration + momentum)

    # ── Per-side edge ──
    yes_edge: Decimal        # true_prob − yes_ask  (>0 means YES is underpriced)
    no_edge: Decimal         # (1 − true_prob) − no_ask  (>0 means NO is underpriced)

    # ── Recommendation ──
    best_side: str           # "YES" | "NO" | "PASS"
    best_price: Decimal
    best_edge: Decimal
    best_token_idx: int      # 0 = YES token, 1 = NO token

    # ── Sizing ──
    kelly: Decimal           # quarter-Kelly fraction for best side

    reason: str              # human-readable explanation


@dataclass(frozen=True)
class EventMispricing:
    """One mispriced outcome inside a multi-outcome event."""
    market_id: str
    question: str
    yes_price: Decimal
    fair_price: Decimal
    edge: Decimal            # fair − actual (positive = underpriced YES)
    side: str                # "YES" if underpriced, "NO" if overpriced
    token_idx: int           # 0 or 1


@dataclass(frozen=True)
class EventEdge:
    """Analysis of all contracts inside one event."""
    event_id: str
    event_title: str
    num_outcomes: int
    total_yes_prob: Decimal  # sum of all YES midpoints
    is_exclusive: bool       # should probs sum to 1?
    overround: Decimal       # total − 1.0 (positive = market takes vig)
    mispricings: list[EventMispricing]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Probability helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def calibrate_probability(raw_prob: Decimal) -> Decimal:
    """Pull extreme probabilities toward 50 %.

    Prediction markets over-price favourites:
      99 % market → resolves YES ~97 % of the time
       1 % market → resolves YES  ~3 % of the time
      50 % market → well-calibrated

    We apply a 3 % linear shrinkage toward 0.50.
    """
    return raw_prob * (_ONE - CALIBRATION_SHRINK) + _HALF * CALIBRATION_SHRINK


def estimate_true_prob(
    yes_ask: Decimal,
    yes_bid: Decimal,
    momentum: Optional[Decimal] = None,
    volume_24h: Optional[Decimal] = None,
) -> Decimal:
    """Best estimate of true YES probability from available data.

    Steps:
      1. Midpoint  — spread-neutral starting point
      2. Calibrate — correct for favourite-longshot bias
      3. Momentum  — if price is moving, true value is ahead of price
    """
    mid = (yes_ask + yes_bid) / 2
    p = calibrate_probability(mid)

    # Momentum: price trending → true value is further in that direction
    if momentum and momentum != _ZERO:
        # Volume-weight the momentum signal (high vol = more reliable)
        vol_factor = min(_ONE, (volume_24h or _ZERO) / Decimal("20000"))
        shift = momentum * Decimal("0.25") * vol_factor
        # Cap shift to ±5 %
        shift = max(Decimal("-0.05"), min(Decimal("0.05"), shift))
        p += shift

    return max(Decimal("0.005"), min(Decimal("0.995"), p))


def kelly_fraction(edge: Decimal, price: Decimal) -> Decimal:
    """Quarter-Kelly fraction for bet sizing.

    Full Kelly: f* = edge / (1 − price)
    We use ¼ Kelly for safety (reduces variance ~75 %, costs ~6 % EV).
    """
    if price <= _ZERO or price >= _ONE or edge <= _ZERO:
        return _ZERO
    full = edge / (_ONE - price)
    return max(_ZERO, min(Decimal("0.25"), full * Decimal("0.25")))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Binary market analysis
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def analyze_binary_market(
    yes_ask: Decimal,
    yes_bid: Decimal,
    momentum: Optional[Decimal] = None,
    volume_24h: Optional[Decimal] = None,
    external_prob: Optional[Decimal] = None,
) -> MarketEdge:
    """Full edge analysis for a binary (YES/NO) market.

    Compares BOTH sides, accounts for spread and calibration,
    returns the side with the best risk-adjusted edge.

    Args:
        yes_ask:  Cost to buy YES (best ask)
        yes_bid:  Revenue from selling YES (best bid)
        momentum: 1-hour price change (positive = YES trending up)
        volume_24h: 24 h volume in USD
        external_prob: Override true-prob estimate if available
    """
    # Derive NO prices from YES (Polymarket YES+NO tokens are complements)
    no_ask = _ONE - yes_bid        # cost to buy NO
    no_bid = _ONE - yes_ask        # revenue from selling NO
    spread = yes_ask - yes_bid
    raw_mid = (yes_ask + yes_bid) / 2

    # True probability estimate
    calibrated = calibrate_probability(raw_mid)
    true_prob = (
        external_prob
        if external_prob is not None
        else estimate_true_prob(yes_ask, yes_bid, momentum, volume_24h)
    )

    # Edge per side
    yes_edge = true_prob - yes_ask
    no_edge = (_ONE - true_prob) - no_ask

    # Pick the better side
    if spread > MAX_SPREAD_ANY:
        # Spread too wide — unreliable pricing
        best_side, best_edge, best_price, idx, k = (
            "PASS", max(yes_edge, no_edge),
            yes_ask if yes_edge >= no_edge else no_ask,
            0 if yes_edge >= no_edge else 1,
            _ZERO,
        )
        reason = f"Spread too wide ({spread:.1%}) — prices unreliable"
    elif yes_edge >= no_edge and yes_edge > MIN_EDGE:
        best_side, best_edge, best_price, idx = "YES", yes_edge, yes_ask, 0
        k = kelly_fraction(yes_edge, yes_ask)
        reason = (
            f"YES +{yes_edge:.1%} vs NO {no_edge:+.1%} | "
            f"prob≈{true_prob:.0%} spread={spread:.1%}"
        )
    elif no_edge > yes_edge and no_edge > MIN_EDGE:
        best_side, best_edge, best_price, idx = "NO", no_edge, no_ask, 1
        k = kelly_fraction(no_edge, no_ask)
        reason = (
            f"NO +{no_edge:.1%} vs YES {yes_edge:+.1%} | "
            f"prob≈{true_prob:.0%} spread={spread:.1%}"
        )
    else:
        best_side = "PASS"
        best_edge = max(yes_edge, no_edge)
        best_price = yes_ask if yes_edge >= no_edge else no_ask
        idx = 0 if yes_edge >= no_edge else 1
        k = _ZERO
        reason = (
            f"No edge: YES {yes_edge:+.1%} NO {no_edge:+.1%} "
            f"(need >{MIN_EDGE:.1%}) | spread={spread:.1%}"
        )

    return MarketEdge(
        yes_ask=yes_ask, yes_bid=yes_bid,
        no_ask=no_ask, no_bid=no_bid,
        spread=spread, raw_mid=raw_mid,
        calibrated_prob=calibrated, true_prob=true_prob,
        yes_edge=yes_edge, no_edge=no_edge,
        best_side=best_side, best_price=best_price,
        best_edge=best_edge, best_token_idx=idx,
        kelly=k, reason=reason,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Convenience wrappers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def analyze_market_data(market: dict) -> Optional[MarketEdge]:
    """Analyze a market dict straight from the Gamma REST API.

    Extracts bestAsk, bestBid, oneHourPriceChange, volume24hr
    and runs full edge analysis.  Returns None for invalid data.
    """
    best_ask = market.get("bestAsk")
    best_bid = market.get("bestBid")
    if not best_ask or not best_bid:
        return None

    try:
        yes_ask = Decimal(str(best_ask))
        yes_bid = Decimal(str(best_bid))
    except Exception:
        return None

    if yes_ask <= _ZERO or yes_bid <= _ZERO or yes_ask > _ONE or yes_bid > _ONE:
        return None
    if yes_ask < yes_bid:
        yes_ask, yes_bid = yes_bid, yes_ask

    momentum = None
    raw_change = market.get("oneHourPriceChange")
    if raw_change is not None:
        try:
            momentum = Decimal(str(raw_change))
        except Exception:
            pass

    volume_24h = _ZERO
    try:
        volume_24h = Decimal(str(market.get("volume24hr", 0)))
    except Exception:
        pass

    return analyze_binary_market(yes_ask, yes_bid, momentum, volume_24h)


def validate_proposed_side(
    analysis: MarketEdge,
    proposed_side: str,
    min_edge_override: Optional[Decimal] = None,
) -> tuple[bool, str, Decimal, int]:
    """Check if a strategy's proposed side has edge.

    Returns: (tradeable, final_side, edge, token_idx)

    Args:
        min_edge_override: If provided, use this instead of the global MIN_EDGE.
                           Allows the quant engine to set dynamic thresholds.

    Logic:
      1. If proposed side has edge > min_edge     → go with it
      2. If proposed has no edge but OTHER does    → switch sides
      3. If neither has edge                      → PASS
    """
    threshold = min_edge_override if min_edge_override is not None else MIN_EDGE

    if proposed_side == "YES":
        prop_edge, other_edge = analysis.yes_edge, analysis.no_edge
        other_side = "NO"
    else:
        prop_edge, other_edge = analysis.no_edge, analysis.yes_edge
        other_side = "YES"

    # Require minimum edge on proposed side (not just > 0)
    if prop_edge > threshold:
        idx = 0 if proposed_side == "YES" else 1
        return True, proposed_side, prop_edge, idx

    # If proposed side doesn't have enough edge, check the other side
    if other_edge > threshold:
        idx = 0 if other_side == "YES" else 1
        return True, other_side, other_edge, idx

    return False, "PASS", max(prop_edge, other_edge), -1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Multi-outcome event analysis
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def analyze_event(event: dict) -> Optional[EventEdge]:
    """Analyze a multi-outcome event for internal consistency.

    For EXCLUSIVE events (e.g., "Who will win?"):
      - Sum of all YES probs should = 1.0
      - If sum > 1.0: overround; find the MOST overpriced → buy its NO
      - If sum < 1.0: underround/arb; buy all YES

    For NON-EXCLUSIVE events (e.g., "Who will perform?"):
      - Sum can be > 1.0 (multiple can be true)
      - Analyze each contract independently
      - But check: if all performers ≈100 %, NOs are nearly free lottery tickets

    We classify by whether sum ≈ 1.0 (exclusive) or >> 1.0 (non-exclusive).
    """
    markets = event.get("markets", [])
    if len(markets) < 2:
        return None

    event_id = event.get("id", "")
    event_title = str(event.get("title", ""))[:80]

    # Gather prices for each outcome
    outcomes: list[tuple[str, str, Decimal, Decimal, Decimal]] = []
    for m in markets:
        ask_raw = m.get("bestAsk")
        bid_raw = m.get("bestBid")
        if not ask_raw or not bid_raw:
            continue
        try:
            ask = Decimal(str(ask_raw))
            bid = Decimal(str(bid_raw))
        except Exception:
            continue
        if ask <= _ZERO or bid <= _ZERO:
            continue
        mid = (ask + bid) / 2
        mid = calibrate_probability(mid)
        market_id = m.get("conditionId", m.get("id", ""))
        question = str(m.get("question", ""))[:80]
        outcomes.append((market_id, question, ask, bid, mid))

    if len(outcomes) < 2:
        return None

    total = sum(mid for _, _, _, _, mid in outcomes)

    # Exclusive if sum is near 1.0 (within 30 % tolerance)
    is_exclusive = Decimal("0.7") <= total <= Decimal("1.4")
    overround = total - _ONE

    mispricings: list[EventMispricing] = []

    if is_exclusive and total > _ZERO:
        # Normalize to find fair prices
        for market_id, question, ask, bid, mid in outcomes:
            fair = mid / total  # what this outcome SHOULD cost
            gap = fair - ask    # positive = YES underpriced

            if abs(gap) > Decimal("0.03"):
                if gap > _ZERO:
                    # YES is cheap → buy YES
                    mispricings.append(EventMispricing(
                        market_id=market_id,
                        question=question,
                        yes_price=ask,
                        fair_price=fair,
                        edge=gap,
                        side="YES",
                        token_idx=0,
                    ))
                else:
                    # YES is expensive → buy NO
                    no_ask = _ONE - bid
                    no_fair = _ONE - fair
                    mispricings.append(EventMispricing(
                        market_id=market_id,
                        question=question,
                        yes_price=ask,
                        fair_price=fair,
                        edge=abs(gap),
                        side="NO",
                        token_idx=1,
                    ))
    else:
        # Non-exclusive: each contract is independent.
        # Look for extremes — very high YES = cheap NO lottery ticket
        for market_id, question, ask, bid, mid in outcomes:
            analysis = analyze_binary_market(ask, bid)
            if analysis.best_side != "PASS" and analysis.best_edge > Decimal("0.02"):
                mispricings.append(EventMispricing(
                    market_id=market_id,
                    question=question,
                    yes_price=ask,
                    fair_price=analysis.true_prob,
                    edge=analysis.best_edge,
                    side=analysis.best_side,
                    token_idx=analysis.best_token_idx,
                ))

    # Sort by edge descending
    mispricings.sort(key=lambda m: m.edge, reverse=True)

    return EventEdge(
        event_id=event_id,
        event_title=event_title,
        num_outcomes=len(outcomes),
        total_yes_prob=total,
        is_exclusive=is_exclusive,
        overround=overround,
        mispricings=mispricings,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Quick diagnostics (for display / logging)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def format_edge_summary(e: MarketEdge) -> str:
    """One-line summary for logging."""
    return (
        f"[{e.best_side}] edge={e.best_edge:+.1%} | "
        f"YES={e.yes_ask:.3f}(e={e.yes_edge:+.1%}) "
        f"NO={e.no_ask:.3f}(e={e.no_edge:+.1%}) | "
        f"prob≈{e.true_prob:.0%} spread={e.spread:.1%}"
    )
