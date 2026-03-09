"""Adaptive Quant Engine — Real-time Learning & Data-Driven Decisions.

This is Roger's BRAIN.  Every other module asks the quant engine:
  "Should I take this trade?"   → score_opportunity()
  "How much should I bet?"      → optimal_size()
  "Is this strategy working?"   → strategy_health()
  "What's the real edge here?"  → bayesian_edge()

HOW IT LEARNS:
 1. Every trade records 12 features (spread, volume, momentum, edge, etc.)
 2. On every exit, the engine updates Bayesian priors for each feature
 3. Features that predicted wins get UP-weighted; losers get DOWN-weighted
 4. Strategy confidence decay — stale strategies lose trust exponentially
 5. Market regime detection — volatile vs calm, adjusts thresholds
 6. Calibration tracking — when we say "70% confident", are we winning 70%?

SPEED:  O(1) scoring (lookup in ~10 feature buckets), O(1) per update.
         No ML, no numpy.  Pure Bayesian counting + exponential moving averages.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from .database import get_connection, DB_PATH

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Feature buckets — discretize continuous features into bins
# so we can count wins/losses per bin.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Spread buckets: [0-1%, 1-3%, 3-5%, 5-8%, 8%+]
SPREAD_BINS = [0.01, 0.03, 0.05, 0.08]
# Volume buckets: [0-5k, 5k-20k, 20k-50k, 50k-100k, 100k+]
VOLUME_BINS = [5000, 20000, 50000, 100000]
# Price buckets: [0-10¢, 10-20¢, 20-40¢, 40-60¢, 60-80¢, 80¢+]
PRICE_BINS = [0.10, 0.20, 0.40, 0.60, 0.80]
# Edge buckets: [0-3%, 3-5%, 5-8%, 8-12%, 12%+]
EDGE_BINS = [0.03, 0.05, 0.08, 0.12]
# Momentum buckets: [<-5%, -5 to -2%, -2 to 0%, 0 to 2%, 2 to 5%, 5%+]
MOMENTUM_BINS = [-0.05, -0.02, 0.0, 0.02, 0.05]
# Liquidity score buckets: [0-0.2, 0.2-0.4, 0.4-0.6, 0.6-0.8, 0.8+]
LIQUIDITY_BINS = [0.2, 0.4, 0.6, 0.8]


def _bucket(value: float, bins: list[float]) -> int:
    """Assign a value to a bucket index.  O(n) where n = len(bins) ≤ 6."""
    for i, edge in enumerate(bins):
        if value < edge:
            return i
    return len(bins)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Trade features — what we measure about every opportunity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class TradeFeatures:
    """Features extracted from a trade opportunity for scoring."""
    # Market microstructure
    spread: float           # bid-ask spread as decimal (0.02 = 2%)
    volume_24h: float       # 24h volume in USD
    price: float            # entry price
    liquidity_score: float  # 0-1: how easy to exit

    # Edge signals
    edge: float             # estimated edge from edge.py
    momentum_1h: float      # 1-hour price change
    confidence: float       # strategy's confidence score

    # Context
    strategy: str           # ARB, SWING, MOMENTUM, etc.
    mode: str               # MOMENTUM_SCALP, DIP_SCALP, etc.
    side: str               # YES or NO
    hour_of_day: int        # 0-23 UTC
    day_of_week: int        # 0=Mon, 6=Sun

    def to_bucket_key(self) -> dict[str, int]:
        """Convert features to bucket indices for counting."""
        return {
            "spread": _bucket(self.spread, SPREAD_BINS),
            "volume": _bucket(self.volume_24h, VOLUME_BINS),
            "price": _bucket(self.price, PRICE_BINS),
            "edge": _bucket(self.edge, EDGE_BINS),
            "momentum": _bucket(self.momentum_1h, MOMENTUM_BINS),
            "liquidity": _bucket(self.liquidity_score, LIQUIDITY_BINS),
        }

    def to_dict(self) -> dict:
        """Serialize for DB storage."""
        return {
            "spread": self.spread,
            "volume_24h": self.volume_24h,
            "price": self.price,
            "liquidity_score": self.liquidity_score,
            "edge": self.edge,
            "momentum_1h": self.momentum_1h,
            "confidence": self.confidence,
            "strategy": self.strategy,
            "mode": self.mode,
            "side": self.side,
            "hour_of_day": self.hour_of_day,
            "day_of_week": self.day_of_week,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Bayesian counter — tracks success rate per feature bucket
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class BayesianCounter:
    """Beta distribution tracker for win rate estimation.

    Uses Beta(alpha, beta) conjugate prior:
      alpha = number of successes + prior_alpha
      beta  = number of failures + prior_beta

    Prior: Beta(2, 2) = uniform-ish, slightly pulled toward 50%.
    Mean:  alpha / (alpha + beta)
    Each update shifts the mean toward the observation.

    Time decay: older observations count less (exponential decay on counts).
    """
    alpha: float = 2.0      # prior successes
    beta: float = 2.0       # prior failures
    total: int = 0           # raw count (before decay)
    last_update: float = 0   # timestamp of last update

    @property
    def mean(self) -> float:
        """Estimated win probability."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def samples(self) -> float:
        """Effective sample size."""
        return self.alpha + self.beta - 4.0  # subtract prior

    @property
    def uncertainty(self) -> float:
        """Standard deviation of the estimate.
        Lower = more confident in our estimate."""
        n = self.alpha + self.beta
        if n <= 0:
            return 1.0
        return math.sqrt(self.alpha * self.beta / (n * n * (n + 1)))

    def update(self, won: bool, weight: float = 1.0):
        """Update with a new observation."""
        if won:
            self.alpha += weight
        else:
            self.beta += weight
        self.total += 1
        self.last_update = time.time()

    def apply_decay(self, decay_factor: float = 0.98):
        """Decay old observations toward the prior.

        This makes recent results matter more.
        Called periodically (e.g., every hour or every N trades).
        """
        # Pull toward prior by shrinking counts
        prior_a, prior_b = 2.0, 2.0
        self.alpha = prior_a + (self.alpha - prior_a) * decay_factor
        self.beta = prior_b + (self.beta - prior_b) * decay_factor

    def to_dict(self) -> dict:
        return {
            "alpha": round(self.alpha, 4),
            "beta": round(self.beta, 4),
            "total": self.total,
            "last_update": self.last_update,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BayesianCounter":
        return cls(
            alpha=d.get("alpha", 2.0),
            beta=d.get("beta", 2.0),
            total=d.get("total", 0),
            last_update=d.get("last_update", 0),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Market quality score
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def market_quality_score(
    spread: float,
    volume_24h: float,
    liquidity_score: float,
    book_depth_bids: int = 5,
    book_depth_asks: int = 5,
) -> float:
    """Score market quality from 0 (terrible) to 1 (excellent).

    Components:
      - Spread tightness (40%): < 2% = great, > 8% = terrible
      - Volume (30%): > $50k = great
      - Liquidity score (20%): from swing_trader
      - Book depth (10%): more levels = better

    Markets scoring < 0.3 should be AVOIDED.
    Markets scoring > 0.7 are GOOD for trading.
    """
    # Spread: 0% → 1.0, 10% → 0.0  (linear clamp)
    spread_score = max(0, min(1, 1.0 - spread / 0.10))

    # Volume: log scale, $1k = 0.1, $10k = 0.5, $100k = 0.9, $500k+ = 1.0
    if volume_24h <= 0:
        vol_score = 0.0
    else:
        vol_score = min(1.0, math.log10(max(1, volume_24h)) / 5.7)  # log10(500k) ≈ 5.7

    # Liquidity: passthrough 0-1
    liq_score = max(0, min(1, liquidity_score))

    # Book depth: 1 level = 0.2, 5+ = 1.0
    depth = min(5, (book_depth_bids + book_depth_asks) / 2)
    depth_score = depth / 5.0

    quality = (
        spread_score * 0.40 +
        vol_score * 0.30 +
        liq_score * 0.20 +
        depth_score * 0.10
    )
    return round(quality, 3)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Calibration tracker — are our confidence scores accurate?
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CALIBRATION_BINS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]


@dataclass
class CalibrationTracker:
    """Tracks whether our confidence scores match reality.

    If we say "70% confident", we should win ~70% of the time.
    If we're over-confident, we tighten thresholds.
    If we're under-confident, we can be more aggressive.

    Uses binned tracking: every confidence score maps to a bin,
    and we count wins/total for that bin.
    """
    bins: dict[int, BayesianCounter] = field(default_factory=dict)

    def record(self, confidence: float, won: bool):
        """Record an outcome for a given confidence level."""
        bucket = _bucket(confidence, CALIBRATION_BINS)
        if bucket not in self.bins:
            self.bins[bucket] = BayesianCounter()
        self.bins[bucket].update(won)

    def adjustment_factor(self, confidence: float) -> float:
        """How much to adjust a confidence score based on calibration history.

        Returns a multiplier:
          > 1.0 = we're under-confident (historically win MORE than predicted)
          < 1.0 = we're over-confident (historically win LESS than predicted)
          = 1.0 = well-calibrated or no data

        Example:
          We say 60% confident, but historically win 40% at that level:
          adjustment = 0.40 / 0.60 = 0.67  → multiply confidence by 0.67
        """
        bucket = _bucket(confidence, CALIBRATION_BINS)
        counter = self.bins.get(bucket)
        if counter is None or counter.samples < 3:
            return 1.0  # not enough data

        actual_win_rate = counter.mean
        if confidence <= 0:
            return 1.0

        ratio = actual_win_rate / confidence
        # Clamp to [0.5, 1.5] — don't over-correct
        return max(0.5, min(1.5, ratio))

    def to_dict(self) -> dict:
        return {str(k): v.to_dict() for k, v in self.bins.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "CalibrationTracker":
        tracker = cls()
        for k, v in d.items():
            tracker.bins[int(k)] = BayesianCounter.from_dict(v)
        return tracker


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Strategy health — real-time performance per strategy
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class StrategyHealth:
    """Real-time health metrics for a strategy.

    Uses exponential moving averages (EMA) for speed:
      - Recent win rate (last 10 trades weighted)
      - Recent PnL (EMA of per-trade PnL)
      - Consecutive losses counter
      - Time since last win

    Strategies in bad health get THROTTLED or PAUSED.
    """
    name: str
    ema_win_rate: float = 0.5     # starts neutral
    ema_pnl: float = 0.0          # EMA of per-trade PnL
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    total_trades: int = 0
    total_wins: int = 0
    total_pnl: float = 0.0
    last_trade_time: float = 0
    last_win_time: float = 0
    # Feature-level Bayesian counters
    feature_counters: dict[str, dict[int, BayesianCounter]] = field(default_factory=dict)

    # EMA smoothing factor: 0.3 = recent trades matter a LOT (fast adaptation)
    EMA_ALPHA = 0.3

    def update(self, won: bool, pnl: float, features: TradeFeatures):
        """Update strategy health with a new trade outcome."""
        now = time.time()
        self.total_trades += 1
        self.total_pnl += pnl
        self.last_trade_time = now

        # EMA updates (exponential smoothing)
        win_val = 1.0 if won else 0.0
        self.ema_win_rate = self.EMA_ALPHA * win_val + (1 - self.EMA_ALPHA) * self.ema_win_rate
        self.ema_pnl = self.EMA_ALPHA * pnl + (1 - self.EMA_ALPHA) * self.ema_pnl

        if won:
            self.total_wins += 1
            self.consecutive_wins += 1
            self.consecutive_losses = 0
            self.last_win_time = now
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0

        # Update feature-level counters
        buckets = features.to_bucket_key()
        for feat_name, bucket_idx in buckets.items():
            if feat_name not in self.feature_counters:
                self.feature_counters[feat_name] = {}
            if bucket_idx not in self.feature_counters[feat_name]:
                self.feature_counters[feat_name][bucket_idx] = BayesianCounter()
            self.feature_counters[feat_name][bucket_idx].update(won)

    def is_healthy(self) -> tuple[bool, str]:
        """Check if strategy is healthy enough to trade.

        Returns (healthy, reason).  Unhealthy strategies should be PAUSED.
        """
        if self.total_trades < 3:
            return True, "Not enough data (< 3 trades)"

        # 3+ consecutive losses → pause
        if self.consecutive_losses >= 3:
            return False, f"3+ consecutive losses ({self.consecutive_losses})"

        # Win rate collapsed below 25% (with enough trades)
        if self.total_trades >= 8 and self.ema_win_rate < 0.25:
            return False, f"Win rate collapsed ({self.ema_win_rate:.0%})"

        # Hemorrhaging money — EMA PnL deeply negative
        if self.total_trades >= 5 and self.ema_pnl < -0.10:
            return False, f"Negative EMA PnL ({self.ema_pnl:.3f})"

        return True, "Healthy"

    def throttle_factor(self) -> float:
        """How much to throttle this strategy (0 = fully paused, 1 = full speed).

        Gradual degradation:
          - Good: 100% (win rate > 50%, positive EMA PnL)
          - OK: 75% (win rate 35-50%)
          - Warning: 50% (win rate 25-35% or 2 consecutive losses)
          - Paused: 0% (3+ consecutive losses, or EMA win rate < 25%)
        """
        if self.total_trades < 3:
            return 0.75  # new strategy — run at 75%

        healthy, _ = self.is_healthy()
        if not healthy:
            return 0.0

        if self.ema_win_rate > 0.55 and self.ema_pnl > 0:
            return 1.0
        if self.ema_win_rate > 0.45:
            return 0.85
        if self.ema_win_rate > 0.35:
            return 0.65
        return 0.40

    def feature_win_rate(self, feature_name: str, bucket_idx: int) -> Optional[float]:
        """Get historical win rate for a specific feature bucket.

        Returns None if not enough data.
        """
        bucket_map = self.feature_counters.get(feature_name, {})
        counter = bucket_map.get(bucket_idx)
        if counter is None or counter.samples < 2:
            return None
        return counter.mean

    def to_dict(self) -> dict:
        fc = {}
        for feat, buckets in self.feature_counters.items():
            fc[feat] = {str(k): v.to_dict() for k, v in buckets.items()}
        return {
            "name": self.name,
            "ema_win_rate": round(self.ema_win_rate, 4),
            "ema_pnl": round(self.ema_pnl, 6),
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "total_trades": self.total_trades,
            "total_wins": self.total_wins,
            "total_pnl": round(self.total_pnl, 6),
            "last_trade_time": self.last_trade_time,
            "last_win_time": self.last_win_time,
            "feature_counters": fc,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyHealth":
        health = cls(name=d.get("name", ""))
        health.ema_win_rate = d.get("ema_win_rate", 0.5)
        health.ema_pnl = d.get("ema_pnl", 0.0)
        health.consecutive_losses = d.get("consecutive_losses", 0)
        health.consecutive_wins = d.get("consecutive_wins", 0)
        health.total_trades = d.get("total_trades", 0)
        health.total_wins = d.get("total_wins", 0)
        health.total_pnl = d.get("total_pnl", 0.0)
        health.last_trade_time = d.get("last_trade_time", 0)
        health.last_win_time = d.get("last_win_time", 0)
        fc_raw = d.get("feature_counters", {})
        for feat, buckets in fc_raw.items():
            health.feature_counters[feat] = {
                int(k): BayesianCounter.from_dict(v)
                for k, v in buckets.items()
            }
        return health


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main Engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class TradeScore:
    """Final computed score for a potential trade."""
    total_score: float        # 0-1:  > 0.5 = take it
    adjusted_edge: float      # edge after all adjustments
    adjusted_confidence: float # confidence after calibration
    market_quality: float     # 0-1
    strategy_health: float    # 0-1 (throttle factor)
    feature_score: float      # 0-1 from Bayesian feature analysis
    should_trade: bool        # final verdict
    reason: str               # human-readable explanation
    recommended_size_pct: float  # % of balance to bet (0-0.05)

    def __str__(self) -> str:
        return (
            f"Score={self.total_score:.2f} Edge={self.adjusted_edge:.1%} "
            f"Conf={self.adjusted_confidence:.0%} Quality={self.market_quality:.2f} "
            f"StratHP={self.strategy_health:.0%} FeatScore={self.feature_score:.2f} "
            f"→ {'✅ TRADE' if self.should_trade else '❌ PASS'} "
            f"({self.reason})"
        )


class QuantEngine:
    """Central brain — scores every opportunity, learns from every outcome.

    Usage:
        engine = QuantEngine()
        engine.load_state()  # Load from DB on startup

        # Before a trade:
        features = TradeFeatures(...)
        score = engine.score_opportunity(features)
        if score.should_trade:
            # execute...

        # After a trade closes:
        engine.record_outcome(features, won=True, pnl=0.02)

        engine.save_state()  # Persist to DB
    """

    # Minimum score to approve a trade (lowered from 0.42 — let more trades through)
    MIN_SCORE = 0.32
    # Minimum market quality to trade (lowered from 0.30 — accept thinner markets)
    MIN_MARKET_QUALITY = 0.20

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        self.strategies: dict[str, StrategyHealth] = {}
        self.calibration = CalibrationTracker()
        self.global_counter = BayesianCounter()  # overall win rate
        self._decay_counter = 0
        self._ensure_table()

    def _ensure_table(self):
        """Create DB table for persisting quant state."""
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS quant_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Feature log: stores features for each trade for analysis
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_features (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id INTEGER,
                    features TEXT,
                    score REAL,
                    outcome TEXT,
                    pnl REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    # ─── Scoring ──────────────────────────────────────────────

    def score_opportunity(self, features: TradeFeatures) -> TradeScore:
        """Score a potential trade from 0 to 1.

        Components (weighted):
          1. Edge magnitude          (25%)
          2. Market quality          (25%)
          3. Feature-based win rate  (20%)
          4. Strategy health         (15%)
          5. Calibrated confidence   (15%)

        All components are 0-1, combined as weighted average.
        """
        # 1. Edge score: map [0%, 15%+] → [0, 1]
        edge_score = min(1.0, max(0, features.edge / 0.15))

        # 2. Market quality
        mq = market_quality_score(
            features.spread, features.volume_24h, features.liquidity_score
        )

        # 3. Feature-based win rate (Bayesian)
        feature_score = self._compute_feature_score(features)

        # 4. Strategy health
        health = self._get_health(features.strategy)
        throttle = health.throttle_factor()

        # 5. Calibrated confidence
        cal_adj = self.calibration.adjustment_factor(features.confidence)
        adj_confidence = features.confidence * cal_adj

        # ── Weighted combination ──
        total = (
            edge_score * 0.25 +
            mq * 0.25 +
            feature_score * 0.20 +
            throttle * 0.15 +
            min(1.0, adj_confidence) * 0.15
        )

        # Adjusted edge: scale edge by market quality + feature score
        combined_factor = (mq * 0.5 + feature_score * 0.3 + throttle * 0.2)
        adjusted_edge = features.edge * combined_factor

        # ── Pattern matching: does this trade match historical winners? ──
        pattern_match, pattern_score = self.matches_winning_pattern(features)

        # ── Auto-pause check ──
        should_pause, pause_reason = self.should_auto_pause_strategy(features.strategy)
        if should_pause:
            throttle = 0.0

        # ── Should we trade? ──
        should_trade = (
            total >= self.MIN_SCORE and
            mq >= self.MIN_MARKET_QUALITY and
            throttle > 0 and
            adjusted_edge > 0.02 and  # at least 2% adjusted edge
            pattern_match  # must match at least some winning patterns (or no data)
        )

        # Build reason
        problems = []
        if total < self.MIN_SCORE:
            problems.append(f"score {total:.2f}<{self.MIN_SCORE}")
        if mq < self.MIN_MARKET_QUALITY:
            problems.append(f"quality {mq:.2f}<{self.MIN_MARKET_QUALITY}")
        if throttle <= 0:
            problems.append(f"strategy paused ({pause_reason})")
        if adjusted_edge <= 0.02:
            problems.append(f"adj_edge {adjusted_edge:.1%}<2%")
        if not pattern_match:
            problems.append(f"pattern mismatch (score={pattern_score:.2f})")

        if should_trade:
            reason = f"All checks passed (score={total:.2f}, pattern={pattern_score:.2f})"
        else:
            reason = " | ".join(problems) if problems else "Unknown"

        # ── Recommended position size ──
        # Scale from 1% to 5% based on score + pattern match quality
        if should_trade:
            base_pct = 0.01 + (total - self.MIN_SCORE) * 0.06  # 1% at min, 5% at score=1.0
            base_pct = min(0.05, max(0.01, base_pct))
            # Scale by feature score AND pattern match (historically winning setups → bigger size)
            size_pct = base_pct * (0.4 + feature_score * 0.3 + pattern_score * 0.3)
        else:
            size_pct = 0.0

        return TradeScore(
            total_score=round(total, 3),
            adjusted_edge=round(adjusted_edge, 4),
            adjusted_confidence=round(adj_confidence, 3),
            market_quality=mq,
            strategy_health=throttle,
            feature_score=round(feature_score, 3),
            should_trade=should_trade,
            reason=reason,
            recommended_size_pct=round(size_pct, 4),
        )

    def _compute_feature_score(self, features: TradeFeatures) -> float:
        """Compute a composite score from Bayesian feature counters.

        For each feature, look up the historical win rate for this bucket.
        Average them, weighting features with more data higher.
        Falls back to 0.5 (neutral) when no data.
        """
        health = self._get_health(features.strategy)
        buckets = features.to_bucket_key()

        total_weight = 0.0
        weighted_sum = 0.0

        for feat_name, bucket_idx in buckets.items():
            win_rate = health.feature_win_rate(feat_name, bucket_idx)
            if win_rate is not None:
                # Weight by number of samples in this bucket
                counter = health.feature_counters[feat_name][bucket_idx]
                n = max(1, counter.samples)
                # Confidence-weighted: more samples → more weight
                weight = min(5.0, math.log2(n + 1))
                weighted_sum += win_rate * weight
                total_weight += weight

        if total_weight <= 0:
            return 0.50  # no data → neutral

        return weighted_sum / total_weight

    # ─── Outcome Recording ────────────────────────────────────

    def record_outcome(
        self,
        features: TradeFeatures,
        won: bool,
        pnl: float,
        trade_id: int = 0,
    ):
        """Record a trade outcome and update all models.

        This is the LEARNING step.  Called after every position closes.
        """
        # 1. Update strategy health
        health = self._get_health(features.strategy)
        health.update(won, pnl, features)

        # 2. Update calibration
        self.calibration.record(features.confidence, won)

        # 3. Update global counter
        self.global_counter.update(won)

        # 4. Periodic decay (every 20 trades)
        self._decay_counter += 1
        if self._decay_counter >= 20:
            self._apply_global_decay()
            self._decay_counter = 0

        # 5. Log to DB for analysis
        self._log_features(trade_id, features, won, pnl)

        # Log learning
        logger.info(
            f"[QUANT] Learned: {features.strategy} {'WIN' if won else 'LOSS'} "
            f"PnL=${pnl:.4f} | EMA_WR={health.ema_win_rate:.0%} "
            f"EMA_PnL=${health.ema_pnl:.4f} | "
            f"Global WR={self.global_counter.mean:.0%} ({self.global_counter.total} trades)"
        )

    def _apply_global_decay(self):
        """Decay all counters so recent trades matter more."""
        for health in self.strategies.values():
            for feat_buckets in health.feature_counters.values():
                for counter in feat_buckets.values():
                    counter.apply_decay(0.95)
        for counter in self.calibration.bins.values():
            counter.apply_decay(0.95)
        self.global_counter.apply_decay(0.95)
        logger.debug("[QUANT] Applied global decay")

    def _log_features(self, trade_id: int, features: TradeFeatures, won: bool, pnl: float):
        """Log features to DB for offline analysis."""
        try:
            with get_connection(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO trade_features (trade_id, features, score, outcome, pnl)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    trade_id,
                    json.dumps(features.to_dict()),
                    0,  # score not computed at exit time
                    "WIN" if won else "LOSS",
                    pnl,
                ))
                conn.commit()
        except Exception as e:
            logger.debug(f"Failed to log features: {e}")

    # ─── Strategy Health ──────────────────────────────────────

    def _get_health(self, strategy: str) -> StrategyHealth:
        """Get or create health tracker for a strategy."""
        key = strategy.upper()
        if key not in self.strategies:
            self.strategies[key] = StrategyHealth(name=key)
        return self.strategies[key]

    def strategy_health(self, strategy: str) -> StrategyHealth:
        """Public accessor for strategy health."""
        return self._get_health(strategy)

    def is_strategy_allowed(self, strategy: str) -> tuple[bool, str]:
        """Quick check: should this strategy be allowed to trade?"""
        health = self._get_health(strategy)
        return health.is_healthy()

    # ─── Dynamic Edge Threshold ───────────────────────────────

    def dynamic_min_edge(self, strategy: str) -> float:
        """Compute adaptive minimum edge threshold for a strategy.

        The idea: if a strategy is doing well, relax the edge requirement
        slightly (capture more opportunities). If it's struggling, tighten
        the requirement (only take the BEST setups).

        Range: 3% (aggressive, hot streak) to 8% (conservative, cold streak)
        """
        health = self._get_health(strategy)
        if health.total_trades < 5:
            return 0.05  # default 5%

        # Base: 5%
        base = 0.05

        # Win rate adjustment: high WR → lower threshold, low WR → higher
        wr_adj = (0.5 - health.ema_win_rate) * 0.06  # ±3%

        # Consecutive loss adjustment: each loss adds +0.5%
        loss_adj = health.consecutive_losses * 0.005

        # Consecutive win adjustment: each win reduces by -0.3%
        win_adj = -health.consecutive_wins * 0.003

        threshold = base + wr_adj + loss_adj + win_adj

        # Clamp to [0.03, 0.12]
        return max(0.03, min(0.12, threshold))

    # ─── Adaptive Learning — Dynamic Parameter Adjustment ─────

    def learned_max_entry_price(self) -> float:
        """Learn the optimal max entry price from historical outcomes.

        Analyzes which price buckets have the best win rates and returns
        a recommended max entry price.  This overrides the static config
        when enough data is available.

        Returns:
            Recommended max entry price (0.10 - 0.50), or 0 if not enough data.
        """
        # Aggregate win rates across all strategies for the 'price' feature
        price_wins: dict[int, tuple[float, float]] = {}  # bucket → (total_wr, samples)
        for health in self.strategies.values():
            price_counters = health.feature_counters.get("price", {})
            for bucket_idx, counter in price_counters.items():
                if counter.samples >= 2:
                    if bucket_idx not in price_wins:
                        price_wins[bucket_idx] = (0.0, 0.0)
                    old_wr, old_n = price_wins[bucket_idx]
                    n = counter.samples
                    price_wins[bucket_idx] = (
                        (old_wr * old_n + counter.mean * n) / (old_n + n),
                        old_n + n,
                    )

        if not price_wins:
            return 0  # not enough data

        # Find the highest price bucket with win rate > 50%
        # PRICE_BINS = [0.10, 0.20, 0.40, 0.60, 0.80]
        # Bucket 0 = <$0.10, 1 = $0.10-0.20, 2 = $0.20-0.40, etc.
        best_bucket = -1
        for bucket_idx in sorted(price_wins.keys()):
            wr, n = price_wins[bucket_idx]
            if wr > 0.50 and n >= 3:
                best_bucket = bucket_idx

        if best_bucket < 0:
            return 0.10  # all buckets losing → stick to cheapest

        # Map bucket to max price
        bucket_to_price = {0: 0.10, 1: 0.20, 2: 0.40, 3: 0.60, 4: 0.80, 5: 1.0}
        recommended = bucket_to_price.get(best_bucket, 0.35)

        logger.info(
            f"[QUANT] Learned max entry price: ${recommended:.2f} "
            f"(winning buckets: {dict((k, f'{v[0]:.0%}/{v[1]:.0f}') for k, v in price_wins.items())})"
        )
        return recommended

    def should_auto_pause_strategy(self, strategy: str) -> tuple[bool, str]:
        """Check if a strategy should be automatically paused based on learning.

        Goes beyond is_healthy() by also considering:
        - Long-term negative PnL trend
        - Calibration shows persistent over-confidence
        - Feature-level data shows no winning setups
        """
        health = self._get_health(strategy)

        # Not enough data to judge
        if health.total_trades < 5:
            return False, "Not enough data"

        # Already paused by health check
        healthy, reason = health.is_healthy()
        if not healthy:
            return True, f"Health check: {reason}"

        # Long-term negative PnL with enough trades
        if health.total_trades >= 10 and health.total_pnl < -1.0:
            return True, f"Persistent negative PnL: ${health.total_pnl:.2f} over {health.total_trades} trades"

        # Win rate below 35% with statistical significance
        if health.total_trades >= 15:
            overall_wr = health.total_wins / health.total_trades
            if overall_wr < 0.35:
                return True, f"Win rate too low: {overall_wr:.0%} over {health.total_trades} trades"

        return False, "Strategy OK"

    def get_winning_patterns(self, strategy: str) -> dict[str, list[int]]:
        """Identify which feature buckets consistently win for a strategy.

        Returns a dict of feature_name → list of winning bucket indices.
        These can be used to pre-filter opportunities.
        """
        health = self._get_health(strategy)
        winning_patterns: dict[str, list[int]] = {}

        for feat_name, buckets in health.feature_counters.items():
            winners = []
            for bucket_idx, counter in buckets.items():
                if counter.samples >= 3 and counter.mean > 0.55:
                    winners.append(bucket_idx)
            if winners:
                winning_patterns[feat_name] = sorted(winners)

        return winning_patterns

    def matches_winning_pattern(self, features: TradeFeatures) -> tuple[bool, float]:
        """Check if a trade's features match historically winning patterns.

        Returns (matches, match_score) where match_score is 0-1.
        Higher scores mean more features match winning buckets.
        """
        patterns = self.get_winning_patterns(features.strategy)
        if not patterns:
            return True, 0.5  # no data → neutral

        buckets = features.to_bucket_key()
        matches = 0
        total = 0

        for feat_name, winning_buckets in patterns.items():
            if feat_name in buckets:
                total += 1
                if buckets[feat_name] in winning_buckets:
                    matches += 1

        if total == 0:
            return True, 0.5

        score = matches / total
        return score >= 0.2, score  # at least 20% of features must match (was 30% — too strict)

    # ─── Persistence ──────────────────────────────────────────

    def save_state(self):
        """Persist all state to DB."""
        state = {
            "strategies": {k: v.to_dict() for k, v in self.strategies.items()},
            "calibration": self.calibration.to_dict(),
            "global_counter": self.global_counter.to_dict(),
            "decay_counter": self._decay_counter,
        }
        try:
            with get_connection(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO quant_state (key, value, updated_at)
                    VALUES ('engine_state', ?, CURRENT_TIMESTAMP)
                """, (json.dumps(state),))
                conn.commit()
            logger.debug("[QUANT] State saved to DB")
        except Exception as e:
            logger.warning(f"[QUANT] Failed to save state: {e}")

    def load_state(self):
        """Load persisted state from DB."""
        try:
            with get_connection(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM quant_state WHERE key = 'engine_state'")
                row = cursor.fetchone()
                if not row:
                    logger.info("[QUANT] No saved state — starting fresh")
                    return

                state = json.loads(row[0])

                # Strategies
                for k, v in state.get("strategies", {}).items():
                    self.strategies[k] = StrategyHealth.from_dict(v)

                # Calibration
                cal_data = state.get("calibration", {})
                if cal_data:
                    self.calibration = CalibrationTracker.from_dict(cal_data)

                # Global counter
                gc_data = state.get("global_counter", {})
                if gc_data:
                    self.global_counter = BayesianCounter.from_dict(gc_data)

                self._decay_counter = state.get("decay_counter", 0)

                total_trades = sum(h.total_trades for h in self.strategies.values())
                logger.info(
                    f"[QUANT] Loaded state: {len(self.strategies)} strategies, "
                    f"{total_trades} total trades, "
                    f"global WR={self.global_counter.mean:.0%}"
                )
        except Exception as e:
            logger.warning(f"[QUANT] Failed to load state: {e}")

    # ─── Reporting ────────────────────────────────────────────

    def print_report(self):
        """Print a comprehensive performance report."""
        print("\n" + "=" * 70)
        print("🧠 QUANT ENGINE REPORT")
        print("=" * 70)

        print(f"\nGlobal: {self.global_counter.total} trades, "
              f"Win Rate={self.global_counter.mean:.0%} "
              f"(±{self.global_counter.uncertainty:.1%})")

        for name, health in sorted(self.strategies.items()):
            h_ok, h_reason = health.is_healthy()
            status = "✅" if h_ok else "🚫"
            throttle = health.throttle_factor()
            min_edge = self.dynamic_min_edge(name)
            print(
                f"\n{status} {name}:"
                f"  Trades={health.total_trades}"
                f"  Wins={health.total_wins}"
                f"  EMA_WR={health.ema_win_rate:.0%}"
                f"  EMA_PnL=${health.ema_pnl:.4f}"
                f"  Throttle={throttle:.0%}"
                f"  MinEdge={min_edge:.1%}"
            )
            if not h_ok:
                print(f"    Reason: {h_reason}")
            if health.consecutive_losses > 0:
                print(f"    ⚠️  {health.consecutive_losses} consecutive loss(es)")

            # Print strongest features
            best_features = []
            for feat_name, buckets in health.feature_counters.items():
                for bucket_idx, counter in buckets.items():
                    if counter.samples >= 3:
                        best_features.append(
                            (feat_name, bucket_idx, counter.mean, counter.samples)
                        )
            if best_features:
                best_features.sort(key=lambda x: x[2], reverse=True)
                print("    Best features:")
                for feat, bucket, wr, n in best_features[:3]:
                    print(f"      {feat}[{bucket}]: {wr:.0%} win rate ({n:.0f} samples)")

        # Calibration
        if self.calibration.bins:
            print(f"\n📏 Calibration:")
            for bucket, counter in sorted(self.calibration.bins.items()):
                if counter.samples >= 2:
                    expected = (CALIBRATION_BINS[bucket] if bucket < len(CALIBRATION_BINS)
                               else CALIBRATION_BINS[-1] + 0.1)
                    actual = counter.mean
                    print(f"    Conf ~{expected:.0%}: Actual WR={actual:.0%} ({counter.total} trades)")

        print("=" * 70)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helper: Extract features from market data + signal
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def extract_features(
    strategy: str,
    mode: str,
    side: str,
    price: float,
    spread: float = 0.0,
    volume_24h: float = 0.0,
    momentum_1h: float = 0.0,
    edge: float = 0.0,
    confidence: float = 0.5,
    liquidity_score: float = 0.5,
) -> TradeFeatures:
    """Convenience constructor for TradeFeatures from raw values."""
    now = datetime.utcnow()
    return TradeFeatures(
        spread=spread,
        volume_24h=volume_24h,
        price=price,
        liquidity_score=liquidity_score,
        edge=edge,
        momentum_1h=momentum_1h,
        confidence=confidence,
        strategy=strategy.upper(),
        mode=mode,
        side=side,
        hour_of_day=now.hour,
        day_of_week=now.weekday(),
    )
