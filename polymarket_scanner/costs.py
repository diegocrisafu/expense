"""Real-world transaction cost model.

Paper-trade P&L that ignores costs is fiction.  Every round trip on Polymarket
pays, in order of impact:

  1. Fees        — taker fee (default 2% = 200 bps) on each leg.
  2. Spread      — you buy at the ASK and sell at the BID; the mid you quote
                   yourself in backtests is optimistic by ~half the spread each
                   side.
  3. Slippage    — thin books move against a market order; modelled as a small
                   fraction that scales with how large the order is vs book depth.

This module is the single source of truth for those costs so that BOTH the
entry gate ("does my edge survive costs?") and the metrics harness ("what did I
*really* net?") agree.  All functions are pure.
"""

from __future__ import annotations

from decimal import Decimal

# ── Tunable cost parameters (bps unless noted) ──
TAKER_FEE_BPS = Decimal("200")        # 2% per leg — matches executor fee_rate_bps
DEFAULT_SLIPPAGE_BPS = Decimal("50")  # 0.5% assumed slippage per leg on entry/exit
_BPS = Decimal("10000")

# Minimum edge (after round-trip costs) required to justify a directional trade.
# Below this the expected profit is dominated by noise + costs → skip.
MIN_NET_EDGE = Decimal("0.02")        # 2%


def half_spread(bid: Decimal, ask: Decimal) -> Decimal:
    """Half the bid/ask spread as a price fraction of the mid.

    This is the cost of crossing the book once (buy at ask vs mid, or sell at
    bid vs mid).  A round trip pays it twice.
    """
    if bid <= 0 or ask <= 0 or ask <= bid:
        return Decimal("0")
    mid = (bid + ask) / 2
    return ((ask - bid) / 2) / mid


def round_trip_cost(
    price: Decimal,
    *,
    fee_bps: Decimal = TAKER_FEE_BPS,
    slippage_bps: Decimal = DEFAULT_SLIPPAGE_BPS,
    spread_frac: Decimal | None = None,
) -> Decimal:
    """Total round-trip cost as a fraction of notional (enter + exit).

    Args:
        price: entry price per share (0-1).
        fee_bps: taker fee per leg.
        slippage_bps: assumed slippage per leg.
        spread_frac: half-spread fraction (from :func:`half_spread`) if a live
            book is available; otherwise slippage stands in for it.

    Returns a Decimal fraction, e.g. 0.05 == 5% of notional lost to friction.
    """
    if price <= 0 or price >= 1:
        return Decimal("1")  # degenerate — treat as all-cost so it's rejected
    per_leg = (fee_bps + slippage_bps) / _BPS
    cost = per_leg * 2  # enter + exit
    if spread_frac is not None:
        cost += spread_frac * 2
    return cost


def net_edge(
    gross_edge: Decimal,
    price: Decimal,
    *,
    fee_bps: Decimal = TAKER_FEE_BPS,
    slippage_bps: Decimal = DEFAULT_SLIPPAGE_BPS,
    spread_frac: Decimal | None = None,
) -> Decimal:
    """Edge remaining after round-trip costs.  Can be negative."""
    return gross_edge - round_trip_cost(
        price, fee_bps=fee_bps, slippage_bps=slippage_bps, spread_frac=spread_frac
    )


def covers_costs(
    gross_edge: Decimal,
    price: Decimal,
    *,
    min_net_edge: Decimal = MIN_NET_EDGE,
    **kwargs,
) -> bool:
    """True if the trade's edge clears costs by the minimum required margin."""
    return net_edge(gross_edge, price, **kwargs) >= min_net_edge


def net_exit_value(shares: Decimal, bid: Decimal, *, fee_bps: Decimal = TAKER_FEE_BPS) -> Decimal:
    """What you ACTUALLY receive selling `shares` at the `bid`, after fee.

    Backtests that mark positions at the mid overstate proceeds; real exits fill
    at the bid and pay a fee.  Use this for honest realised-P&L accounting.
    """
    gross = shares * bid
    fee = gross * (fee_bps / _BPS)
    return gross - fee
