"""Whale/smart money tracking for trade signals.

Monitors large trades and profitable users to follow their positions.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

import httpx

from .trading_config import MIN_WHALE_TRADE_SIZE, MIN_SIGNAL_EDGE, GAMMA_HOST

logger = logging.getLogger(__name__)


@dataclass
class WhaleActivity:
    """A detected whale trade."""
    user_address: str
    token_id: str
    market_question: str
    side: str  # "BUY" or "SELL"
    size_usd: Decimal
    price: Decimal
    timestamp: datetime
    
    @property
    def is_significant(self) -> bool:
        return self.size_usd >= MIN_WHALE_TRADE_SIZE


@dataclass
class TradeSignal:
    """A trading signal based on whale activity or other factors."""
    token_id: str
    market_id: str
    market_question: str
    side: str
    suggested_price: Decimal
    confidence: float  # 0-1
    source: str  # e.g., "whale_follow", "consensus", "momentum"
    edge_estimate: Decimal
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()
    
    @property
    def is_actionable(self) -> bool:
        return self.edge_estimate >= MIN_SIGNAL_EDGE and self.confidence >= 0.6


class WhaleTracker:
    """Tracks whale activity on Polymarket."""
    
    def __init__(self):
        self.known_whales: set[str] = set()
        self.recent_activity: list[WhaleActivity] = []
    
    async def get_recent_large_trades(
        self,
        min_size_usd: Decimal = MIN_WHALE_TRADE_SIZE,
        hours_back: int = 6,
    ) -> list[WhaleActivity]:
        """Fetch recent large trades from Polymarket.
        
        Note: Polymarket doesn't have a direct API for this.
        In production, you'd use:
        - On-chain indexing (The Graph, Dune Analytics)
        - Third-party APIs that track trades
        - WebSocket feed monitoring
        """
        # Placeholder - would need blockchain indexing
        logger.info(f"Checking for trades >= ${min_size_usd} in last {hours_back}h")
        
        # In a real implementation, query something like:
        # - Dune Analytics API
        # - Custom indexer
        # - Polymarket's internal trade feed (if available)
        
        return []
    
    async def get_profitable_traders(
        self,
        min_profit_usd: Decimal = Decimal("1000"),
        min_trades: int = 50,
    ) -> list[str]:
        """Find addresses of historically profitable traders.
        
        This would require:
        - Historical trade data
        - Position resolution tracking
        - PnL calculation per address
        """
        # Placeholder - would need historical data analysis
        logger.info(f"Finding traders with >= ${min_profit_usd} profit, {min_trades}+ trades")
        return []
    
    def analyze_market_consensus(
        self,
        market_id: str,
        recent_trades: list[WhaleActivity],
    ) -> Optional[TradeSignal]:
        """Analyze if whales are aligned on a position.
        
        Returns a signal if there's strong consensus among large traders.
        """
        if not recent_trades:
            return None
        
        # Filter to this market
        market_trades = [t for t in recent_trades if market_id in t.token_id]
        
        if len(market_trades) < 3:
            return None  # Need multiple trades for consensus
        
        # Count buy vs sell volume
        buy_volume = sum(t.size_usd for t in market_trades if t.side == "BUY")
        sell_volume = sum(t.size_usd for t in market_trades if t.side == "SELL")
        total = buy_volume + sell_volume
        
        if total == 0:
            return None
        
        buy_ratio = buy_volume / total
        
        # Strong consensus if 70%+ on one side
        if buy_ratio >= 0.7:
            avg_price = sum(t.price * t.size_usd for t in market_trades if t.side == "BUY") / buy_volume
            return TradeSignal(
                token_id=market_trades[0].token_id,
                market_id=market_id,
                market_question=market_trades[0].market_question,
                side="BUY",
                suggested_price=avg_price,
                confidence=float(buy_ratio),
                source="whale_consensus",
                edge_estimate=Decimal("0.05") * Decimal(str(buy_ratio)),
            )
        elif buy_ratio <= 0.3:
            avg_price = sum(t.price * t.size_usd for t in market_trades if t.side == "SELL") / sell_volume
            return TradeSignal(
                token_id=market_trades[0].token_id,
                market_id=market_id,
                market_question=market_trades[0].market_question,
                side="SELL",
                suggested_price=avg_price,
                confidence=float(1 - buy_ratio),
                source="whale_consensus",
                edge_estimate=Decimal("0.05") * Decimal(str(1 - buy_ratio)),
            )
        
        return None


class SignalGenerator:
    """Generates trade signals from multiple sources."""
    
    def __init__(self):
        self.whale_tracker = WhaleTracker()
    
    async def generate_signals(
        self,
        market_ids: list[str] = None,
    ) -> list[TradeSignal]:
        """Generate trading signals for given markets.
        
        Combines:
        - Whale activity tracking
        - Momentum analysis
        - Market mispricing detection
        """
        signals = []
        
        # Get whale activity
        try:
            whale_trades = await self.whale_tracker.get_recent_large_trades()
            
            if market_ids:
                for market_id in market_ids:
                    signal = self.whale_tracker.analyze_market_consensus(
                        market_id, whale_trades
                    )
                    if signal and signal.is_actionable:
                        signals.append(signal)
        except Exception as e:
            logger.error(f"Error generating whale signals: {e}")
        
        return signals
    
    async def get_best_opportunity(self) -> Optional[TradeSignal]:
        """Get the single best trading opportunity right now."""
        signals = await self.generate_signals()
        
        if not signals:
            return None
        
        # Sort by edge * confidence
        signals.sort(
            key=lambda s: float(s.edge_estimate) * s.confidence,
            reverse=True,
        )
        
        return signals[0]
