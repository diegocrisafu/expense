"""Backtest engine — replay price paths through the risk + exit rules.

WHY THIS EXISTS
    You cannot claim a strategy is profitable without testing it out-of-sample.
    Live paper-trading tells you eventually, but slowly and unrepeatably.  A
    backtest lets you replay a known price path deterministically and measure
    win rate / profit factor before risking a cent.

HONEST LIMITATION (read this)
    The project's `snapshots` / `orderbook_levels` tables are EMPTY — no market
    data was ever captured, so there is nothing real to replay yet.  This module
    is the *engine*; it is unit-tested on synthetic paths and is ready the moment
    a data-capture pipeline populates snapshots.  Until then, a backtest here
    proves the exit/risk LOGIC behaves, not that any strategy has edge.

MODEL
    A "trade" is: enter long at `entry_price`, then walk a series of observed
    prices.  At each step we apply the same rules the live bot uses:
      • take-profit / stop-loss / trailing-stop from the strategy profile
      • the real 5-share order minimum and the 5% position cap (via risk_manager)
      • fees + slippage on entry and exit (via costs.py)
    Exit value is marked at the *bid we actually cross*, net of fees — no
    placeholder mids.  Output feeds straight into metrics.compute_metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from .costs import net_exit_value, round_trip_cost
from .metrics import ClosedTrade, Metrics, compute_metrics
from .risk_manager import STRATEGY_PROFILES, StrategyBudget, order_cost


@dataclass
class PriceStep:
    """One observed market tick for a token we hold."""
    bid: Decimal          # what we could sell into right now
    hours_elapsed: float  # hold time at this tick


@dataclass
class BacktestTrade:
    """A single simulated trade: an entry + the price path that followed."""
    strategy: str
    entry_price: Decimal
    balance_at_entry: Decimal
    path: Sequence[PriceStep]


@dataclass
class SimResult:
    entered: bool
    reason: str
    entry_price: Decimal
    exit_price: Decimal
    shares: Decimal
    cost_basis: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal          # after fees on the exit
    hold_hours: float


def _profile(strategy: str) -> StrategyBudget:
    return STRATEGY_PROFILES.get(
        strategy.upper(),
        StrategyBudget(name=strategy, allocation_pct=Decimal("0.05"),
                       max_per_trade_pct=Decimal("0.05"), max_open_positions=1),
    )


def simulate_trade(trade: BacktestTrade, base_bet: Decimal = Decimal("1.00")) -> SimResult:
    """Run one trade through entry sizing + the exit rules on its price path."""
    prof = _profile(trade.strategy)
    entry = trade.entry_price

    # Size the position exactly as the live bot would: 5-share floor + 5% cap.
    shares, cost = order_cost(base_bet, entry)
    cap = trade.balance_at_entry * Decimal("0.05")
    if cost > cap:
        return SimResult(False, "REJECTED_5PCT_CAP", entry, entry, Decimal("0"),
                         Decimal("0"), Decimal("0"), Decimal("0"), 0.0)

    tp_price = entry * (Decimal("1") + prof.take_profit_pct)
    sl_price = entry * (Decimal("1") - prof.stop_loss_pct)
    high = entry
    trail_price = entry * (Decimal("1") - prof.trailing_stop_pct)

    exit_price = entry
    reason = "PATH_END"
    hold = 0.0
    for step in trade.path:
        bid = step.bid
        hold = step.hours_elapsed
        if bid > high:
            high = bid
            trail_price = high * (Decimal("1") - prof.trailing_stop_pct)
        if bid >= tp_price:
            exit_price, reason = bid, "TAKE_PROFIT"
            break
        if bid <= sl_price:
            exit_price, reason = bid, "STOP_LOSS"
            break
        if bid > entry and bid <= trail_price:
            exit_price, reason = bid, "TRAILING_STOP"
            break
        if hold >= prof.max_hold_hours:
            exit_price, reason = bid, "MAX_HOLD"
            break
        exit_price = bid  # mark-to-market at last seen bid

    proceeds = net_exit_value(shares, exit_price)   # fees applied on the way out
    gross_pnl = (exit_price - entry) * shares
    net_pnl = proceeds - cost                        # true realised P&L
    return SimResult(True, reason, entry, exit_price, shares, cost,
                     gross_pnl, net_pnl, hold)


def trade_from_capture(
    token_id: str,
    strategy: str,
    entry_price: Decimal,
    balance_at_entry: Decimal,
    db_path: str = None,
) -> BacktestTrade | None:
    """Build a BacktestTrade from a captured real price series (market_data).

    Returns None if we have fewer than 2 observations for the token — you can't
    replay a path you never recorded.  This is the bridge from live-captured
    data to the offline backtester.
    """
    from datetime import datetime
    from .market_data import load_series
    from .database import DB_PATH

    series = load_series(token_id, db_path or DB_PATH)
    if len(series) < 2:
        return None

    def _parse(ts):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(str(ts), fmt)
            except (ValueError, TypeError):
                continue
        return None

    t0 = _parse(series[0]["ts"])
    steps: list[PriceStep] = []
    for row in series:
        bid = row["bid"] if row["bid"] is not None else row["mid"]
        if bid is None:
            continue
        t = _parse(row["ts"])
        hours = ((t - t0).total_seconds() / 3600.0) if (t and t0) else float(len(steps))
        steps.append(PriceStep(bid=Decimal(str(bid)), hours_elapsed=hours))

    if len(steps) < 2:
        return None
    return BacktestTrade(strategy, entry_price, balance_at_entry, steps)


def run_backtest(trades: Sequence[BacktestTrade]) -> tuple[Metrics, list[SimResult]]:
    """Simulate a batch of trades and score them with the metrics harness."""
    results = [simulate_trade(t) for t in trades]
    closed = [
        ClosedTrade(strategy=t.strategy, entry_price=r.entry_price,
                    size=r.shares, pnl=r.net_pnl)
        for t, r in zip(trades, results) if r.entered
    ]
    metrics = compute_metrics(closed, label="BACKTEST")
    return metrics, results
