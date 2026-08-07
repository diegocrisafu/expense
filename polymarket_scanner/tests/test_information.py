"""Tests for the information-edge module.

The LLM call itself isn't tested (it needs a key and is non-deterministic), but
the parts that decide whether to ACT on an estimate are pure and must be right:
a real edge only exists when we're confident AND we disagree with the market by
more than costs. Also verifies the engine is inert by default (no key/flag).
"""

from decimal import Decimal

from polymarket_scanner.information import (
    ExternalEstimate,
    InformationEngine,
    MIN_CONFIDENCE,
    MIN_DIVERGENCE,
    build_news_query_url,
    external_prob_for,
    parse_news_items,
    qualifies,
)


def _est(prob, conf):
    return ExternalEstimate(prob=Decimal(str(prob)), confidence=conf,
                            rationale="test", source="test")


# ── Gating logic ───────────────────────────────────────────────────────────

def test_confident_and_divergent_estimate_qualifies():
    # market says 30%, we confidently say 55% → 25pt divergence → act
    assert qualifies(_est("0.55", 0.8), Decimal("0.30")) is True
    assert external_prob_for(_est("0.55", 0.8), Decimal("0.30")) == Decimal("0.55")


def test_low_confidence_is_ignored():
    est = _est("0.55", MIN_CONFIDENCE - 0.01)
    assert qualifies(est, Decimal("0.30")) is False
    assert external_prob_for(est, Decimal("0.30")) is None


def test_small_divergence_is_ignored():
    # agrees with the market within costs → no edge, don't act
    price = Decimal("0.50")
    est = _est(price + MIN_DIVERGENCE - Decimal("0.01"), 0.9)
    assert qualifies(est, price) is False
    assert external_prob_for(est, price) is None


def test_none_estimate_never_qualifies():
    assert qualifies(None, Decimal("0.30")) is False
    assert external_prob_for(None, Decimal("0.30")) is None


def test_degenerate_probabilities_rejected():
    assert qualifies(_est("0", 0.9), Decimal("0.30")) is False
    assert qualifies(_est("1", 0.9), Decimal("0.30")) is False


# ── News plumbing ──────────────────────────────────────────────────────────

def test_news_query_url_is_scoped_to_the_question():
    url = build_news_query_url("Will Switzerland win the 2026 FIFA World Cup?")
    assert url.startswith("https://news.google.com/rss/search?q=")
    assert "Switzerland" in url


def test_parse_news_items_handles_rss_and_junk():
    rss = b"""<rss><channel>
      <item><title>Team A advances</title><source>BBC</source><pubDate>Wed, 09 Jul 2026</pubDate></item>
      <item><title>Team B eliminated</title></item>
    </channel></rss>"""
    items = parse_news_items(rss, limit=8)
    assert len(items) == 2
    assert items[0]["title"] == "Team A advances"
    assert parse_news_items(b"not xml at all") == []


# ── Inert by default ───────────────────────────────────────────────────────

def test_engine_is_inert_without_flag_and_key(monkeypatch):
    monkeypatch.delenv("INFO_EDGE_ENABLED", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    eng = InformationEngine()
    assert eng.enabled is False
    assert eng.available is False


def test_parse_extracts_estimate_from_model_json():
    est = InformationEngine._parse(
        'Here you go: {"prob": 0.62, "confidence": 0.7, "rationale": "news favors yes"}',
        source="claude+news",
    )
    assert est is not None
    assert est.prob == Decimal("0.62")
    assert est.confidence == 0.7


def test_parse_rejects_out_of_range_or_garbage():
    assert InformationEngine._parse('{"prob": 1.4, "confidence": 0.7}', "x") is None
    assert InformationEngine._parse("no json here", "x") is None
