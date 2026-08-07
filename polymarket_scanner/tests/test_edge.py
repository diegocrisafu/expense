"""Tests for the probability & edge engine.

These lock in the *direction* of the favourite-longshot calibration, which an
earlier version of the code had backwards.  The bug mattered: shrinking prices
toward 0.50 inflated the estimated probability of cheap longshots and
manufactured phantom edge on exactly the trades that later resolved to $0.
"""

from decimal import Decimal

import pytest

from polymarket_scanner.edge import (
    MIN_EDGE,
    analyze_binary_market,
    calibrate_probability,
    estimate_true_prob,
)


def _f(d: Decimal) -> float:
    return float(d)


# ── Favourite-longshot calibration direction ───────────────────────────────

def test_longshots_are_pulled_toward_zero():
    """A 5% market should be estimated *below* 5%, not above it."""
    assert _f(calibrate_probability(Decimal("0.05"))) < 0.05
    assert _f(calibrate_probability(Decimal("0.10"))) < 0.10
    assert _f(calibrate_probability(Decimal("0.20"))) < 0.20


def test_favourites_are_pushed_toward_one():
    """A 95% market should be estimated *above* 95%."""
    assert _f(calibrate_probability(Decimal("0.95"))) > 0.95
    assert _f(calibrate_probability(Decimal("0.90"))) > 0.90
    assert _f(calibrate_probability(Decimal("0.80"))) > 0.80


def test_fair_coin_is_left_alone():
    """A 50/50 market is already well calibrated."""
    assert calibrate_probability(Decimal("0.50")) == Decimal("0.50")


def test_calibration_is_monotonic_and_bounded():
    prev = Decimal("0")
    for i in range(1, 100):
        p = Decimal(i) / Decimal("100")
        c = calibrate_probability(p)
        assert Decimal("0") < c < Decimal("1")
        assert c > prev  # order-preserving
        prev = c


# ── The bug this prevents: phantom edge on a fairly-priced longshot ─────────

def test_fairly_priced_longshot_has_no_yes_edge():
    """Buying a fairly-priced 5% longshot should be a PASS, not a YES.

    Under the old shrink-toward-0.50 calibration this returned positive YES
    edge and the bot bought it; those trades resolved to zero.
    """
    e = analyze_binary_market(Decimal("0.06"), Decimal("0.04"))  # mid = 0.05
    assert e.yes_edge < 0
    assert e.best_side in ("PASS", "NO")


def test_favourite_is_not_penalised():
    """A fairly-priced favourite should not be pushed into a spurious NO."""
    e = analyze_binary_market(Decimal("0.96"), Decimal("0.94"))  # mid = 0.95
    assert e.no_edge < MIN_EDGE


# ── Momentum must not fabricate edge on longshots ──────────────────────────

def test_momentum_is_damped_at_the_extremes():
    """A one-hour wiggle on a cheap longshot is noise, so it barely moves the
    estimate — far less than the same wiggle on a 50/50 market."""
    base_ls = estimate_true_prob(Decimal("0.06"), Decimal("0.04"))
    mom_ls = estimate_true_prob(
        Decimal("0.06"), Decimal("0.04"),
        momentum=Decimal("0.05"), volume_24h=Decimal("50000"),
    )
    base_mid = estimate_true_prob(Decimal("0.51"), Decimal("0.49"))
    mom_mid = estimate_true_prob(
        Decimal("0.51"), Decimal("0.49"),
        momentum=Decimal("0.05"), volume_24h=Decimal("50000"),
    )
    longshot_kick = mom_ls - base_ls
    midmarket_kick = mom_mid - base_mid
    assert longshot_kick < midmarket_kick
    assert longshot_kick < Decimal("0.005")  # essentially negligible


def test_momentum_never_exceeds_cap():
    """Even a huge move + high volume can't shift the estimate past ±5%."""
    hi = estimate_true_prob(
        Decimal("0.51"), Decimal("0.49"),
        momentum=Decimal("0.9"), volume_24h=Decimal("10000000"),
    )
    base = estimate_true_prob(Decimal("0.51"), Decimal("0.49"))
    assert abs(hi - base) <= Decimal("0.05")


# ── Probabilities always stay in a sane band ───────────────────────────────

@pytest.mark.parametrize("ask,bid", [
    ("0.02", "0.01"),
    ("0.50", "0.50"),
    ("0.99", "0.98"),
])
def test_true_prob_stays_in_bounds(ask, bid):
    p = estimate_true_prob(Decimal(ask), Decimal(bid))
    assert Decimal("0.005") <= p <= Decimal("0.995")
