"""Aggressive trading strategies for active market participation.

Since pure arbitrage is rare, this module implements additional strategies
that will generate trades while maintaining expected positive value.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional
import json

import httpx

from .config import GAMMA_API_BASE
from .trading_config import SIGNAL_BET_SIZE, MIN_SIGNAL_EDGE
from .edge import analyze_market_data, validate_proposed_side, format_edge_summary

logger = logging.getLogger(__name__)


@dataclass
class MomentumSignal:
    """A momentum-based trading signal."""
    token_id: str
    market_id: str
    market_question: str
    side: str  # BUY or SELL
    current_price: Decimal
    price_change_1h: Decimal
    volume_24h: Decimal
    confidence: float
    rationale: str


class AggressiveTrader:
    """Implements active trading strategies beyond pure arbitrage."""
    
    def __init__(self):
        self.recent_trades: list[str] = []  # Track to avoid duplicate trades
        
    async def find_momentum_opportunities(
        self,
        min_price_change: Decimal = Decimal("0.05"),
        min_volume: Decimal = Decimal("1000"),
        max_opportunities: int = 3,
    ) -> list[MomentumSignal]:
        """Find markets with strong recent momentum.
        
        Strategy: Buy into markets showing positive momentum (price increasing),
        as this often indicates incoming news/information.
        
        Args:
            min_price_change: Minimum 1h price change to consider
            min_volume: Minimum 24h volume in USD
            max_opportunities: Maximum signals to return
        """
        signals = []
        
        try:
            async with httpx.AsyncClient() as client:
                # Get active markets sorted by recent activity
                response = await client.get(
                    f"{GAMMA_API_BASE}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": 100,
                    },
                    timeout=30.0,
                )
                response.raise_for_status()
                markets = response.json()

                for market in markets:
                    # Skip if already traded recently
                    market_id = market.get("conditionId", market.get("id", ""))
                    if market_id in self.recent_trades:
                        continue

                    # Get price movement data
                    one_hour_change = market.get("oneHourPriceChange")
                    volume_24h = market.get("volume24hr", 0)

                    if one_hour_change is None:
                        continue

                    price_change = Decimal(str(one_hour_change))
                    volume = Decimal(str(volume_24h))

                    # Skip low volume markets
                    if volume < min_volume:
                        continue

                    # Look for significant price movement
                    if abs(price_change) < min_price_change:
                        continue
                    
                    # Get current best prices
                    best_ask = market.get("bestAsk")
                    best_bid = market.get("bestBid") 
                    
                    if not best_ask or not best_bid:
                        continue
                    
                    # Trade tokens up to MAX_ENTRY_PRICE (config controls this now)
                    ask_price = Decimal(str(best_ask))
                    if ask_price > Decimal("0.50") or ask_price < Decimal("0.02"):
                        continue
                    
                    # Parse token IDs
                    clob_ids_raw = market.get("clobTokenIds", "[]")
                    try:
                        clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
                    except:
                        continue
                    
                    if not clob_ids:
                        continue
                    
                    # ── Edge analysis: compare YES vs NO, pick the side with real edge ──
                    analysis = analyze_market_data(market)
                    if analysis is None:
                        continue
                    
                    # Strategy proposes a direction from momentum
                    proposed = "YES" if price_change > 0 else "NO"
                    tradeable, final_side, edge, token_idx = validate_proposed_side(analysis, proposed)
                    
                    if not tradeable or token_idx < 0:
                        continue  # No edge on either side after costs
                    if final_side == "NO" and len(clob_ids) < 2:
                        continue
                    
                    token_id = clob_ids[token_idx]
                    price = analysis.yes_ask if final_side == "YES" else analysis.no_ask
                    side = "BUY"
                    
                    if not token_id or price <= 0:
                        continue
                    
                    # Confidence = momentum strength + edge magnitude + volume + cheap price boost
                    momentum_strength = min(abs(price_change) / Decimal("0.08"), Decimal("1"))
                    volume_score = min(volume / Decimal("8000"), Decimal("1"))
                    edge_boost = min(float(edge) * 10, 1.0)  # scale 10% edge → 1.0
                    # Cheap entries get confidence boost (proven by historical data)
                    price_boost = 0.20 if price < Decimal("0.10") else (0.12 if price < Decimal("0.25") else 0.05)
                    confidence = float(momentum_strength * Decimal("0.30") + volume_score * Decimal("0.20")) + edge_boost * 0.35 + price_boost
                    
                    signal = MomentumSignal(
                        token_id=token_id,
                        market_id=market_id,
                        market_question=market.get("question", "")[:100],
                        side=side,
                        current_price=price,
                        price_change_1h=price_change,
                        volume_24h=volume,
                        confidence=confidence,
                        rationale=(
                            f"{'📈' if price_change > 0 else '📉'} {price_change:+.1%} 1h, "
                            f"vol ${volume:.0f} | {final_side} edge +{edge:.1%} | "
                            f"{analysis.reason}"
                        ),
                    )
                    signals.append(signal)
                    
                    if len(signals) >= max_opportunities * 2:
                        break

        except Exception as e:
            logger.error(f"Error finding momentum opportunities: {e}")

        # Sort by confidence
        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals[:max_opportunities]

    async def find_mispriced_markets(
        self,
        max_opportunities: int = 5,
    ) -> list[MomentumSignal]:
        """Find markets where YES/NO pricing is inconsistent.

        Uses the edge engine to compare both sides of every market.
        Picks markets where one side has genuine edge after spread.
        """
        signals = []

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{GAMMA_API_BASE}/markets",
                    params={"active": "true", "closed": "false", "limit": 100},
                    timeout=30.0,
                )
                response.raise_for_status()
                markets = response.json()

                for market in markets:
                    market_id = market.get("conditionId", market.get("id", ""))
                    if market_id in self.recent_trades:
                        continue

                    # Edge analysis — compare YES vs NO
                    analysis = analyze_market_data(market)
                    if analysis is None or analysis.best_side == "PASS":
                        continue

                    # Need volume for exit liquidity
                    volume = Decimal(str(market.get("volume24hr", 0)))
                    if volume < Decimal("1000"):
                        continue

                    clob_ids_raw = market.get("clobTokenIds", "[]")
                    try:
                        clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
                    except Exception:
                        continue
                    if not clob_ids:
                        continue
                    if analysis.best_side == "NO" and len(clob_ids) < 2:
                        continue

                    token_id = clob_ids[analysis.best_token_idx]
                    price = analysis.best_price

                    signals.append(MomentumSignal(
                        token_id=token_id,
                        market_id=market_id,
                        market_question=market.get("question", "")[:100],
                        side="BUY",
                        current_price=price,
                        price_change_1h=Decimal("0"),
                        volume_24h=volume,
                        confidence=min(0.75, 0.45 + float(analysis.best_edge) * 5),
                        rationale=(
                            f"🎯 MISPRICED: {analysis.best_side} edge "
                            f"+{analysis.best_edge:.1%} | {analysis.reason}"
                        ),
                    ))

                    if len(signals) >= max_opportunities:
                        break

        except Exception as e:
            logger.error(f"Error finding mispriced markets: {e}")

        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals[:max_opportunities]
    
    def record_trade(self, market_id: str):
        """Record that we've traded this market to avoid duplicates."""
        self.recent_trades.append(market_id)
        # Keep only last 20 trades
        if len(self.recent_trades) > 20:
            self.recent_trades = self.recent_trades[-20:]
