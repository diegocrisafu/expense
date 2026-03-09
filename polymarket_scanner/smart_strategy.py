"""Smart directional strategies — beyond simple momentum.

Implements:
1. Mean Reversion / Contrarian: Buy the dip when price drops sharply
   then stabilises (oversold bounce). Sell when price spikes unsustainably.
2. Correlation / Related-Market logic: If "Will X happen by June?"
   trades at $0.40 but "Will X happen by December?" trades at $0.35,
   the June contract is overpriced (or December is underpriced).
3. Volatility-Weighted Entry: Larger bets on low-volatility edges,
   smaller bets on high-volatility ones.
4. News / Volume Spike Detection: Unusual volume without proportional
   price movement signals informed traders accumulating.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

import httpx

from .config import GAMMA_API_BASE
from .trading_config import SIGNAL_BET_SIZE, CLOB_HOST
from .edge import (
    analyze_market_data,
    analyze_binary_market,
    analyze_event,
    validate_proposed_side,
    MAX_SPREAD_SCALP,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class SmartSignal:
    """A directional signal from an advanced strategy."""
    token_id: str
    market_id: str
    market_question: str
    side: str               # BUY or SELL
    current_price: Decimal
    target_price: Decimal   # where we think the price should be
    edge_estimate: Decimal  # expected profit %
    confidence: float       # 0-1
    strategy: str           # MEAN_REVERSION | CORRELATED | VOL_SPIKE | CONTRARIAN
    rationale: str
    volatility: Decimal = Decimal("0")  # recent price σ
    suggested_size: Decimal = SIGNAL_BET_SIZE

    @property
    def is_actionable(self) -> bool:
        # Structural plays (correlated mispricings) are inherently high-confidence
        # because the edge is mathematical, not directional.
        if self.edge_estimate >= Decimal("1.0"):
            return self.confidence >= 0.50 and self.current_price <= Decimal("0.55")
        elif self.edge_estimate >= Decimal("0.15"):
            return self.confidence >= 0.55 and self.current_price <= Decimal("0.55")
        elif self.edge_estimate >= Decimal("0.05"):
            return self.confidence >= 0.60 and self.current_price <= Decimal("0.55")
        else:
            return False  # edge too small to justify the trade


# ---------------------------------------------------------------------------
# Strategy engine
# ---------------------------------------------------------------------------
class SmartStrategy:
    """Runs multiple directional strategies and returns ranked signals."""

    def __init__(self):
        self.recent_signals: list[str] = []  # dedup

    async def generate_all_signals(self, max_signals: int = 5) -> list[SmartSignal]:
        """Run all smart strategies and merge their output."""
        all_signals: list[SmartSignal] = []

        try:
            # Run strategies concurrently
            results = await asyncio.gather(
                self.find_mean_reversion(),
                self.find_volume_spikes(),
                self.find_correlated_mispricings(),
                self.find_event_mispricings(),
                return_exceptions=True,
            )

            for result in results:
                if isinstance(result, list):
                    all_signals.extend(result)
                elif isinstance(result, Exception):
                    logger.error(f"Strategy error: {result}")

        except Exception as e:
            logger.error(f"SmartStrategy error: {e}")

        # Deduplicate by token_id
        seen = set()
        unique = []
        for sig in all_signals:
            if sig.token_id not in seen and sig.token_id not in self.recent_signals:
                seen.add(sig.token_id)
                unique.append(sig)

        # Sort by edge * confidence (expected value)
        unique.sort(
            key=lambda s: float(s.edge_estimate) * s.confidence,
            reverse=True,
        )
        return unique[:max_signals]

    # ------------------------------------------------------------------
    # Strategy 1: Mean Reversion / Contrarian
    # ------------------------------------------------------------------
    async def find_mean_reversion(self) -> list[SmartSignal]:
        """Find markets where price dropped sharply and may bounce.

        Logic:
        - Price fell >5% in 1h but volume stayed moderate → panic sell, buy the dip
        - Price spiked >8% on low volume → likely to revert, sell / buy the other side
        """
        signals = []
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{GAMMA_API_BASE}/markets",
                    params={"active": "true", "closed": "false", "limit": 100},
                    timeout=30.0,
                )
                resp.raise_for_status()
                markets = resp.json()

                for market in markets:
                    one_hour_change = market.get("oneHourPriceChange")
                    volume_24h = Decimal(str(market.get("volume24hr", 0)))
                    if one_hour_change is None:
                        continue

                    price_change = Decimal(str(one_hour_change))
                    best_ask = market.get("bestAsk")
                    best_bid = market.get("bestBid")
                    if not best_ask or not best_bid:
                        continue

                    yes_price = Decimal(str(best_ask))
                    no_price = Decimal("1") - Decimal(str(best_bid))

                    clob_ids = self._parse_clob_ids(market)
                    if not clob_ids:
                        continue

                    market_id = market.get("conditionId", market.get("id", ""))

                    # --- Dip buying (contrarian) ---
                    # Price dropped >5% in 1h, volume not exploding (no structural news)
                    if price_change < Decimal("-0.05") and volume_24h < Decimal("50000"):
                        # Edge check: is YES actually underpriced after the drop?
                        analysis = analyze_market_data(market)
                        if analysis is None:
                            continue
                        tradeable, final_side, edge, tidx = validate_proposed_side(analysis, "YES")
                        if not tradeable:
                            continue

                        if final_side == "YES":
                            target = yes_price * Decimal("1.08")
                            token_id = clob_ids[0]
                            price = analysis.yes_ask
                        else:
                            if len(clob_ids) < 2:
                                continue
                            target = analysis.no_ask * Decimal("1.08")
                            token_id = clob_ids[1]
                            price = analysis.no_ask

                        if edge > Decimal("0.01"):
                            signals.append(SmartSignal(
                                token_id=token_id,
                                market_id=market_id,
                                market_question=market.get("question", "")[:100],
                                side="BUY",
                                current_price=price,
                                target_price=target,
                                edge_estimate=edge,
                                confidence=min(0.85, 0.55 + float(abs(price_change)) * 2 + float(edge) * 2),
                                strategy="CONTRARIAN",
                                rationale=(
                                    f"CONTRARIAN: Dropped {price_change:+.1%}/1h, "
                                    f"vol ${volume_24h:.0f} — {final_side} edge +{edge:.1%}"
                                ),
                            ))

                    # --- Spike selling (fade the rally) ---
                    if price_change > Decimal("0.08") and volume_24h < Decimal("30000"):
                        # Edge check: is NO underpriced after the spike?
                        analysis = analyze_market_data(market)
                        if analysis is None:
                            continue
                        tradeable, final_side, edge, tidx = validate_proposed_side(analysis, "NO")
                        if not tradeable:
                            continue
                        if final_side == "NO" and len(clob_ids) < 2:
                            continue

                        token_id = clob_ids[tidx]
                        price = analysis.no_ask if final_side == "NO" else analysis.yes_ask
                        target = price * Decimal("1.10")

                        if edge > Decimal("0.01") and price > Decimal("0.05"):
                            signals.append(SmartSignal(
                                token_id=token_id,
                                market_id=market_id,
                                market_question=market.get("question", "")[:100],
                                side="BUY",
                                current_price=price,
                                target_price=target,
                                edge_estimate=edge,
                                confidence=min(0.80, 0.50 + float(price_change) + float(edge) * 2),
                                strategy="CONTRARIAN",
                                rationale=(
                                    f"FADE RALLY: Spiked {price_change:+.1%} low vol — "
                                    f"{final_side} @ ${price:.3f} edge +{edge:.1%}"
                                ),
                                ))

        except Exception as e:
            logger.error(f"Mean reversion scan error: {e}")

        return signals

    # ------------------------------------------------------------------
    # Strategy 2: Volume Spike Detection (accumulation)
    # ------------------------------------------------------------------
    async def find_volume_spikes(self) -> list[SmartSignal]:
        """Find markets with unusual volume but flat price → informed accumulation.

        Logic: If volume surges 3x+ typical but price barely moved,
        smart money is accumulating quietly. Follow them.
        """
        signals = []
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{GAMMA_API_BASE}/markets",
                    params={"active": "true", "closed": "false", "limit": 100},
                    timeout=30.0,
                )
                resp.raise_for_status()
                markets = resp.json()

                for market in markets:
                    one_hour_change = market.get("oneHourPriceChange")
                    volume_24h = Decimal(str(market.get("volume24hr", 0)))
                    volume_num = market.get("volumeNum", 0)  # number of trades
                    if one_hour_change is None:
                        continue

                    price_change = abs(Decimal(str(one_hour_change)))
                    best_ask = market.get("bestAsk")
                    best_bid = market.get("bestBid")
                    if not best_ask or not best_bid:
                        continue

                    yes_price = Decimal(str(best_ask))
                    clob_ids = self._parse_clob_ids(market)
                    if not clob_ids:
                        continue

                    market_id = market.get("conditionId", market.get("id", ""))

                    # High trade count (many trades) but small price move
                    # This signals accumulation by informed traders
                    if (
                        volume_num and int(volume_num) > 100 and
                        volume_24h > Decimal("5000") and
                        price_change < Decimal("0.02")  # less than 2% move
                    ):
                        # Edge check: which side is being accumulated?
                        analysis = analyze_market_data(market)
                        if analysis is None:
                            continue

                        actual_change = Decimal(str(one_hour_change))
                        proposed = "YES" if actual_change >= 0 else "NO"
                        tradeable, final_side, edge, tidx = validate_proposed_side(analysis, proposed)
                        if not tradeable:
                            continue
                        if final_side == "NO" and len(clob_ids) < 2:
                            continue

                        token_id = clob_ids[tidx]
                        price = analysis.yes_ask if final_side == "YES" else analysis.no_ask

                        vol_conf = min(0.85, 0.55 + float(edge) * 3 + int(volume_num) / 2000)
                        signals.append(SmartSignal(
                            token_id=token_id,
                            market_id=market_id,
                            market_question=market.get("question", "")[:100],
                            side="BUY",
                            current_price=price,
                            target_price=price * Decimal("1.05"),
                            edge_estimate=edge,
                            confidence=vol_conf,
                            strategy="MOMENTUM",
                            rationale=(
                                f"ACCUMULATION: {volume_num} trades, vol ${volume_24h:.0f}, "
                                f"price {actual_change:+.1%} — {final_side} edge +{edge:.1%}"
                            ),
                        ))

        except Exception as e:
            logger.error(f"Volume spike scan error: {e}")

        return signals[:5]

    # ------------------------------------------------------------------
    # Strategy 3: Correlated / Related Market Mispricings
    # ------------------------------------------------------------------
    async def find_correlated_mispricings(self) -> list[SmartSignal]:
        """Find related markets with inconsistent pricing.

        Logic:
        - Markets about the same topic with different deadlines should be
          monotonically priced (shorter deadline = lower probability).
        - If "X by March" = $0.60 and "X by December" = $0.50, buy December
          (it must be >= March's probability).
        - Also checks for near-identical questions with divergent prices.
        """
        signals = []
        try:
            async with httpx.AsyncClient() as client:
                # Get events (groups of related markets)
                resp = await client.get(
                    f"{GAMMA_API_BASE}/events",
                    params={"active": "true", "closed": "false", "limit": 50},
                    timeout=30.0,
                )
                resp.raise_for_status()
                events = resp.json()

                for event in events:
                    event_markets = event.get("markets", [])
                    if len(event_markets) < 2:
                        continue

                    # Sort markets by end date if available
                    dated_markets = []
                    for m in event_markets:
                        end_date = m.get("endDate") or m.get("endDateIso")
                        best_ask = m.get("bestAsk")
                        if end_date and best_ask:
                            dated_markets.append({
                                "market": m,
                                "end_date": end_date,
                                "price": Decimal(str(best_ask)),
                            })

                    if len(dated_markets) < 2:
                        continue

                    # Sort by end date
                    dated_markets.sort(key=lambda x: x["end_date"])

                    # Check monotonicity: later deadline should have >= probability
                    for i in range(len(dated_markets) - 1):
                        earlier = dated_markets[i]
                        later = dated_markets[i + 1]

                        if later["price"] < earlier["price"] - Decimal("0.05"):
                            # Later deadline is cheaper → mispriced (should be >=)
                            m = later["market"]
                            clob_ids = self._parse_clob_ids(m)
                            if not clob_ids:
                                continue

                            market_id = m.get("conditionId", m.get("id", ""))
                            edge = earlier["price"] - later["price"]

                            rel_edge = edge / later["price"] if later["price"] > 0 else Decimal("0")
                            # Confidence scales with edge magnitude + cheap entry.
                            # Monotonicity violations are STRUCTURAL — they represent
                            # mathematical mispricings, not directional bets.
                            # 10% edge → 0.80, 50% edge → 0.90, 100%+ edge → 0.95
                            conf = min(0.95, 0.75 + float(min(rel_edge, Decimal("5"))) * 0.04)
                            # Cheaper entries are safer — boost confidence for < $0.15
                            if later["price"] < Decimal("0.15"):
                                conf = min(0.95, conf + 0.05)

                            signals.append(SmartSignal(
                                token_id=clob_ids[0],
                                market_id=market_id,
                                market_question=m.get("question", "")[:100],
                                side="BUY",
                                current_price=later["price"],
                                target_price=earlier["price"],  # should converge
                                edge_estimate=rel_edge,
                                confidence=conf,
                                strategy="CORRELATED",
                                rationale=(
                                    f"CORRELATED: Later deadline @ ${later['price']:.2f} < "
                                    f"earlier @ ${earlier['price']:.2f} — buying the gap"
                                ),
                            ))

        except Exception as e:
            logger.error(f"Correlation scan error: {e}")

        return signals[:5]

    # ------------------------------------------------------------------
    # Strategy 4: Multi-Outcome Event Mispricings
    # ------------------------------------------------------------------
    async def find_event_mispricings(self) -> list[SmartSignal]:
        """Find mispriced outcomes inside multi-outcome events.

        For EXCLUSIVE events (e.g., "Who will win?"):
          All YES probs should sum to ~1.0.  If an outcome's YES is
          overpriced vs the normalized fair value, buy its NO.

        For NON-EXCLUSIVE events (e.g., "Who will perform at halftime?"):
          Each contract is independent — compare YES vs NO for each.
          A 99 % YES does NOT mean NO is 1 %.  After calibration the
          true NO probability could be 2-4 % → a cheap lottery ticket.
        """
        signals = []
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{GAMMA_API_BASE}/events",
                    params={"active": "true", "closed": "false", "limit": 50},
                    timeout=30.0,
                )
                resp.raise_for_status()
                events = resp.json()

                for event in events:
                    event_edge = analyze_event(event)
                    if event_edge is None or not event_edge.mispricings:
                        continue

                    for mp in event_edge.mispricings[:2]:  # top-2 per event
                        # Get CLOB token IDs for this market
                        market_dict = next(
                            (m for m in event.get("markets", [])
                             if m.get("conditionId", m.get("id", "")) == mp.market_id),
                            None,
                        )
                        if market_dict is None:
                            continue
                        clob_ids = self._parse_clob_ids(market_dict)
                        if not clob_ids:
                            continue
                        if mp.side == "NO" and len(clob_ids) < 2:
                            continue

                        token_id = clob_ids[mp.token_idx]
                        price = mp.yes_price if mp.side == "YES" else (Decimal("1") - mp.yes_price)
                        if price <= Decimal("0"):
                            continue

                        excl = "EXCL" if event_edge.is_exclusive else "INDEP"
                        # Scale confidence with edge and cheap price
                        # Event-level structural mispricings are reliable
                        evt_conf = min(0.95, 0.70 + float(mp.edge) * 3)
                        if price < Decimal("0.15"):
                            evt_conf = min(0.95, evt_conf + 0.05)

                        signals.append(SmartSignal(
                            token_id=token_id,
                            market_id=mp.market_id,
                            market_question=mp.question,
                            side="BUY",
                            current_price=price,
                            target_price=mp.fair_price if mp.side == "YES" else (Decimal("1") - mp.fair_price),
                            edge_estimate=mp.edge,
                            confidence=evt_conf,
                            strategy="CORRELATED",
                            rationale=(
                                f"EVENT [{excl}]: {event_edge.event_title[:40]} | "
                                f"{mp.side} edge +{mp.edge:.1%} | "
                                f"sum={event_edge.total_yes_prob:.0%} "
                                f"overround={event_edge.overround:+.1%}"
                            ),
                        ))

        except Exception as e:
            logger.error(f"Event mispricing scan error: {e}")

        return signals[:6]

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_clob_ids(market: dict) -> list[str]:
        raw = market.get("clobTokenIds", "[]")
        try:
            ids = json.loads(raw) if isinstance(raw, str) else raw
            return ids if isinstance(ids, list) else []
        except Exception:
            return []

    def record_signal(self, token_id: str):
        """Mark a token as recently signalled to avoid duplicates."""
        self.recent_signals.append(token_id)
        if len(self.recent_signals) > 30:
            self.recent_signals = self.recent_signals[-30:]
