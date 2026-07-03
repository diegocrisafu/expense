"""Performance measurement harness.

You cannot optimise for "higher win rate" or "higher profit factor" if you never
compute them.  This module turns the closed-trade record into the standard
quant scorecard, so every future parameter change can be judged against a
number instead of a vibe.

Reported per strategy and overall:
  • trades, win rate
  • gross profit / gross loss, PROFIT FACTOR (Σwin / Σloss)
  • net P&L and ROI vs starting capital
  • expectancy per trade, payoff ratio (avg win / avg loss)
  • max drawdown of the realised equity curve
  • a COST-ADJUSTED net that applies the real-world fee+slippage model, so the
    headline number is not paper-trade fantasy.

The core (`compute_metrics`) is pure — feed it a list of closed trades — so it
is unit-tested directly without a database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

from .costs import round_trip_cost
from .database import get_connection, DB_PATH
from .trading_config import STARTING_BALANCE

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")


@dataclass
class ClosedTrade:
    """One resolved/closed trade — the minimum needed to score performance."""
    strategy: str
    entry_price: Decimal
    size: Decimal            # shares
    pnl: Decimal             # recorded realised P&L (dollars)

    @property
    def notional(self) -> Decimal:
        return self.entry_price * self.size

    @property
    def cost_adjusted_pnl(self) -> Decimal:
        """Recorded P&L minus the real-world round-trip friction on notional."""
        friction = round_trip_cost(self.entry_price) * self.notional
        return self.pnl - friction


@dataclass
class Metrics:
    """The scorecard for a set of trades."""
    label: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_profit: Decimal = _ZERO
    gross_loss: Decimal = _ZERO            # positive magnitude
    net_pnl: Decimal = _ZERO
    net_pnl_after_costs: Decimal = _ZERO
    max_drawdown: Decimal = _ZERO
    starting_capital: Decimal = STARTING_BALANCE

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades) if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        if self.gross_loss > 0:
            return float(self.gross_profit / self.gross_loss)
        return float("inf") if self.gross_profit > 0 else 0.0

    @property
    def expectancy(self) -> Decimal:
        return (self.net_pnl / self.trades) if self.trades else _ZERO

    @property
    def avg_win(self) -> Decimal:
        return (self.gross_profit / self.wins) if self.wins else _ZERO

    @property
    def avg_loss(self) -> Decimal:
        return (self.gross_loss / self.losses) if self.losses else _ZERO

    @property
    def payoff_ratio(self) -> float:
        return float(self.avg_win / self.avg_loss) if self.avg_loss > 0 else 0.0

    @property
    def roi(self) -> float:
        return float(self.net_pnl / self.starting_capital) if self.starting_capital else 0.0


def compute_metrics(
    trades: Iterable[ClosedTrade],
    label: str = "ALL",
    starting_capital: Decimal = STARTING_BALANCE,
) -> Metrics:
    """Pure scorer.  Trades are processed IN ORDER for the drawdown curve."""
    m = Metrics(label=label, starting_capital=starting_capital)
    equity = _ZERO
    peak = _ZERO
    for t in trades:
        m.trades += 1
        m.net_pnl += t.pnl
        m.net_pnl_after_costs += t.cost_adjusted_pnl
        if t.pnl > 0:
            m.wins += 1
            m.gross_profit += t.pnl
        elif t.pnl < 0:
            m.losses += 1
            m.gross_loss += -t.pnl
        # realised equity curve → max drawdown
        equity += t.pnl
        peak = max(peak, equity)
        drawdown = peak - equity
        m.max_drawdown = max(m.max_drawdown, drawdown)
    return m


def load_closed_trades(db_path: str = DB_PATH) -> list[ClosedTrade]:
    """Read resolved trades (with a recorded P&L) ordered by resolution time."""
    rows: list[ClosedTrade] = []
    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT strategy, entry_price, size, pnl
                FROM trade_history
                WHERE pnl IS NOT NULL
                ORDER BY COALESCE(resolved_at, timestamp) ASC
            """)
            for r in cursor.fetchall():
                strategy, entry_price, size, pnl = r
                rows.append(ClosedTrade(
                    strategy=(strategy or "UNKNOWN").upper(),
                    entry_price=Decimal(str(entry_price or 0)),
                    size=Decimal(str(size or 0)),
                    pnl=Decimal(str(pnl or 0)),
                ))
    except Exception as e:
        logger.warning(f"load_closed_trades failed: {e}")
    return rows


def metrics_by_strategy(trades: list[ClosedTrade]) -> dict[str, Metrics]:
    groups: dict[str, list[ClosedTrade]] = {}
    for t in trades:
        groups.setdefault(t.strategy, []).append(t)
    return {name: compute_metrics(ts, label=name) for name, ts in groups.items()}


def format_report(db_path: str = DB_PATH) -> str:
    """Human-readable scorecard for the CLI / final report / dashboard."""
    trades = load_closed_trades(db_path)
    overall = compute_metrics(trades, label="ALL")
    per = metrics_by_strategy(trades)

    def pf(m: Metrics) -> str:
        v = m.profit_factor
        return "∞" if v == float("inf") else f"{v:.2f}"

    lines: list[str] = []
    lines.append("═" * 72)
    lines.append("📊 PERFORMANCE SCORECARD (realised, cost-adjusted)")
    lines.append("═" * 72)
    if overall.trades == 0:
        lines.append("  No closed trades yet — scorecard populates as positions resolve.")
        lines.append("═" * 72)
        return "\n".join(lines)

    header = f"  {'Strategy':<12}{'N':>4}{'Win%':>7}{'PF':>7}{'Net$':>9}{'NetCost$':>10}{'Expect':>9}{'MaxDD':>8}"
    lines.append(header)
    lines.append("  " + "─" * (len(header) - 2))
    for name, m in sorted(per.items(), key=lambda kv: kv[1].net_pnl, reverse=True):
        lines.append(
            f"  {name:<12}{m.trades:>4}{m.win_rate*100:>6.0f}%{pf(m):>7}"
            f"{float(m.net_pnl):>9.2f}{float(m.net_pnl_after_costs):>10.2f}"
            f"{float(m.expectancy):>9.3f}{float(m.max_drawdown):>8.2f}"
        )
    lines.append("  " + "─" * (len(header) - 2))
    lines.append(
        f"  {'ALL':<12}{overall.trades:>4}{overall.win_rate*100:>6.0f}%{pf(overall):>7}"
        f"{float(overall.net_pnl):>9.2f}{float(overall.net_pnl_after_costs):>10.2f}"
        f"{float(overall.expectancy):>9.3f}{float(overall.max_drawdown):>8.2f}"
    )
    lines.append("")
    lines.append(
        f"  ROI: {overall.roi*100:+.1f}%  |  Payoff (avgWin/avgLoss): {overall.payoff_ratio:.2f}"
        f"  |  Wins {overall.wins}/{overall.losses} losses"
    )
    lines.append("  NOTE: PF<1 or negative NetCost$ ⇒ strategy is unprofitable after real costs.")
    lines.append("═" * 72)
    return "\n".join(lines)


def print_report(db_path: str = DB_PATH) -> None:
    print(format_report(db_path))
