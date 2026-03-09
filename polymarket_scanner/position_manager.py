"""Active Position Management — Take Profit, Stop Loss, Capital Recycling.

This module transforms the bot from "buy and hold until resolution" to
an active trader that:
1. Monitors live prices of all open positions
2. Sells when Take Profit target is hit (default: +20%)
3. Sells when Stop Loss is hit (default: -10%)
4. Implements trailing stops for winners
5. Recycles capital so freed cash can be re-deployed

Every position gets an automatic exit plan the moment it is opened.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

import httpx

from .config import GAMMA_API_BASE
from .database import get_connection, DB_PATH
from .trading_config import (
    TAKE_PROFIT_PCT,
    STOP_LOSS_PCT,
    TRAILING_STOP_PCT,
    MAX_HOLD_HOURS,
    MIN_EXIT_SHARES,
    CLOB_HOST,
)

logger = logging.getLogger(__name__)


@dataclass
class ManagedPosition:
    """A position actively managed by the exit engine."""
    position_id: int
    trade_id: int
    market_id: str
    token_id: str
    side: str               # BUY / BUY_BOTH
    entry_price: Decimal
    size: Decimal            # in shares
    cost_basis: Decimal      # total USDC spent
    current_price: Decimal   # last observed price
    high_water_mark: Decimal # highest price seen (for trailing stop)
    opened_at: datetime
    market_question: str

    # Computed targets (set once on construction)
    take_profit_price: Decimal = Decimal("0")
    stop_loss_price: Decimal = Decimal("0")
    trailing_stop_price: Decimal = Decimal("0")

    def __post_init__(self):
        if self.take_profit_price == 0:
            self.take_profit_price = self.entry_price * (1 + TAKE_PROFIT_PCT)
        if self.stop_loss_price == 0:
            self.stop_loss_price = self.entry_price * (1 - STOP_LOSS_PCT)
        if self.trailing_stop_price == 0:
            self.trailing_stop_price = self.high_water_mark * (1 - TRAILING_STOP_PCT)

    @property
    def unrealized_pnl(self) -> Decimal:
        """Current unrealized P&L in USDC."""
        return (self.current_price - self.entry_price) * self.size

    @property
    def unrealized_pnl_pct(self) -> Decimal:
        """Current unrealized P&L as a percentage."""
        if self.entry_price == 0:
            return Decimal("0")
        return (self.current_price - self.entry_price) / self.entry_price

    @property
    def hold_duration(self) -> timedelta:
        return datetime.utcnow() - self.opened_at

    @property
    def hold_hours(self) -> float:
        return self.hold_duration.total_seconds() / 3600


@dataclass
class ExitSignal:
    """Reason to close a position."""
    position_id: int
    reason: str          # TAKE_PROFIT | STOP_LOSS | TRAILING_STOP | TIME_EXIT | MANUAL
    trigger_price: Decimal
    expected_pnl: Decimal
    urgency: int = 1     # 1=normal, 2=high, 3=critical


class PositionManager:
    """Actively manages open positions with exit strategies.

    Lifecycle:
        1. Bot opens a position → calls register_position()
        2. Every cycle → calls check_exits() which fetches live prices
        3. If any exit condition is met → returns ExitSignal list
        4. Bot calls execute_exit() → sells on CLOB and recycles capital
    """

    # Max consecutive sell failures before force-closing a position
    MAX_SELL_FAILURES = 5
    # If bid stays below this fraction of entry for this many hours, consider dead
    DEAD_MARKET_BID_RATIO = Decimal("0.01")  # bid < 1% of entry
    DEAD_MARKET_HOURS = 24  # must be dead for 24h

    def __init__(self, executor=None, db_path: str = None):
        self.db_path = db_path or DB_PATH
        self.executor = executor  # TradingExecutor reference for selling
        self._sell_fail_counts: dict[int, int] = {}  # position_id → consecutive failures
        self._ensure_tables()

    # ------------------------------------------------------------------
    # Database setup
    # ------------------------------------------------------------------
    def _ensure_tables(self):
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS managed_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id INTEGER,
                    market_id TEXT,
                    token_id TEXT,
                    side TEXT,
                    entry_price DECIMAL(18, 8),
                    size DECIMAL(18, 6),
                    cost_basis DECIMAL(18, 6),
                    current_price DECIMAL(18, 8),
                    high_water_mark DECIMAL(18, 8),
                    take_profit_price DECIMAL(18, 8),
                    stop_loss_price DECIMAL(18, 8),
                    trailing_stop_price DECIMAL(18, 8),
                    market_question TEXT,
                    status TEXT DEFAULT 'ACTIVE',
                    exit_reason TEXT,
                    exit_price DECIMAL(18, 8),
                    exit_pnl DECIMAL(18, 6),
                    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP
                )
            """)
            conn.commit()

    # ------------------------------------------------------------------
    # Position registration
    # ------------------------------------------------------------------
    def register_position(
        self,
        trade_id: int,
        market_id: str,
        token_id: str,
        side: str,
        entry_price: Decimal,
        size: Decimal,
        market_question: str = "",
        take_profit_pct: Optional[Decimal] = None,
        stop_loss_pct: Optional[Decimal] = None,
        trailing_stop_pct: Optional[Decimal] = None,
    ) -> int:
        """Register a newly-opened position for active management.

        Args:
            take_profit_pct: Override default TP %. Pass from risk_manager profile.
            stop_loss_pct: Override default SL %.
            trailing_stop_pct: Override default trailing stop %.

        Returns:
            managed position ID
        """
        _tp = take_profit_pct if take_profit_pct is not None else TAKE_PROFIT_PCT
        _sl = stop_loss_pct if stop_loss_pct is not None else STOP_LOSS_PCT
        _ts = trailing_stop_pct if trailing_stop_pct is not None else TRAILING_STOP_PCT

        cost_basis = entry_price * size
        tp_price = entry_price * (1 + _tp)
        sl_price = entry_price * (1 - _sl)
        ts_price = entry_price * (1 - _ts)

        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO managed_positions
                (trade_id, market_id, token_id, side, entry_price, size,
                 cost_basis, current_price, high_water_mark,
                 take_profit_price, stop_loss_price, trailing_stop_price,
                 market_question)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id, market_id, token_id, side,
                str(entry_price), str(size), str(cost_basis),
                str(entry_price), str(entry_price),
                str(tp_price), str(sl_price), str(ts_price),
                market_question,
            ))
            conn.commit()
            pos_id = cursor.lastrowid

        print(f"   📋 Position managed: TP=${tp_price:.3f} SL=${sl_price:.3f}")
        logger.info(
            f"Registered position #{pos_id}: {side} {size} shares @ ${entry_price:.4f} "
            f"| TP=${tp_price:.4f} SL=${sl_price:.4f}"
        )
        return pos_id

    # ------------------------------------------------------------------
    # Load active positions from DB
    # ------------------------------------------------------------------
    def _load_active_positions(self) -> list[ManagedPosition]:
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, trade_id, market_id, token_id, side,
                       entry_price, size, cost_basis, current_price,
                       high_water_mark, take_profit_price, stop_loss_price,
                       trailing_stop_price, market_question, opened_at
                FROM managed_positions
                WHERE status = 'ACTIVE'
            """)
            positions = []
            for row in cursor.fetchall():
                positions.append(ManagedPosition(
                    position_id=row[0],
                    trade_id=row[1],
                    market_id=row[2],
                    token_id=row[3],
                    side=row[4],
                    entry_price=Decimal(str(row[5])),
                    size=Decimal(str(row[6])),
                    cost_basis=Decimal(str(row[7])),
                    current_price=Decimal(str(row[8])),
                    high_water_mark=Decimal(str(row[9])),
                    take_profit_price=Decimal(str(row[10])),
                    stop_loss_price=Decimal(str(row[11])),
                    trailing_stop_price=Decimal(str(row[12])),
                    market_question=row[13] or "",
                    opened_at=datetime.fromisoformat(row[14]) if row[14] else datetime.utcnow(),
                ))
            return positions

    # ------------------------------------------------------------------
    # Live price fetching
    # ------------------------------------------------------------------
    async def _fetch_live_price(self, token_id: str) -> Optional[tuple[Decimal, Decimal]]:
        """Fetch latest bid and mid-market price for a token from the CLOB.
        
        Returns:
            (mid_price, bid_price) or None if unavailable.
            mid_price is used for exit checks, bid_price for actual sell orders.
        """
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{CLOB_HOST}/book",
                    params={"token_id": token_id},
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    return None

                book = resp.json()
                bids = book.get("bids", [])
                asks = book.get("asks", [])

                best_bid = Decimal(str(bids[0]["price"])) if bids else None
                best_ask = Decimal(str(asks[0]["price"])) if asks else None

                if best_bid and best_ask:
                    mid = (best_bid + best_ask) / 2
                    return (mid, best_bid)
                elif best_bid:
                    return (best_bid, best_bid)
                elif best_ask:
                    return (best_ask, best_ask)
                return None
        except Exception as e:
            logger.debug(f"Price fetch failed for {token_id[:20]}: {e}")
            return None

    # ------------------------------------------------------------------
    # Core exit-check logic
    # ------------------------------------------------------------------
    async def check_exits(self) -> list[ExitSignal]:
        """Check all active positions for exit conditions.

        Returns a list of ExitSignal objects for positions that should be closed.
        """
        positions = self._load_active_positions()
        if not positions:
            return []

        exit_signals: list[ExitSignal] = []

        for pos in positions:
            # Skip arb positions — they resolve on their own
            if pos.side == "BUY_BOTH":
                continue

            # Fetch live price (mid for checks, bid for selling)
            price_data = await self._fetch_live_price(pos.token_id)

            # --- DEAD MARKET DETECTION ---
            # If we can't fetch price OR bid is near-zero for a long-held position,
            # the market is likely resolved/delisted.  Force-close to free the slot.
            if price_data is None:
                if pos.hold_hours > self.DEAD_MARKET_HOURS * 2:
                    logger.info(f"Force-closing dead position (no price data, held {pos.hold_hours:.0f}h): {pos.market_question[:40]}")
                    self._close_position(pos.position_id, "MARKET_DEAD", Decimal("0"), -pos.cost_basis)
                continue

            live_price, bid_price = price_data

            # Detect resolved/dead markets: bid near zero for extended time
            if (bid_price < pos.entry_price * self.DEAD_MARKET_BID_RATIO
                    and pos.hold_hours > self.DEAD_MARKET_HOURS):
                pnl = (bid_price - pos.entry_price) * pos.size
                logger.info(
                    f"Dead market detected (bid ${bid_price:.4f}, held {pos.hold_hours:.0f}h): "
                    f"{pos.market_question[:40]}"
                )
                self._close_position(pos.position_id, "MARKET_DEAD", bid_price, pnl)
                continue

            # Update current price & high water mark
            pos.current_price = live_price
            if live_price > pos.high_water_mark:
                pos.high_water_mark = live_price
                # Update trailing stop when new high
                pos.trailing_stop_price = live_price * (1 - TRAILING_STOP_PCT)

            # Persist updated prices
            self._update_position_prices(pos)

            # --- Safety: skip exit if bid is unreliable ---
            # If spread is too wide, the bid is not a real price — skip this cycle.
            spread = live_price - bid_price if live_price > bid_price else Decimal("0")
            if spread > Decimal("0.08"):
                logger.debug(f"Skipping exit check for {pos.market_question[:30]}: spread ${spread:.3f} too wide")
                continue

            # For low-bid markets that aren't yet "dead", allow time-exit
            # but don't block all exit logic (removed the old 40% gate that
            # trapped positions forever).
            bid_too_low = bid_price < pos.entry_price * Decimal("0.40")

            # --- Check exit conditions ---

            # 1) TAKE PROFIT — only if BID (actual sell price) is above entry
            if not bid_too_low and bid_price >= pos.take_profit_price and bid_price > pos.entry_price:
                pnl = (bid_price - pos.entry_price) * pos.size
                exit_signals.append(ExitSignal(
                    position_id=pos.position_id,
                    reason="TAKE_PROFIT",
                    trigger_price=bid_price,
                    expected_pnl=pnl,
                    urgency=2,
                ))
                print(f"   TAKE PROFIT hit: {pos.market_question[:40]}... +${pnl:.2f}")
                continue

            # 2) STOP LOSS — trigger even on low bids (cut losses)
            if bid_price <= pos.stop_loss_price:
                pnl = (bid_price - pos.entry_price) * pos.size
                exit_signals.append(ExitSignal(
                    position_id=pos.position_id,
                    reason="STOP_LOSS",
                    trigger_price=bid_price,
                    expected_pnl=pnl,
                    urgency=3,
                ))
                print(f"   STOP LOSS hit: {pos.market_question[:40]}... ${pnl:.2f}")
                continue

            # 3) TRAILING STOP (only if we're in profit)
            if not bid_too_low and live_price > pos.entry_price and live_price <= pos.trailing_stop_price:
                pnl = (bid_price - pos.entry_price) * pos.size
                exit_signals.append(ExitSignal(
                    position_id=pos.position_id,
                    reason="TRAILING_STOP",
                    trigger_price=bid_price,
                    expected_pnl=pnl,
                    urgency=2,
                ))
                print(f"   TRAILING STOP: {pos.market_question[:40]}... +${pnl:.2f}")
                continue

            # 4) TIME-BASED EXIT — always allowed, even with low bid
            if pos.hold_hours > MAX_HOLD_HOURS:
                pnl = (bid_price - pos.entry_price) * pos.size
                exit_signals.append(ExitSignal(
                    position_id=pos.position_id,
                    reason="TIME_EXIT",
                    trigger_price=bid_price,
                    expected_pnl=pnl,
                    urgency=1,
                ))
                print(
                    f"   TIME EXIT ({pos.hold_hours:.0f}h): "
                    f"{pos.market_question[:40]}... ${pnl:.2f}"
                )
                continue

        # Sort by urgency (critical first)
        exit_signals.sort(key=lambda s: s.urgency, reverse=True)
        return exit_signals

    # ------------------------------------------------------------------
    # Execute exits (sell positions)
    # ------------------------------------------------------------------
    async def execute_exit(self, signal: ExitSignal) -> Optional[Decimal]:
        """Execute a sell order to close a position.

        Returns the USDC recovered (capital recycled), or None on failure.
        """
        # Load position details
        positions = self._load_active_positions()
        pos = next((p for p in positions if p.position_id == signal.position_id), None)
        if not pos:
            logger.warning(f"Position {signal.position_id} not found for exit")
            return None

        sell_price = signal.trigger_price
        shares_to_sell = pos.size

        # SAFETY: refuse to "take profit" at a loss
        if signal.reason == "TAKE_PROFIT" and sell_price < pos.entry_price:
            logger.warning(
                f"Refusing fake TP: sell ${sell_price:.3f} < entry ${pos.entry_price:.3f} "
                f"for {pos.market_question[:30]}"
            )
            return None

        print(
            f"   💰 SELLING {shares_to_sell:.1f} shares @ ${sell_price:.4f} "
            f"({signal.reason}) → PnL ${signal.expected_pnl:.2f}"
        )

        success = False
        if self.executor and not self.executor.paper_trading:
            success = await self._live_sell(pos, sell_price)
        else:
            # Paper trade — always succeeds
            success = True
            logger.info(
                f"[PAPER] SELL {shares_to_sell} shares of {pos.token_id[:20]}... "
                f"@ ${sell_price:.4f}"
            )

        if success:
            self._sell_fail_counts.pop(pos.position_id, None)
            recovered = sell_price * shares_to_sell
            self._close_position(pos.position_id, signal.reason, sell_price, signal.expected_pnl)
            return recovered

        # --- Sell failed: track consecutive failures ---
        fail_count = self._sell_fail_counts.get(pos.position_id, 0) + 1
        self._sell_fail_counts[pos.position_id] = fail_count
        logger.warning(
            f"Sell failed for position {pos.position_id} "
            f"({fail_count}/{self.MAX_SELL_FAILURES}): {pos.market_question[:40]}"
        )

        if fail_count >= self.MAX_SELL_FAILURES:
            logger.warning(
                f"Force-closing position {pos.position_id} after {fail_count} sell failures "
                f"(market likely resolved or shares not held)"
            )
            self._sell_fail_counts.pop(pos.position_id, None)
            self._close_position(
                pos.position_id, "SELL_FAILED",
                sell_price, signal.expected_pnl,
            )
            # Return cost_basis as recovered since the position is cleared
            # (actual recovery depends on whether shares resolved profitably)
            return pos.cost_basis

        return None

    async def _live_sell(self, pos: ManagedPosition, price: Decimal) -> bool:
        """Place a real sell order on Polymarket CLOB."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            shares = float(pos.size)
            if shares < float(MIN_EXIT_SHARES):
                shares = float(MIN_EXIT_SHARES)

            order_args = OrderArgs(
                token_id=pos.token_id,
                price=float(price),
                size=shares,
                side="SELL",
            )

            logger.info(f"[LIVE] Selling {shares:.2f} shares @ ${price:.4f}")
            signed = self.executor.client.create_order(order_args)
            result = self.executor.client.post_order(signed, OrderType.GTC)
            
            # Check if the sell actually went through
            status = result.get('status', '') if isinstance(result, dict) else ''
            if status in ('matched', 'live', 'delayed'):
                logger.info(f"[LIVE] Sell order accepted (status={status}): {result}")
                return True
            else:
                logger.warning(f"[LIVE] Sell order failed (status={status}): {result}")
                return False
        except Exception as e:
            logger.error(f"Failed to sell: {e}")
            return False

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    def _update_position_prices(self, pos: ManagedPosition):
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE managed_positions
                SET current_price = ?,
                    high_water_mark = ?,
                    trailing_stop_price = ?
                WHERE id = ?
            """, (
                str(pos.current_price),
                str(pos.high_water_mark),
                str(pos.trailing_stop_price),
                pos.position_id,
            ))
            conn.commit()

    def _close_position(self, position_id: int, reason: str, price: Decimal, pnl: Decimal):
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE managed_positions
                SET status = 'CLOSED',
                    exit_reason = ?,
                    exit_price = ?,
                    exit_pnl = ?,
                    closed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (reason, str(price), str(pnl), position_id))
            conn.commit()

    # ------------------------------------------------------------------
    # Summary / reporting
    # ------------------------------------------------------------------
    def get_portfolio_summary(self) -> dict:
        """Get a full picture of managed positions."""
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()

            # Active positions
            cursor.execute("""
                SELECT COUNT(*), 
                       COALESCE(SUM(CAST(cost_basis AS FLOAT)), 0),
                       COALESCE(SUM((CAST(current_price AS FLOAT) - CAST(entry_price AS FLOAT)) 
                                    * CAST(size AS FLOAT)), 0)
                FROM managed_positions WHERE status = 'ACTIVE'
            """)
            active_row = cursor.fetchone()

            # Closed positions
            cursor.execute("""
                SELECT COUNT(*),
                       COALESCE(SUM(CAST(exit_pnl AS FLOAT)), 0),
                       exit_reason, COUNT(*)
                FROM managed_positions WHERE status = 'CLOSED'
                GROUP BY exit_reason
            """)
            closed_rows = cursor.fetchall()

            # Overall closed stats
            cursor.execute("""
                SELECT COUNT(*),
                       COALESCE(SUM(CAST(exit_pnl AS FLOAT)), 0),
                       COALESCE(SUM(CASE WHEN CAST(exit_pnl AS FLOAT) > 0 THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN CAST(exit_pnl AS FLOAT) <= 0 THEN 1 ELSE 0 END), 0)
                FROM managed_positions WHERE status = 'CLOSED'
            """)
            closed_summary = cursor.fetchone()

            return {
                "active_count": active_row[0],
                "capital_deployed": Decimal(str(active_row[1])),
                "unrealized_pnl": Decimal(str(active_row[2])),
                "closed_count": closed_summary[0] if closed_summary else 0,
                "realized_pnl": Decimal(str(closed_summary[1])) if closed_summary else Decimal("0"),
                "wins": closed_summary[2] if closed_summary else 0,
                "losses": closed_summary[3] if closed_summary else 0,
                "exit_reasons": {row[2]: row[3] for row in closed_rows if row[2]},
            }

    def print_position_report(self):
        """Pretty-print the current portfolio state."""
        summary = self.get_portfolio_summary()
        positions = self._load_active_positions()

        print("\n" + "=" * 60)
        print("📊 POSITION MANAGER REPORT")
        print("=" * 60)

        if positions:
            print(f"\n🟢 ACTIVE POSITIONS ({summary['active_count']}):")
            print(f"   Capital deployed: ${summary['capital_deployed']:.2f}")
            print(f"   Unrealized P&L:   ${summary['unrealized_pnl']:.2f}")
            print()
            for p in positions:
                pnl_pct = p.unrealized_pnl_pct * 100
                emoji = "📈" if pnl_pct > 0 else "📉"
                print(
                    f"   {emoji} {p.market_question[:45]:<45} "
                    f"Entry=${p.entry_price:.3f} Now=${p.current_price:.3f} "
                    f"PnL={pnl_pct:+.1f}% ({p.hold_hours:.0f}h)"
                )
        else:
            print("\n   No active positions")

        if summary["closed_count"] > 0:
            win_rate = summary["wins"] / max(summary["wins"] + summary["losses"], 1) * 100
            print(f"\n🔴 CLOSED: {summary['closed_count']} trades")
            print(f"   Realized P&L: ${summary['realized_pnl']:.2f}")
            print(f"   Win rate:      {win_rate:.0f}%")
            print(f"   Exit reasons:  {dict(summary['exit_reasons'])}")

        print("=" * 60)
