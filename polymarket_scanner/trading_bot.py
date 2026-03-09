"""Autonomous Trading Bot for Polymarket — v3.

Complete trading system with:
1. Arbitrage detection (risk-free profit)
2. Swing/Scalp trading (profit from price movement, not resolution)
3. Momentum signals (buy active movers)
4. Smart strategies (contrarian, correlation, volume spike)
5. Active position management (per-strategy TP/SL/trailing stops)
6. Risk-managed capital allocation (no more all-in bets)
7. Performance dashboard (CLI + Web on localhost:8080)

RISK MANAGEMENT:
- Max 10% of balance per trade
- Per-strategy capital budgets
- $1.00 reserve floor
- Different exit profiles per strategy
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime
from decimal import Decimal
from typing import Optional

from .config import DB_PATH
from .database import init_database, save_opportunity
from .detection import scan_market_for_opportunities
from .executor import TradingExecutor
from .ingestion.gamma import GammaAPIClient, parse_market
from .ingestion.clob import CLOBAPIClient
from .models import OpportunityType
# SignalGenerator removed — WhaleTracker is a stub returning [].
# Kept signals.py for future implementation.
from .learning import LearningEngine
from .resolution import ResolutionTracker
from .aggressive import AggressiveTrader
from .position_manager import PositionManager
from .smart_strategy import SmartStrategy
from .swing_trader import SwingTrader
from .risk_manager import RiskManager
from .dashboard import start_web_dashboard, print_cli_dashboard
from .quant_engine import QuantEngine, extract_features
from .trading_config import (
    STARTING_BALANCE,
    STOP_LOSS_THRESHOLD,
    ARB_BET_SIZE,
    SIGNAL_BET_SIZE,
    MAX_TRADES_PER_HOUR,
    DASHBOARD_PORT,
    MIN_GLOBAL_CONFIDENCE,
    MAX_ENTRY_PRICE,
)

logger = logging.getLogger(__name__)


class TradingBot:
    """Autonomous trading bot for Polymarket — v3."""
    
    def __init__(self, paper_trading: bool = True):
        self.paper_trading = paper_trading
        self.gamma = GammaAPIClient()
        self.clob = CLOBAPIClient()
        self.executor = TradingExecutor(paper_trading=paper_trading)
        self.learning = LearningEngine()
        self.resolution_tracker = ResolutionTracker()
        self.aggressive_trader = AggressiveTrader()
        
        # ─── v2 components ───
        self.position_manager = PositionManager(executor=self.executor)
        self.smart_strategy = SmartStrategy()
        
        # ─── v3 components ───
        self.swing_trader = SwingTrader()
        self.risk_manager = RiskManager()
        
        # ─── v4: Adaptive quant engine ───
        self.quant_engine = QuantEngine()
        
        self.running = False
        self.trades_this_hour = 0
        self.last_hour_reset = datetime.utcnow()
        self.last_resolution_check = datetime.utcnow()
        self.capital_recycled = Decimal("0")
        
        # ─── Dedup: prevent placing multiple bets on the same market ───
        self._recently_traded_markets: set[str] = set()
        self._recently_traded_tokens: set[str] = set()
        # ─── Event-level dedup: prevent multi-bucket bets on same event ───
        # e.g. betting on 5 Elon tweet-count ranges simultaneously
        self._recently_traded_events: set[str] = set()
        # Separate timer for dedup clearing (6 hours, not hourly)
        self._last_dedup_clear = datetime.utcnow()
        
    def initialize(self) -> bool:
        """Initialize the bot and all sub-systems."""
        init_database(DB_PATH)
        
        self.executor.balance = STARTING_BALANCE
        
        if not self.executor.initialize():
            logger.error("Failed to initialize executor")
            return False
        
        # Sync real account state on startup (live mode only)
        if not self.paper_trading:
            self.executor.sync_balance_from_api()
        
        # Load quant engine state (Bayesian models, calibration, strategy health)
        self.quant_engine.load_state()
        logger.info("Quant engine loaded — adaptive scoring active")
        
        # Populate dedup set from recent trades in DB (survive restarts)
        try:
            from .database import get_connection
            with get_connection(DB_PATH) as conn:
                cursor = conn.cursor()
                # Load all markets traded in the last 6 hours
                cursor.execute(
                    "SELECT DISTINCT market_id, token_id FROM trade_history WHERE timestamp > datetime('now', '-2 hours')"
                )
                for row in cursor.fetchall():
                    if row[0]:
                        self._recently_traded_markets.add(row[0])
                    if row[1]:
                        self._recently_traded_tokens.add(row[1])
                # Also load ACTIVE managed positions
                cursor.execute(
                    "SELECT DISTINCT market_id, token_id FROM managed_positions WHERE status = 'ACTIVE'"
                )
                for row in cursor.fetchall():
                    if row[0]:
                        self._recently_traded_markets.add(row[0])
                    if row[1]:
                        self._recently_traded_tokens.add(row[1])
            logger.info(f"Dedup loaded: {len(self._recently_traded_markets)} markets, {len(self._recently_traded_tokens)} tokens from recent history")
        except Exception as e:
            logger.warning(f"Failed to load dedup history: {e}")
        
        # Start web dashboard in background
        try:
            start_web_dashboard(port=DASHBOARD_PORT, db_path=DB_PATH)
        except Exception as e:
            logger.warning(f"Dashboard failed to start: {e} (continuing without it)")
        
        logger.info(f"Trading bot v4 initialized (paper_trading={self.paper_trading})")
        logger.info(f"Starting balance: ${self.executor.balance}")
        logger.info(f"Stop loss: ${STOP_LOSS_THRESHOLD}")
        logger.info(f"Risk manager active — per-strategy budgets enforced")
        return True
    
    def _check_hourly_limits(self) -> bool:
        """Reset hourly trade counter and check limits."""
        now = datetime.utcnow()
        if (now - self.last_hour_reset).seconds >= 3600:
            self.trades_this_hour = 0
            self.last_hour_reset = now
        # Clear stale dedup entries every 2 hours — allows re-entry into markets faster
        if (now - self._last_dedup_clear).total_seconds() >= 7200:  # 2 hours
            self._recently_traded_markets.clear()
            self._recently_traded_tokens.clear()
            self._recently_traded_events.clear()
            self._last_dedup_clear = now
            logger.info("Dedup sets cleared (2-hour cycle)")
        return self.trades_this_hour < MAX_TRADES_PER_HOUR
    
    def _get_event_key(self, market_question: str) -> str:
        """Extract an event key from a market question for event-level dedup.

        Markets in the same event share question structure, e.g.:
          "Will Elon tweet 340-359 times?" and "Will Elon tweet 360-379 times?"
        We normalize by stripping numbers and range patterns to get a common key.
        """
        import re
        # Remove numbers, ranges (e.g. "340-359"), dollar amounts, percentages
        key = re.sub(r'\d+[\.,]?\d*[%k]?', '#', market_question.lower())
        # Remove range patterns like "#-#"
        key = re.sub(r'#\s*[-–to]+\s*#', '#RANGE#', key)
        # Collapse whitespace
        key = re.sub(r'\s+', ' ', key).strip()
        return key

    def _has_position_in_market(self, market_id: str, token_id: str, market_question: str = "") -> bool:
        """Check if we already have a position or recent trade in this market OR event.

        Prevents the bot from piling into the same market every cycle.
        Also prevents multi-bucket bets on the same event (e.g. 5 Elon tweet ranges).
        Checks:
          1. In-memory recently-traded set (catches even failed orders)
          2. Event-level dedup (same event = only 1 bet allowed)
          3. Database ACTIVE managed positions
        """
        # Check in-memory dedup (includes failed/unfilled attempts)
        if market_id in self._recently_traded_markets:
            return True
        if token_id in self._recently_traded_tokens:
            return True

        # ─── EVENT-LEVEL DEDUP: prevent multi-bucket bets ───
        if market_question:
            event_key = self._get_event_key(market_question)
            if event_key in self._recently_traded_events:
                logger.info(f"Event-level dedup: already traded this event type ({event_key[:50]})")
                return True

        # Check database for ACTIVE positions in this market
        try:
            from .database import get_connection
            with get_connection(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM managed_positions WHERE market_id = ? AND status = 'ACTIVE'",
                    (market_id,)
                )
                if cursor.fetchone()[0] > 0:
                    return True

                # Also check trade_history for recent trades (last 6 hours)
                cursor.execute(
                    "SELECT COUNT(*) FROM trade_history WHERE market_id = ? AND timestamp > datetime('now', '-2 hours')",
                    (market_id,)
                )
                if cursor.fetchone()[0] > 0:
                    return True
        except Exception:
            pass

        return False

    def _mark_market_traded(self, market_id: str, token_id: str, market_question: str = ""):
        """Mark a market as traded to prevent re-entry."""
        self._recently_traded_markets.add(market_id)
        self._recently_traded_tokens.add(token_id)
        # Also mark the event so we don't bet on other buckets of the same event
        if market_question:
            event_key = self._get_event_key(market_question)
            self._recently_traded_events.add(event_key)
    
    # ==================================================================
    # Phase 1: Check exits on existing positions (SELL logic)
    # ==================================================================
    async def manage_positions(self) -> Decimal:
        """Check all positions for exit conditions and execute sells.
        
        Returns:
            Total USDC recovered (capital recycled)
        """
        recycled = Decimal("0")
        
        exit_signals = await self.position_manager.check_exits()
        
        if not exit_signals:
            return recycled
        
        print(f"  → {len(exit_signals)} exit signal(s) detected")
        
        for sig in exit_signals:
            recovered = await self.position_manager.execute_exit(sig)
            if recovered:
                recycled += recovered
                self.executor.balance += recovered
                self.capital_recycled += recovered
                self.executor.open_positions = max(0, self.executor.open_positions - 1)
                
                # ─── QUANT ENGINE: Record outcome for learning ───
                try:
                    pos = next(
                        (p for p in self.position_manager._load_active_positions()
                         if p.position_id == sig.position_id), None
                    )
                    if pos is None:
                        # Position already closed, reconstruct minimal features
                        won = sig.expected_pnl > 0
                        features = extract_features(
                            strategy="UNKNOWN", mode="unknown", side="BUY",
                            price=float(sig.trigger_price),
                            edge=0.05,
                            confidence=0.5,
                        )
                    else:
                        won = sig.expected_pnl > 0
                        # Look up strategy from trade_history
                        strat = "UNKNOWN"
                        try:
                            from .database import get_connection as _gc
                            with _gc(DB_PATH) as conn:
                                cursor = conn.cursor()
                                cursor.execute(
                                    "SELECT strategy FROM trade_history WHERE id = ?",
                                    (pos.trade_id,)
                                )
                                row = cursor.fetchone()
                                if row:
                                    strat = row[0] or "UNKNOWN"
                        except Exception:
                            pass
                        features = extract_features(
                            strategy=strat, mode=sig.reason.lower(), side=pos.side,
                            price=float(pos.entry_price),
                            spread=0.03,  # approximate
                            edge=float(pos.take_profit_price - pos.entry_price) / float(pos.entry_price) if pos.entry_price > 0 else 0.05,
                            confidence=0.5,
                        )
                    self.quant_engine.record_outcome(
                        features, won=won,
                        pnl=float(sig.expected_pnl),
                        trade_id=getattr(pos, 'trade_id', 0) if pos else 0,
                    )
                except Exception as e:
                    logger.debug(f"Quant outcome recording failed: {e}")
        
        if recycled > Decimal("0"):
            print(f"  ♻️  Capital recycled: ${recycled:.2f} → new balance: ${self.executor.balance:.2f}")
        
        return recycled
    
    # ==================================================================
    # Phase 2: Scan for arbitrage
    # ==================================================================
    async def scan_for_arbitrage(self) -> int:
        """Scan markets for arbitrage opportunities.
        
        Returns:
            Number of opportunities found and traded
        """
        traded = 0
        markets_checked = 0
        max_markets_per_cycle = 60
        
        async for market_data in self.gamma.iter_all_markets(active=True):
            if not self.running:
                break
            
            markets_checked += 1
            if markets_checked > max_markets_per_cycle:
                break
            
            # Check balance
            balance = self.executor.get_balance()
            if balance <= STOP_LOSS_THRESHOLD:
                logger.warning(f"Stop loss triggered! Balance: ${balance}")
                self.running = False
                break
            
            market = parse_market(market_data)
            if not market.outcomes:
                continue
            
            # Fetch orderbooks
            orderbooks = {}
            for outcome in market.outcomes:
                if outcome.outcome_id:
                    book = await self.clob.get_orderbook(outcome.outcome_id)
                    if book:
                        orderbooks[outcome.outcome_id] = book
            
            if len(orderbooks) < len(market.outcomes):
                continue
            
            # Get adaptive bet size from learning engine, then validate with risk manager
            optimal_bet = self.learning.get_optimal_bet_size(
                "ARB", ARB_BET_SIZE, self.executor.get_balance()
            )
            
            # Risk manager gate
            allowed, risk_sized_bet, risk_reason = self.risk_manager.check_trade(
                "ARB", optimal_bet, self.executor.get_balance()
            )
            if not allowed:
                continue
            optimal_bet = risk_sized_bet
            arb_profile = self.risk_manager.get_strategy_profile("ARB")
            
            # Check for arbitrage
            opportunities = scan_market_for_opportunities(market, orderbooks, optimal_bet)
            
            for opp in opportunities:
                if opp.opportunity_type == OpportunityType.COMPLEMENT_ARB:
                    # Check if category is historically profitable
                    category = market_data.get("category", "unknown")
                    should_trade, reason = self.learning.should_trade_category(category)
                    
                    print(f"\n🎯 ARB FOUND: {market.question[:50]}...")
                    print(f"   Profit: ${opp.profit_bound:.4f} | Bet: ${optimal_bet}")
                    print(f"   Category: {category} ({reason})")
                    
                    if not should_trade:
                        print(f"   ⚠️ Skipping due to poor category history")
                        continue
                    
                    # Record trade for learning
                    trade_id = self.learning.record_trade(
                        strategy="ARB",
                        market_id=market.market_id,
                        market_question=market.question,
                        token_id=market.outcomes[0].outcome_id if market.outcomes else "",
                        side="BUY_BOTH",
                        entry_price=Decimal("1") - opp.profit_bound,
                        size=optimal_bet,
                        category=category,
                    )
                    
                    # Execute!
                    if self.executor.execute_arbitrage(opp):
                        self.resolution_tracker.record_position(
                            trade_id=trade_id,
                            market_id=market.market_id,
                            token_id=market.outcomes[0].outcome_id if market.outcomes else "",
                            side="BUY_BOTH",
                            entry_price=Decimal("1") - opp.profit_bound,
                            size=optimal_bet,
                        )
                        
                        # Register with position manager for active tracking
                        self.position_manager.register_position(
                            trade_id=trade_id,
                            market_id=market.market_id,
                            token_id=market.outcomes[0].outcome_id if market.outcomes else "",
                            side="BUY_BOTH",
                            entry_price=Decimal("1") - opp.profit_bound,
                            size=optimal_bet,
                            market_question=market.question,
                            take_profit_pct=arb_profile.take_profit_pct,
                            stop_loss_pct=arb_profile.stop_loss_pct,
                            trailing_stop_pct=arb_profile.trailing_stop_pct,
                        )
                        
                        traded += 1
                        self.trades_this_hour += 1
                        save_opportunity(opp, DB_PATH)
                        
                        # Update balance after trade
                        self.executor.balance -= optimal_bet
                        
                        if not self._check_hourly_limits():
                            logger.info("Hourly trade limit reached")
                            return traded
        
        return traded
    
    # ==================================================================
    # Phase 3: Swing / Scalp trades (profit from price movement)
    # ==================================================================
    async def check_swing_trades(self) -> int:
        """Run swing/scalp strategies — buy to sell for quick profit."""
        traded = 0
        
        if not self._check_hourly_limits():
            return traded
        
        swing_signals = await self.swing_trader.find_swing_opportunities(max_signals=5)
        
        if swing_signals:
            actionable = [s for s in swing_signals if s.is_actionable]
            logger.info(
                f"Swing scan: {len(swing_signals)} signals, {len(actionable)} actionable"
            )
            for s in swing_signals:
                if not s.is_actionable:
                    reasons = []
                    if s.confidence < 0.55: reasons.append(f"conf={s.confidence:.0%}<55%")
                    if s.edge_estimate < Decimal("0.03"): reasons.append(f"edge={s.edge_estimate:.1%}<3%")
                    if s.reward_risk_ratio < 1.3: reasons.append(f"R:R={s.reward_risk_ratio:.1f}<1.3")
                    if s.liquidity_score < 0.3: reasons.append(f"liq={s.liquidity_score:.2f}<0.3")
                    logger.debug(f"Swing rejected [{s.mode}]: {', '.join(reasons)} — {s.market_question[:40]}")
        
        for sig in swing_signals:
            if traded >= 3:
                break
            if not sig.is_actionable:
                continue

            # ─── GLOBAL CONFIDENCE GATE ───
            if sig.confidence < MIN_GLOBAL_CONFIDENCE:
                logger.info(f"Swing skipped (conf {sig.confidence:.0%} < {MIN_GLOBAL_CONFIDENCE:.0%}): {sig.market_question[:40]}")
                continue

            # ─── MAX ENTRY PRICE: Never buy expensive outcomes ───
            if sig.current_price > MAX_ENTRY_PRICE:
                logger.info(f"Swing skipped (price ${sig.current_price:.3f} > ${MAX_ENTRY_PRICE}): {sig.market_question[:40]}")
                continue

            # ─── DEDUP: Skip if we already have a position in this market ───
            if self._has_position_in_market(sig.market_id, sig.token_id, sig.market_question):
                logger.info(f"Swing skipped (already traded): {sig.market_question[:40]}")
                continue

            balance = self.executor.get_balance()

            # Risk manager decides size
            allowed, bet_size, risk_reason = self.risk_manager.check_trade(
                "SWING", SIGNAL_BET_SIZE, balance
            )
            if not allowed:
                logger.info(f"Swing blocked: {risk_reason}")
                continue

            profile = self.risk_manager.get_strategy_profile("SWING")

            # ─── QUANT ENGINE: Score this opportunity ───
            features = extract_features(
                strategy="SWING", mode=sig.mode, side="BUY",
                price=float(sig.current_price),
                spread=abs(float(sig.current_price - sig.stop_price)) if sig.current_price > 0 else 0.05,
                volume_24h=float(sig.volume_24h),
                momentum_1h=0.0,
                edge=float(sig.edge_estimate),
                confidence=sig.confidence,
                liquidity_score=sig.liquidity_score,
            )
            quant_score = self.quant_engine.score_opportunity(features)
            if not quant_score.should_trade:
                logger.info(f"Swing blocked by quant engine: {quant_score.reason}")
                continue

            # Adjust bet size by quant recommendation
            if quant_score.recommended_size_pct > 0:
                quant_bet = (balance * Decimal(str(quant_score.recommended_size_pct))).quantize(Decimal("0.01"))
                bet_size = min(bet_size, max(Decimal("0.10"), quant_bet))

            print(f"\n SWING [{sig.mode}]: {sig.market_question[:50]}...")
            print(f"   {sig.rationale}")
            print(f"   R:R {sig.reward_risk_ratio:.1f} | Conf: {sig.confidence:.0%} | Bet: ${bet_size:.2f}")
            print(f"   Quant: score={quant_score.total_score:.2f} edge={quant_score.adjusted_edge:.1%} quality={quant_score.market_quality:.2f}")

            # Mark as traded BEFORE execution to prevent re-entry even if order fails
            self._mark_market_traded(sig.market_id, sig.token_id, sig.market_question)
            
            trade_id = self.learning.record_trade(
                strategy="SWING",
                market_id=sig.market_id,
                market_question=sig.market_question,
                token_id=sig.token_id,
                side=sig.side,
                entry_price=sig.current_price,
                size=bet_size,
                category=sig.mode.lower(),
            )
            
            if self.executor.execute_signal_trade(
                sig.token_id, sig.side,
                sig.current_price, f"swing_{sig.mode}",
            ):
                shares = bet_size / sig.current_price if sig.current_price > 0 else Decimal("5")
                shares = max(shares, Decimal("5"))
                
                self.position_manager.register_position(
                    trade_id=trade_id,
                    market_id=sig.market_id,
                    token_id=sig.token_id,
                    side=sig.side,
                    entry_price=sig.current_price,
                    size=shares,
                    market_question=sig.market_question,
                    take_profit_pct=profile.take_profit_pct,
                    stop_loss_pct=profile.stop_loss_pct,
                    trailing_stop_pct=profile.trailing_stop_pct,
                )
                
                self.swing_trader.record_trade(sig.token_id)
                self.executor.balance -= bet_size
                traded += 1
                self.trades_this_hour += 1
                print(f"   ✅ Swing trade executed! (TP +{profile.take_profit_pct:.0%} / SL -{profile.stop_loss_pct:.0%})")
        
        return traded
    
    # ==================================================================
    # Phase 4: Smart signals (momentum + contrarian + correlation)
    # ==================================================================
    async def check_signals(self) -> int:
        """Check for signal-based opportunities using ALL strategies."""
        traded = 0
        
        # ─── 4a: Momentum ───
        momentum_signals = await self.aggressive_trader.find_momentum_opportunities(
            min_price_change=Decimal("0.02"),
            min_volume=Decimal("3000"),
            max_opportunities=5,
        )

        for signal in momentum_signals:
            if traded >= 4:
                break

            # ─── GLOBAL CONFIDENCE GATE ───
            if signal.confidence < MIN_GLOBAL_CONFIDENCE:
                logger.info(f"Momentum skipped (conf {signal.confidence:.0%} < {MIN_GLOBAL_CONFIDENCE:.0%}): {signal.market_question[:40]}")
                continue

            # ─── MAX ENTRY PRICE: Never buy expensive outcomes ───
            if signal.current_price > MAX_ENTRY_PRICE:
                logger.info(f"Momentum skipped (price ${signal.current_price:.3f} > ${MAX_ENTRY_PRICE}): {signal.market_question[:40]}")
                continue

            # ─── DEDUP: Skip if already traded ───
            if self._has_position_in_market(signal.market_id, signal.token_id, signal.market_question):
                logger.info(f"Momentum skipped (already traded): {signal.market_question[:40]}")
                continue

            balance = self.executor.get_balance()

            # Risk manager gate
            allowed, bet_size, risk_reason = self.risk_manager.check_trade(
                "MOMENTUM", SIGNAL_BET_SIZE, balance
            )
            if not allowed:
                logger.info(f"Momentum blocked: {risk_reason}")
                continue

            mom_profile = self.risk_manager.get_strategy_profile("MOMENTUM")

            # ─── QUANT ENGINE: Score this momentum opportunity ───
            features = extract_features(
                strategy="MOMENTUM", mode="momentum", side=signal.side,
                price=float(signal.current_price),
                volume_24h=float(signal.volume_24h),
                momentum_1h=float(signal.price_change_1h),
                edge=0.05,  # momentum doesn't have explicit edge
                confidence=signal.confidence,
                liquidity_score=min(1.0, float(signal.volume_24h) / 100000),
            )
            quant_score = self.quant_engine.score_opportunity(features)
            if not quant_score.should_trade:
                logger.info(f"Momentum blocked by quant engine: {quant_score.reason}")
                continue

            # Adjust bet size
            if quant_score.recommended_size_pct > 0:
                quant_bet = (balance * Decimal(str(quant_score.recommended_size_pct))).quantize(Decimal("0.01"))
                bet_size = min(bet_size, max(Decimal("0.10"), quant_bet))

            print(f"\n MOMENTUM: {signal.market_question[:50]}...")
            print(f"   {signal.rationale} | Confidence: {signal.confidence:.0%}")
            print(f"   Price: ${signal.current_price:.3f} | Bet: ${bet_size:.2f}")
            print(f"   Quant: score={quant_score.total_score:.2f} quality={quant_score.market_quality:.2f}")

            # Mark as traded BEFORE execution
            self._mark_market_traded(signal.market_id, signal.token_id, signal.market_question)
            
            trade_id = self.learning.record_trade(
                strategy="MOMENTUM",
                market_id=signal.market_id,
                market_question=signal.market_question,
                token_id=signal.token_id,
                side=signal.side,
                entry_price=signal.current_price,
                size=bet_size,
                category="momentum",
            )
            
            if self.executor.execute_signal_trade(
                signal.token_id,
                signal.side,
                signal.current_price,
                f"momentum_{signal.rationale[:20]}",
            ):
                shares = bet_size / signal.current_price if signal.current_price > 0 else Decimal("5")
                shares = max(shares, Decimal("5"))
                
                # Track for resolution
                self.resolution_tracker.record_position(
                    trade_id=trade_id,
                    market_id=signal.market_id,
                    token_id=signal.token_id,
                    side=signal.side,
                    entry_price=signal.current_price,
                    size=bet_size,
                )
                
                # Register with per-strategy exit profile
                self.position_manager.register_position(
                    trade_id=trade_id,
                    market_id=signal.market_id,
                    token_id=signal.token_id,
                    side=signal.side,
                    entry_price=signal.current_price,
                    size=shares,
                    market_question=signal.market_question,
                    take_profit_pct=mom_profile.take_profit_pct,
                    stop_loss_pct=mom_profile.stop_loss_pct,
                    trailing_stop_pct=mom_profile.trailing_stop_pct,
                )
                
                self.aggressive_trader.record_trade(signal.market_id)
                self.executor.balance -= bet_size
                traded += 1
                self.trades_this_hour += 1
                print(f"   ✅ Trade executed! (${bet_size:.2f})")
        
        # ─── 4b: Smart strategies (contrarian, correlation, vol spike) ───
        if traded < 6:
            smart_signals = await self.smart_strategy.generate_all_signals(max_signals=8)

            if smart_signals:
                actionable = [s for s in smart_signals if s.is_actionable]
                logger.info(
                    f"Smart scan: {len(smart_signals)} signals, {len(actionable)} actionable "
                    f"({', '.join(s.strategy for s in smart_signals)})"
                )

            for sig in smart_signals:
                if traded >= 6:
                    break
                if not sig.is_actionable:
                    continue

                # ─── GLOBAL CONFIDENCE GATE ───
                if sig.confidence < MIN_GLOBAL_CONFIDENCE:
                    logger.info(f"Smart [{sig.strategy}] skipped (conf {sig.confidence:.0%} < {MIN_GLOBAL_CONFIDENCE:.0%}): {sig.market_question[:40]}")
                    continue

                # ─── MAX ENTRY PRICE: Never buy expensive outcomes ───
                if sig.current_price > MAX_ENTRY_PRICE:
                    logger.info(f"Smart [{sig.strategy}] skipped (price ${sig.current_price:.3f} > ${MAX_ENTRY_PRICE}): {sig.market_question[:40]}")
                    continue

                # ─── DEDUP: Skip if already traded ───
                if self._has_position_in_market(sig.market_id, sig.token_id, sig.market_question):
                    logger.info(f"Smart skipped (already traded): {sig.market_question[:40]}")
                    continue

                balance = self.executor.get_balance()
                
                # Risk manager gate
                strat_name = sig.strategy.upper()
                allowed, bet_size, risk_reason = self.risk_manager.check_trade(
                    strat_name, SIGNAL_BET_SIZE, balance
                )
                if not allowed:
                    logger.info(f"Smart [{strat_name}] blocked: {risk_reason}")
                    continue
                
                smart_profile = self.risk_manager.get_strategy_profile(strat_name)
                
                # ─── QUANT ENGINE: Score this smart opportunity ───
                features = extract_features(
                    strategy=strat_name, mode=sig.strategy.lower(), side=sig.side,
                    price=float(sig.current_price),
                    edge=float(sig.edge_estimate),
                    confidence=sig.confidence,
                    volume_24h=float(getattr(sig, 'volume_24h', 0) or 0),
                    liquidity_score=0.5,
                )
                quant_score = self.quant_engine.score_opportunity(features)
                if not quant_score.should_trade:
                    logger.info(f"Smart [{strat_name}] blocked by quant engine: {quant_score.reason}")
                    continue
                
                # Adjust bet size
                if quant_score.recommended_size_pct > 0:
                    quant_bet = (balance * Decimal(str(quant_score.recommended_size_pct))).quantize(Decimal("0.01"))
                    bet_size = min(bet_size, max(Decimal("0.10"), quant_bet))
                
                print(f"\n🧠 SMART [{sig.strategy}]: {sig.market_question[:50]}...")
                print(f"   {sig.rationale}")
                print(f"   Edge: {sig.edge_estimate:.1%} | Confidence: {sig.confidence:.0%} | Bet: ${bet_size:.2f}")
                print(f"   🧠 Quant: score={quant_score.total_score:.2f} quality={quant_score.market_quality:.2f}")
                
                # Mark as traded BEFORE execution
                self._mark_market_traded(sig.market_id, sig.token_id, sig.market_question)

                trade_id = self.learning.record_trade(
                    strategy=sig.strategy,
                    market_id=sig.market_id,
                    market_question=sig.market_question,
                    token_id=sig.token_id,
                    side=sig.side,
                    entry_price=sig.current_price,
                    size=bet_size,
                    category=sig.strategy.lower(),
                )
                
                if self.executor.execute_signal_trade(
                    sig.token_id, sig.side,
                    sig.current_price, f"smart_{sig.strategy}",
                ):
                    shares = bet_size / sig.current_price if sig.current_price > 0 else Decimal("5")
                    shares = max(shares, Decimal("5"))
                    
                    self.resolution_tracker.record_position(
                        trade_id=trade_id,
                        market_id=sig.market_id,
                        token_id=sig.token_id,
                        side=sig.side,
                        entry_price=sig.current_price,
                        size=bet_size,
                    )
                    
                    self.position_manager.register_position(
                        trade_id=trade_id,
                        market_id=sig.market_id,
                        token_id=sig.token_id,
                        side=sig.side,
                        entry_price=sig.current_price,
                        size=shares,
                        market_question=sig.market_question,
                        take_profit_pct=smart_profile.take_profit_pct,
                        stop_loss_pct=smart_profile.stop_loss_pct,
                        trailing_stop_pct=smart_profile.trailing_stop_pct,
                    )
                    
                    self.smart_strategy.record_signal(sig.token_id)
                    self.executor.balance -= bet_size
                    traded += 1
                    self.trades_this_hour += 1
                    print(f"   ✅ Smart trade executed! (${bet_size:.2f})")
        
        return traded
    
    # ==================================================================
    # Main loop
    # ==================================================================
    async def run(self, scan_interval: int = 30):
        """Run the trading bot continuously."""
        self.running = True
        
        print("\n" + "=" * 70)
        print("🤖 ROGER — POLYMARKET TRADING BOT v4 (Adaptive Quant)")
        print("=" * 70)
        print(f"Mode: {'PAPER TRADING' if self.paper_trading else '🔴 LIVE TRADING'}")
        print(f"Balance: ${self.executor.balance}")
        print(f"Base bets: Arb ${ARB_BET_SIZE} | Signal ${SIGNAL_BET_SIZE}")
        print(f"Risk rule: Max 5% per trade | $1.00 reserve floor")
        print(f"Strategies: ARB · SWING · MOMENTUM · CORRELATED")
        print(f"Quant Engine: Bayesian scoring · calibration · feature learning")
        print(f"Dashboard: http://localhost:{DASHBOARD_PORT}")
        print("=" * 70)
        self.risk_manager.print_allocation_report(self.executor.balance)
        print("\nPress Ctrl+C to stop\n")
        
        cycle = 0
        while self.running:
            cycle += 1
            balance = self.executor.get_balance()
            
            print(f"\n{'─' * 60}")
            print(
                f"[Cycle {cycle}] Balance: ${balance:.2f} | "
                f"Trades/hr: {self.trades_this_hour} | "
                f"Recycled: ${self.capital_recycled:.2f}"
            )
            
            # ── Phase 0: Check resolved positions (every 5 cycles) ──
            if cycle % 5 == 0:
                print("  → Checking position resolutions...")
                resolved, pnl = await self.resolution_tracker.check_all_positions()
                if resolved > 0:
                    self.executor.balance += pnl
                    print(f"  💰 Resolved {resolved} position(s), PnL: ${pnl:.2f}")
            
            # ── Phase 1: SELL — Check exits on active positions ──
            print("  → Checking position exits (TP/SL/trailing)...")
            recycled = await self.manage_positions()
            
            # Check stop loss
            balance = self.executor.get_balance()
            if balance <= STOP_LOSS_THRESHOLD:
                print(f"\n⛔ STOP LOSS TRIGGERED! Final balance: ${balance}")
                break
            
            # ── Phase 2: BUY — Scan for arbitrage (every cycle — free money) ──
            arb_trades = 0
            print("  → Scanning for arbitrage...")
            arb_trades = await self.scan_for_arbitrage()
            if arb_trades > 0:
                print(f"  ✅ Executed {arb_trades} arb trade(s)")

            # ── Phase 3: BUY — Swing / scalp trades ──
            swing_trades = 0
            if self._check_hourly_limits():
                print("  → Running swing/scalp scanner...")
                swing_trades = await self.check_swing_trades()
                if swing_trades > 0:
                    print(f"  ✅ Executed {swing_trades} swing trade(s)")

            # ── Phase 4: BUY — Signal strategies (always run if budget allows) ──
            signal_trades = 0
            if self._check_hourly_limits():
                print("  → Running signal strategies...")
                signal_trades = await self.check_signals()
                if signal_trades > 0:
                    print(f"  ✅ Executed {signal_trades} signal trade(s)")
            
            # ── Periodic allocation report (every 10 cycles) ──
            if cycle % 10 == 0:
                self.risk_manager.print_allocation_report(balance)
            
            # ── Periodic balance sync from API (every 10 cycles, live only) ──
            if cycle % 10 == 0 and not self.paper_trading:
                synced = self.executor.sync_balance_from_api()
                if synced is not None:
                    logger.info(f"Balance synced from API: ${synced:.2f}")
            
            # ── Save quant engine state & report (every 15 cycles) ──
            if cycle % 15 == 0:
                self.quant_engine.save_state()
                self.quant_engine.print_report()
            
            # Rate limit
            if not self._check_hourly_limits():
                print(f"  ⏸️ Hourly limit reached, waiting 5 min...")
                await asyncio.sleep(300)
            else:
                await asyncio.sleep(scan_interval)
        
        # ── Final reports ──
        self.quant_engine.save_state()
        self.quant_engine.print_report()
        self.position_manager.print_position_report()
        self.resolution_tracker.print_position_report()
        self.learning.print_performance_report()
        print(f"\n🏁 Bot stopped. Final balance: ${self.executor.get_balance()}")
        print(f"   Capital recycled: ${self.capital_recycled:.2f}")
    
    def stop(self):
        """Stop the bot gracefully."""
        self.running = False


async def main():
    """Entry point for the trading bot."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Polymarket Trading Bot v2")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable live trading (default: paper trading)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Seconds between scans (default: 30)",
    )
    parser.add_argument(
        "--dashboard-only",
        action="store_true",
        help="Only run the dashboard (no trading)",
    )
    args = parser.parse_args()
    
    # Setup logging — silence noisy HTTP loggers (64% of log volume)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    
    # Dashboard-only mode
    if args.dashboard_only:
        from .dashboard import run_cli_dashboard
        start_web_dashboard(port=DASHBOARD_PORT, db_path=DB_PATH)
        await run_cli_dashboard(db_path=DB_PATH)
        return
    
    # Create bot
    bot = TradingBot(paper_trading=not args.live)
    
    # Handle Ctrl+C
    def handle_signal(signum, frame):
        print("\n\nReceived interrupt, stopping...")
        bot.stop()
    
    signal.signal(signal.SIGINT, handle_signal)
    
    # Initialize and run
    if not bot.initialize():
        print("Failed to initialize bot")
        sys.exit(1)
    
    await bot.run(scan_interval=args.interval)


if __name__ == "__main__":
    asyncio.run(main())
