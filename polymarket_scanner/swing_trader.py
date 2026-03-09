"""Swing / Scalp Strategy — Profit from Price Movement, Not Resolution.

This is the KEY insight:  On Polymarket you can BUY shares at $X and SELL
them later at $X+Δ without waiting for the market to resolve.  If you buy
YES at $0.40 and sell at $0.44, that's a +10% return in hours or days.

HOW IT WORKS:
1. Scan for markets with CLEAR short-term momentum (1h price change)
2. Buy the side that's trending, with a TIGHT take-profit (+5-8%)
3. Sell BEFORE the momentum fades — we don't care about the outcome
4. Cut losses quickly if the trade goes against us (-5% stop-loss)

EDGE vs HOLDING TO RESOLUTION:
- Hold to resolution:  Profit = $1.00 - entry_price  (if you're right)
                       Loss   = entry_price          (if you're wrong)
                       Time   = days/weeks/months
- Swing trade:         Profit = Δprice × shares      (just need movement)
                       Loss   = smaller (tight stop)
                       Time   = hours to 1-2 days

WHAT MAKES A GOOD SWING TARGET:
- Active market (volume > $5k/24h — enough liquidity to sell)
- Price between $0.15 and $0.85 (mid-range = room to move both ways)
- 1h price change > 1% (showing momentum)
- Not in final 24h before resolution (don't get squeezed)

DIFFERENT SWING MODES:
1. MOMENTUM_SCALP:  Buy trending → sell on 5-8% gain
2. DIP_SCALP:       Buy sharp drops → sell on 3-5% bounce
3. RANGE_SCALP:     Buy near support, sell near resistance
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

import httpx

from .config import GAMMA_API_BASE
from .trading_config import CLOB_HOST
from .edge import analyze_market_data, validate_proposed_side, MAX_SPREAD_SCALP

logger = logging.getLogger(__name__)


@dataclass
class SwingSignal:
    """A swing/scalp trading opportunity."""
    token_id: str
    market_id: str
    market_question: str
    side: str
    current_price: Decimal
    target_price: Decimal       # where we expect to sell
    stop_price: Decimal         # where we cut the loss
    edge_estimate: Decimal
    confidence: float
    mode: str                   # MOMENTUM_SCALP | DIP_SCALP | RANGE_SCALP
    rationale: str
    volume_24h: Decimal = Decimal("0")
    liquidity_score: float = 0  # 0-1, how easy to exit

    @property
    def strategy(self) -> str:
        return "SWING"

    @property
    def reward_risk_ratio(self) -> float:
        """How much upside vs downside.  Want >= 1.5."""
        upside = float(self.target_price - self.current_price)
        downside = float(self.current_price - self.stop_price)
        if downside <= 0:
            return 0
        return upside / downside

    @property
    def is_actionable(self) -> bool:
        return (
            self.confidence >= 0.45
            and self.edge_estimate >= Decimal("0.03")
            and self.reward_risk_ratio >= 1.1
            and self.liquidity_score >= 0.2
        )


class SwingTrader:
    """Scans for quick-flip opportunities on Polymarket.

    Instead of betting on outcomes, we bet on PRICE MOVEMENT.
    Buy low → sell slightly higher → pocket the difference.
    """

    def __init__(self):
        self.recent_trades: list[str] = []  # token_ids to avoid re-entry

    async def find_swing_opportunities(
        self,
        max_signals: int = 5,
    ) -> list[SwingSignal]:
        """Run all swing scan modes and return ranked opportunities."""
        all_signals: list[SwingSignal] = []

        try:
            results = await asyncio.gather(
                self._scan_momentum_scalps(),
                self._scan_dip_scalps(),
                self._scan_range_scalps(),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, list):
                    all_signals.extend(result)
                elif isinstance(result, Exception):
                    logger.error(f"Swing scan error: {result}")
        except Exception as e:
            logger.error(f"SwingTrader error: {e}")

        # Dedup
        seen = set()
        unique = []
        for sig in all_signals:
            if sig.token_id not in seen and sig.token_id not in self.recent_trades:
                seen.add(sig.token_id)
                unique.append(sig)

        # Rank by: reward/risk ratio × confidence × liquidity
        unique.sort(
            key=lambda s: s.reward_risk_ratio * s.confidence * s.liquidity_score,
            reverse=True,
        )
        return unique[:max_signals]

    # ------------------------------------------------------------------
    # Mode 1: MOMENTUM SCALP
    # Buy into strong short-term trends, sell for quick profit
    # ------------------------------------------------------------------
    async def _scan_momentum_scalps(self) -> list[SwingSignal]:
        """Find markets with clear 1h momentum and enough volume to exit."""
        signals = []
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{GAMMA_API_BASE}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": 100,
                        "order": "volume24hr",
                        "ascending": "false",
                    },
                    timeout=30.0,
                )
                resp.raise_for_status()
                markets = resp.json()

                for market in markets:
                    sig = self._evaluate_momentum_scalp(market)
                    if sig:
                        signals.append(sig)

        except Exception as e:
            logger.error(f"Momentum scalp scan error: {e}")

        return signals[:5]

    def _evaluate_momentum_scalp(self, market: dict) -> Optional[SwingSignal]:
        """Check if a market is a good momentum scalp target."""
        one_hour_change = market.get("oneHourPriceChange")
        volume_24h = Decimal(str(market.get("volume24hr", 0)))
        best_ask = market.get("bestAsk")
        best_bid = market.get("bestBid")
        volume_num = market.get("volumeNum", 0)

        if one_hour_change is None or not best_ask or not best_bid:
            return None

        price_change = Decimal(str(one_hour_change))
        yes_price = Decimal(str(best_ask))

        # Filters — relaxed to find more opportunities
        if abs(price_change) < Decimal("0.02"):
            return None
        if volume_24h < Decimal("5000") or (volume_num and int(volume_num) < 50):
            return None
        if yes_price < Decimal("0.08") or yes_price > Decimal("0.90"):
            return None

        # Edge analysis — spread gate + side validation
        analysis = analyze_market_data(market)
        if analysis is None or analysis.spread > MAX_SPREAD_SCALP:
            return None  # spread too wide to scalp profitably

        clob_ids = self._parse_clob_ids(market)
        if not clob_ids:
            return None

        market_id = market.get("conditionId", market.get("id", ""))
        liquidity = self._calc_liquidity_score(volume_24h, volume_num)

        # Propose side from momentum, validate with edge
        proposed = "YES" if price_change > 0 else "NO"
        tradeable, final_side, edge, tidx = validate_proposed_side(analysis, proposed)
        if not tradeable or tidx < 0:
            return None
        if final_side == "NO" and len(clob_ids) < 2:
            return None

        token_id = clob_ids[tidx]
        entry = analysis.yes_ask if final_side == "YES" else analysis.no_ask
        target = entry * (Decimal("1") + Decimal("0.06"))  # +6% target
        stop = entry * (Decimal("1") - Decimal("0.04"))    # -4% stop
        scalp_edge = (target - entry) / entry
        rationale = (
            f"MOMENTUM SCALP: {final_side} {price_change:+.1%}/1h, "
            f"edge +{edge:.1%}, spread {analysis.spread:.1%}, "
            f"${entry:.3f} → ${target:.3f} (vol ${volume_24h:,.0f})"
        )

        return SwingSignal(
            token_id=token_id,
            market_id=market_id,
            market_question=market.get("question", "")[:100],
            side="BUY",
            current_price=entry,
            target_price=target,
            stop_price=stop,
            edge_estimate=scalp_edge,
            confidence=min(0.85, 0.50 + float(abs(price_change)) * 3 + float(edge) * 2),
            mode="MOMENTUM_SCALP",
            rationale=rationale,
            volume_24h=volume_24h,
            liquidity_score=liquidity,
        )
    # ------------------------------------------------------------------
    # Mode 2: DIP SCALP
    # Buy sharp drops, sell on the bounce
    # ------------------------------------------------------------------
    async def _scan_dip_scalps(self) -> list[SwingSignal]:
        """Find markets that just dropped hard and should bounce."""
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
                    sig = self._evaluate_dip_scalp(market)
                    if sig:
                        signals.append(sig)

        except Exception as e:
            logger.error(f"Dip scalp scan error: {e}")

        return signals[:4]

    def _evaluate_dip_scalp(self, market: dict) -> Optional[SwingSignal]:
        """Check if a market dipped and is likely to bounce."""
        one_hour_change = market.get("oneHourPriceChange")
        volume_24h = Decimal(str(market.get("volume24hr", 0)))
        best_ask = market.get("bestAsk")
        best_bid = market.get("bestBid")
        volume_num = market.get("volumeNum", 0)

        if one_hour_change is None or not best_ask or not best_bid:
            return None

        price_change = Decimal(str(one_hour_change))

        # Need sharp drop (>2%) — these often bounce
        if price_change > Decimal("-0.02"):
            return None
        # But not a total collapse (>20% = probably news, don't fight it)
        if price_change < Decimal("-0.20"):
            return None
        # Need liquidity
        if volume_24h < Decimal("5000"):
            return None

        yes_price = Decimal(str(best_ask))
        if yes_price < Decimal("0.08") or yes_price > Decimal("0.90"):
            return None

        # Edge analysis — spread gate
        analysis = analyze_market_data(market)
        if analysis is None or analysis.spread > MAX_SPREAD_SCALP:
            return None

        clob_ids = self._parse_clob_ids(market)
        if not clob_ids:
            return None

        market_id = market.get("conditionId", market.get("id", ""))
        liquidity = self._calc_liquidity_score(volume_24h, volume_num)

        # Dip = buy YES (we expect a bounce).  Validate edge.
        tradeable, final_side, edge, tidx = validate_proposed_side(analysis, "YES")
        if not tradeable or tidx < 0:
            return None
        if final_side == "NO" and len(clob_ids) < 2:
            return None

        token_id = clob_ids[tidx]
        entry = analysis.yes_ask if final_side == "YES" else analysis.no_ask
        target = entry * (Decimal("1") + Decimal("0.05"))
        stop = entry * (Decimal("1") - Decimal("0.04"))
        scalp_edge = (target - entry) / entry

        return SwingSignal(
            token_id=token_id,
            market_id=market_id,
            market_question=market.get("question", "")[:100],
            side="BUY",
            current_price=entry,
            target_price=target,
            stop_price=stop,
            edge_estimate=scalp_edge,
            confidence=min(0.80, 0.45 + float(abs(price_change)) * 3 + float(edge) * 2),
            mode="DIP_SCALP",
            rationale=(
                f"DIP SCALP: Dropped {price_change:+.1%}/1h, {final_side} at ${entry:.3f} "
                f"→ ${target:.3f}, edge +{edge:.1%} (vol ${volume_24h:,.0f})"
            ),
            volume_24h=volume_24h,
            liquidity_score=liquidity,
        )

    # ------------------------------------------------------------------
    # Mode 3: RANGE SCALP
    # Buy near the bottom of a price range, sell near the top
    # ------------------------------------------------------------------
    async def _scan_range_scalps(self) -> list[SwingSignal]:
        """Find stable markets oscillating in a range — buy low, sell high within range."""
        signals = []
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{GAMMA_API_BASE}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": 100,
                    },
                    timeout=30.0,
                )
                resp.raise_for_status()
                markets = resp.json()

                for market in markets:
                    sig = self._evaluate_range_scalp(market)
                    if sig:
                        signals.append(sig)

        except Exception as e:
            logger.error(f"Range scalp scan error: {e}")

        return signals[:3]

    def _evaluate_range_scalp(self, market: dict) -> Optional[SwingSignal]:
        """Check if a market is range-bound and near support."""
        volume_24h = Decimal(str(market.get("volume24hr", 0)))
        one_hour_change = market.get("oneHourPriceChange")
        best_ask = market.get("bestAsk")
        best_bid = market.get("bestBid")
        volume_num = market.get("volumeNum", 0)

        if not best_ask or not best_bid or one_hour_change is None:
            return None

        price_change = abs(Decimal(str(one_hour_change)))
        yes_price = Decimal(str(best_ask))
        bid_price = Decimal(str(best_bid))

        # For range trading, we want STABLE markets (small 1h change)
        if price_change > Decimal("0.03"):
            return None
        # Need decent volume (active market)
        if volume_24h < Decimal("8000"):
            return None
        # Price in sweet spot for range trading
        if yes_price < Decimal("0.10") or yes_price > Decimal("0.70"):
            return None

        # Spread check: tighter spread = better for scalping
        spread = yes_price - bid_price
        if spread > Decimal("0.07"):
            return None  # too wide, slippage will kill profits

        # Edge analysis — extra spread validation
        analysis = analyze_market_data(market)
        if analysis is None or analysis.spread > Decimal("0.07"):
            return None

        clob_ids = self._parse_clob_ids(market)
        if not clob_ids:
            return None

        market_id = market.get("conditionId", market.get("id", ""))
        liquidity = self._calc_liquidity_score(volume_24h, volume_num)

        # Range: buy whichever side the edge engine recommends
        if analysis.best_side == "PASS":
            return None
        if analysis.best_side == "NO" and len(clob_ids) < 2:
            return None

        tidx = analysis.best_token_idx
        token_id = clob_ids[tidx]
        entry = analysis.best_price
        target = entry + Decimal("0.03")  # $0.03 absolute move
        stop = entry - Decimal("0.02")    # $0.02 stop
        edge = (target - entry) / entry

        return SwingSignal(
            token_id=token_id,
            market_id=market_id,
            market_question=market.get("question", "")[:100],
            side="BUY",
            current_price=entry,
            target_price=target,
            stop_price=stop,
            edge_estimate=edge,
            confidence=min(0.80, 0.60 + float(analysis.best_edge) * 2),
            mode="RANGE_SCALP",
            rationale=(
                f"RANGE SCALP: {analysis.best_side} edge +{analysis.best_edge:.1%}, "
                f"spread ${spread:.3f}, ${entry:.3f} → ${target:.3f} "
                f"(vol ${volume_24h:,.0f})"
            ),
            volume_24h=volume_24h,
            liquidity_score=liquidity,
        )

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

    @staticmethod
    def _calc_liquidity_score(volume_24h: Decimal, volume_num) -> float:
        """Score how easy it will be to exit this position.
        0 = no liquidity, 1 = very liquid.
        """
        vol_score = min(1.0, float(volume_24h) / 100000)
        trade_score = min(1.0, int(volume_num or 0) / 500)
        return (vol_score * 0.6 + trade_score * 0.4)

    def record_trade(self, token_id: str):
        """Mark token as recently traded to avoid immediate re-entry."""
        self.recent_trades.append(token_id)
        if len(self.recent_trades) > 50:
            self.recent_trades = self.recent_trades[-50:]
