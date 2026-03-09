"""Unit tests for quant_engine module."""

import pytest
from decimal import Decimal

from polymarket_scanner.quant_engine import (
    BayesianCounter,
    CalibrationTracker,
    TradeFeatures,
    market_quality_score,
    extract_features,
    _bucket,
    PRICE_BINS,
    SPREAD_BINS,
    VOLUME_BINS,
)


class TestBucket:
    """Tests for the _bucket helper."""

    def test_below_all_bins(self):
        """Value below the first bin → bucket 0."""
        assert _bucket(0.01, PRICE_BINS) == 0

    def test_above_all_bins(self):
        """Value above the last bin → last bucket."""
        result = _bucket(0.99, PRICE_BINS)
        assert result == len(PRICE_BINS)

    def test_exact_boundary(self):
        """Value exactly at a bin edge is placed in the next bucket."""
        # PRICE_BINS = [0.10, 0.20, 0.30, 0.50, 0.70]
        assert _bucket(0.10, PRICE_BINS) >= 1

    def test_empty_bins(self):
        """Empty bin list → always bucket 0."""
        assert _bucket(0.5, []) == 0


class TestBayesianCounter:
    """Tests for BayesianCounter Beta distribution tracker."""

    def test_initial_mean(self):
        """Prior Beta(2,2) gives mean 0.5."""
        bc = BayesianCounter()
        assert bc.mean == pytest.approx(0.5)

    def test_update_win(self):
        """Winning observation increases mean."""
        bc = BayesianCounter()
        initial = bc.mean
        bc.update(won=True)
        assert bc.mean > initial

    def test_update_loss(self):
        """Losing observation decreases mean."""
        bc = BayesianCounter()
        initial = bc.mean
        bc.update(won=False)
        assert bc.mean < initial

    def test_many_wins_approaches_one(self):
        """After many wins, mean approaches 1.0."""
        bc = BayesianCounter()
        for _ in range(100):
            bc.update(won=True)
        assert bc.mean > 0.95

    def test_samples_count(self):
        """Sample count reflects observations (prior subtracted)."""
        bc = BayesianCounter()
        # Default alpha=2, beta=2; samples = (2+2) - 4 = 0
        assert bc.samples == pytest.approx(0.0)
        bc.update(won=True)
        assert bc.samples == pytest.approx(1.0)
        bc.update(won=False)
        assert bc.samples == pytest.approx(2.0)

    def test_uncertainty_decreases_with_data(self):
        """More data = lower uncertainty."""
        bc = BayesianCounter()
        u1 = bc.uncertainty
        for _ in range(50):
            bc.update(won=True)
        u2 = bc.uncertainty
        assert u2 < u1

    def test_decay(self):
        """Apply decay shrinks alpha/beta toward prior."""
        bc = BayesianCounter()
        for _ in range(10):
            bc.update(won=True)
        pre_alpha = bc.alpha
        bc.apply_decay(0.90)
        assert bc.alpha < pre_alpha

    def test_serialization_roundtrip(self):
        """to_dict / from_dict preserves state."""
        bc = BayesianCounter()
        bc.update(won=True)
        bc.update(won=False)
        d = bc.to_dict()
        bc2 = BayesianCounter.from_dict(d)
        assert bc2.alpha == bc.alpha
        assert bc2.beta == bc.beta


class TestCalibrationTracker:
    """Tests for CalibrationTracker."""

    def test_initial_adjustment_is_one(self):
        """No data → adjustment factor of 1.0 (no correction)."""
        ct = CalibrationTracker()
        assert ct.adjustment_factor(0.7) == pytest.approx(1.0)

    def test_record_and_adjust(self):
        """After recording outcomes, adjustment reflects calibration."""
        ct = CalibrationTracker()
        # Record 10 predictions at 70% confidence; 5 win → real WR = 50%
        for _ in range(5):
            ct.record(confidence=0.7, won=True)
        for _ in range(5):
            ct.record(confidence=0.7, won=False)
        # Real WR = 50%, predicted = 70% → factor ≈ 0.71
        factor = ct.adjustment_factor(0.7)
        assert factor < 1.0  # We're overconfident → should reduce

    def test_serialization_roundtrip(self):
        """to_dict / from_dict preserves state."""
        ct = CalibrationTracker()
        ct.record(confidence=0.6, won=True)
        ct.record(confidence=0.6, won=False)
        d = ct.to_dict()
        ct2 = CalibrationTracker.from_dict(d)
        assert ct2.adjustment_factor(0.6) == ct.adjustment_factor(0.6)


class TestMarketQualityScore:
    """Tests for market_quality_score function."""

    def test_excellent_market(self):
        """Tight spread, high volume, good liquidity → high score."""
        score = market_quality_score(
            spread=0.01,
            volume_24h=100000.0,
            liquidity_score=0.9,
            book_depth_bids=10,
            book_depth_asks=10,
        )
        assert score > 0.7

    def test_terrible_market(self):
        """Wide spread, no volume, no liquidity → low score."""
        score = market_quality_score(
            spread=0.15,
            volume_24h=10.0,
            liquidity_score=0.05,
            book_depth_bids=1,
            book_depth_asks=1,
        )
        assert score < 0.4

    def test_score_in_valid_range(self):
        """Score is always between 0 and 1."""
        for spread in [0.001, 0.05, 0.20]:
            for vol in [0, 5000, 200000]:
                score = market_quality_score(spread, vol, 0.5)
                assert 0.0 <= score <= 1.0


class TestExtractFeatures:
    """Tests for the extract_features helper."""

    def test_returns_trade_features(self):
        """extract_features returns a TradeFeatures object."""
        f = extract_features(
            strategy="MOMENTUM",
            mode="momentum",
            side="BUY",
            price=0.15,
            edge=0.08,
            confidence=0.85,
        )
        assert isinstance(f, TradeFeatures)
        assert f.strategy == "MOMENTUM"
        assert f.side == "BUY"

    def test_bucket_key_is_dict(self):
        """to_bucket_key returns a dict of feature→bucket_index."""
        f = extract_features(
            strategy="CORRELATED",
            mode="correlated",
            side="BUY",
            price=0.10,
            edge=0.05,
            confidence=0.70,
        )
        key = f.to_bucket_key()
        assert isinstance(key, dict)
        assert "spread" in key
        assert "volume" in key
        assert "price" in key
        assert "edge" in key

    def test_serialization_roundtrip(self):
        """to_dict produces a complete dictionary."""
        f = extract_features(
            strategy="SWING",
            mode="dip_scalp",
            side="BUY",
            price=0.25,
            spread=0.03,
            volume_24h=50000,
            momentum_1h=-0.02,
            edge=0.10,
            confidence=0.90,
            liquidity_score=0.7,
        )
        d = f.to_dict()
        assert d["strategy"] == "SWING"
        assert d["price"] == 0.25
        assert d["confidence"] == 0.90
