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
    SMART_EXIT_ENABLED,
)
from .smart_exit import (
    MarketSnapshot,
    PositionContext,
    SmartExitVerdict,
    evaluate_position,
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

    async def _fetch_full_book(self, token_id: str) -> Optional[dict]:
        """Fetch the full orderbook for smart exit analysis.

        Returns dict with: bid, ask, mid, spread, book_depth_bid, book_depth_ask,
        or None if unavailable.
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

                if not best_bid or not best_ask:
                    return None

                mid = (best_bid + best_ask) / 2
                spread = best_ask - best_bid

                # Sum total depth on each side (USD value)
                depth_bid = sum(
                    Decimal(str(b.get("size", 0))) * Decimal(str(b.get("price", 0)))
                    for b in bids
                )
                depth_ask = sum(
                    Decimal(str(a.get("size", 0))) * Decimal(str(a.get("price", 0)))
                    for a in asks
                )

                return {
                    "bid": best_bid,
                    "ask": best_ask,
                    "mid": mid,
                    "spread": spread,
                    "book_depth_bid": depth_bid,
                    "book_depth_ask": depth_ask,
                }
        except Exception as e:
            logger.debug(f"Full book fetch failed for {token_id[:20]}: {e}")
            return None

    async def _fetch_market_data(self, market_id: str) -> Optional[dict]:
        """Fetch market-level data (volume, momentum) from Gamma API for smart exits."""
        try:
            from .config import GAMMA_API_BASE
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{GAMMA_API_BASE}/markets/{market_id}",
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    return None
                return resp.json()
        except Exception as e:
            logger.debug(f"Market data fetch failed for {market_id[:20]}: {e}")
            return None

    def _get_entry_edge(self, trade_id: int) -> Decimal:
        """Look up the edge at entry time.

        Tries trade_history.edge first (if column exists), then estimates
        from the take_profit_price vs entry_price ratio in managed_positions.
        Falls back to 0.05 (5%) as a conservative default.
        """
        try:
            with get_connection(self.db_path) as conn:
                cursor = conn.cursor()
                # Try the edge column (may not exist in older schemas)
                try:
                    cursor.execute(
                        "SELECT edge FROM trade_history WHERE id = ?", (trade_id,)
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        return Decimal(str(row[0]))
                except Exception:
                    pass
                # Estimate from TP/entry ratio: if TP was +40%, edge was ~8-10%
                cursor.execute(
                    "SELECT entry_price, take_profit_price FROM managed_positions WHERE trade_id = ?",
                    (trade_id,)
                )
                row = cursor.fetchone()
                if row and row[0] and row[1]:
                    entry = Decimal(str(row[0]))
                    tp = Decimal(str(row[1]))
                    if entry > 0:
                        # Rough estimate: edge ≈ (TP - entry) / 4
                        return max(Decimal("0.01"), (tp - entry) / 4)
        except Exception:
            pass
        return Decimal("0.05")

    def _get_strategy(self, trade_id: int) -> str:
        """Look up the strategy from trade_history."""
        try:
            with get_connection(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT strategy FROM trade_history WHERE id = ?", (trade_id,)
                )
                row = cursor.fetchone()
                if row and row[0]:
                    return row[0].upper()
        except Exception:
            pass
        return "UNKNOWN"

    async def _build_smart_snapshot(
        self, pos: 'ManagedPosition', book: dict, market_data: Optional[dict]
    ) -> MarketSnapshot:
        """Build a MarketSnapshot for the smart exit engine from raw data."""
        bid = book["bid"]
        ask = book["ask"]
        mid = book["mid"]
        spread = book["spread"]
        spread_pct = spread / mid if mid > 0 else Decimal("1")

        # Extract volume and momentum from Gamma market data
        volume_24h = Decimal("0")
        momentum_1h = Decimal("0")
        if market_data:
            try:
                volume_24h = Decimal(str(market_data.get("volume24hr", 0) or 0))
            except Exception:
                pass
            try:
                momentum_1h = Decimal(str(market_data.get("oneHourPriceChange", 0) or 0))
            except Exception:
                pass

        # Re-calculate current edge using the edge engine
        from .edge import analyze_binary_market
        edge_analysis = analyze_binary_market(
            yes_ask=ask, yes_bid=bid,
            momentum=momentum_1h, volume_24h=volume_24h,
        )

        # Determine current edge on OUR side
        if pos.side == "BUY" or pos.side == "YES":
            current_edge = edge_analysis.yes_edge
        else:
            current_edge = edge_analysis.no_edge

        entry_edge = self._get_entry_edge(pos.trade_id)

        return MarketSnapshot(
            bid=bid,
            ask=ask,
            mid=mid,
            spread=spread,
            spread_pct=spread_pct,
            volume_24h=volume_24h,
            momentum_1h=momentum_1h,
            book_depth_bid=book["book_depth_bid"],
            book_depth_ask=book["book_depth_ask"],
            current_edge=current_edge,
            edge_at_entry=entry_edge,
        )

    # ------------------------------------------------------------------
    # Core exit-check logic
    # ------------------------------------------------------------------
    async def check_exits(self) -> list[ExitSignal]:
        """Check all active positions for exit conditions.

        Two-pass system:
          Pass 1: Fixed exits (TP/SL/trailing/time) — fast, no extra API calls.
          Pass 2: Smart exits — fetches full orderbook + market data, runs the
                  intelligent reassessment engine for positions that Pass 1 didn't exit.

        Returns a list of ExitSignal objects for positions that should be closed.
        """
        positions = self._load_active_positions()
        if not positions:
            return []

        exit_signals: list[ExitSignal] = []
        positions_for_smart_eval: list[tuple[ManagedPosition, Decimal, Decimal]] = []

        for pos in positions:
            # Skip arb positions — they resolve on their own
            if pos.side == "BUY_BOTH":
                continue

            # Fetch live price (mid for checks, bid for selling)
            price_data = await self._fetch_live_price(pos.token_id)

            # --- DEAD MARKET DETECTION ---
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
                pos.trailing_stop_price = live_price * (1 - TRAILING_STOP_PCT)

            # Persist updated prices
            self._update_position_prices(pos)

            # --- Safety: skip exit if bid is unreliable ---
            spread = live_price - bid_price if live_price > bid_price else Decimal("0")
            if spread > Decimal("0.08"):
                logger.debug(f"Skipping exit check for {pos.market_question[:30]}: spread ${spread:.3f} too wide")
                continue

            bid_too_low = bid_price < pos.entry_price * Decimal("0.40")

            # ═══ PASS 1: Fixed exit conditions (fast, no extra API calls) ═══
            fixed_exit = False

            # 1) TAKE PROFIT
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
                fixed_exit = True

            # 2) STOP LOSS
            elif bid_price <= pos.stop_loss_price:
                pnl = (bid_price - pos.entry_price) * pos.size
                exit_signals.append(ExitSignal(
                    position_id=pos.position_id,
                    reason="STOP_LOSS",
                    trigger_price=bid_price,
                    expected_pnl=pnl,
                    urgency=3,
                ))
                print(f"   STOP LOSS hit: {pos.market_question[:40]}... ${pnl:.2f}")
                fixed_exit = True

            # 3) TRAILING STOP
            elif not bid_too_low and live_price > pos.entry_price and live_price <= pos.trailing_stop_price:
                pnl = (bid_price - pos.entry_price) * pos.size
                exit_signals.append(ExitSignal(
                    position_id=pos.position_id,
                    reason="TRAILING_STOP",
                    trigger_price=bid_price,
                    expected_pnl=pnl,
                    urgency=2,
                ))
                print(f"   TRAILING STOP: {pos.market_question[:40]}... +${pnl:.2f}")
                fixed_exit = True

            # 4) TIME-BASED EXIT
            elif pos.hold_hours > MAX_HOLD_HOURS:
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
                fixed_exit = True

            # If no fixed exit triggered, queue for smart evaluation
            if not fixed_exit:
                positions_for_smart_eval.append((pos, live_price, bid_price))

        # ═══ PASS 2: Smart exit evaluation (market-aware, calculated) ═══
        if SMART_EXIT_ENABLED and positions_for_smart_eval:
            smart_signals = await self._run_smart_exits(positions_for_smart_eval)
            exit_signals.extend(smart_signals)

        # Sort by urgency (critical first)
        exit_signals.sort(key=lambda s: s.urgency, reverse=True)
        return exit_signals

    async def _run_smart_exits(
        self,
        positions: list[tuple[ManagedPosition, Decimal, Decimal]],
    ) -> list[ExitSignal]:
        """Run the intelligent exit engine on positions that didn't trigger fixed exits.

        For each position:
        1. Fetch full orderbook (depth, spread)
        2. Fetch market data (volume, momentum)
        3. Re-calculate edge on our side
        4. Evaluate composite health score
        5. Make calculated exit/hold/tighten decision
        """
        smart_signals: list[ExitSignal] = []

        for pos, live_price, bid_price in positions:
            try:
                # Fetch full market intelligence
                book = await self._fetch_full_book(pos.token_id)
                if not book:
                    continue

                market_data = await self._fetch_market_data(pos.market_id)
                snapshot = await self._build_smart_snapshot(pos, book, market_data)

                strategy = self._get_strategy(pos.trade_id)

                pos_ctx = PositionContext(
                    entry_price=pos.entry_price,
                    current_price=pos.current_price,
                    high_water_mark=pos.high_water_mark,
                    size=pos.size,
                    cost_basis=pos.cost_basis,
                    hold_hours=pos.hold_hours,
                    side=pos.side,
                    strategy=strategy,
                )

                # Run the smart exit engine
                verdict = evaluate_position(snapshot, pos_ctx)

                if verdict.should_exit:
                    pnl = (bid_price - pos.entry_price) * pos.size
                    smart_signals.append(ExitSignal(
                        position_id=pos.position_id,
                        reason=verdict.reason,
                        trigger_price=bid_price,
                        expected_pnl=pnl,
                        urgency=verdict.urgency,
                    ))
                    print(
                        f"   SMART EXIT [{verdict.reason}]: "
                        f"{pos.market_question[:40]}... "
                        f"${pnl:+.2f} (health={verdict.health_score:.2f})"
                    )
                    logger.info(
                        f"Smart exit for #{pos.position_id}: {verdict.explanation}"
                    )

                elif verdict.new_trailing_pct is not None:
                    # Tighten trailing stop without exiting
                    new_trail_price = pos.high_water_mark * (1 - verdict.new_trailing_pct)
                    if new_trail_price > pos.trailing_stop_price:
                        pos.trailing_stop_price = new_trail_price
                        self._update_position_prices(pos)
                        logger.info(
                            f"Tightened trail for #{pos.position_id}: "
                            f"→ ${new_trail_price:.4f} ({verdict.new_trailing_pct:.1%} trail) | "
                            f"{verdict.explanation}"
                        )

            except Exception as e:
                logger.debug(f"Smart exit eval failed for #{pos.position_id}: {e}")
                continue

        return smart_signals

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
        """Close a managed position AND sync the learning ledger.

        This is the single authority for a *managed* exit.  Previously it only
        touched managed_positions, so real profitable exits never reached
        trade_history — and resolution.py later booked the same position's
        eventual worthless expiry as a total loss.  That double-count is why
        trade_history showed 0 wins while the manager booked 21.  Now the
        manager's realised exit is the truth of record in BOTH tables, and
        resolution.py skips anything already closed here.
        """
        won = pnl > Decimal("0")
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

            # Sync the learning ledger (trade_history) with the REAL exit.
            # pnl here is already computed on shares, so it is unit-correct.
            row = cursor.execute(
                "SELECT trade_id FROM managed_positions WHERE id = ?", (position_id,)
            ).fetchone()
            if row and row[0] is not None:
                trade_id = row[0]
                cursor.execute("""
                    UPDATE trade_history
                    SET status = ?, exit_price = ?, pnl = ?, resolved_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'PENDING'
                """, ("WON" if won else "LOST", str(price), str(pnl), trade_id))
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
