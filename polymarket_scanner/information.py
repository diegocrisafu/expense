"""Information edge — estimate a market's TRUE probability from OUTSIDE data.

Why this exists
---------------
`edge.py` can only ever reprice the market's own quotes (midpoint + calibration
+ momentum).  On its own the bot therefore has *no independent view of the
world* — it just bets that the market's own price is slightly off, which after
costs is a coin flip.  Every genuine, repeatable edge on a prediction market
comes from knowing something the price has not yet absorbed.

This module fills the dormant `external_prob` hook in `analyze_binary_market`:
it forms an INDEPENDENT probability estimate for a specific market from real
information (targeted news + an LLM), and the bot only acts when that estimate
diverges from the market price by MORE than round-trip trading costs.

Hard-won design rules
---------------------
  * **Off by default.**  Requires `INFO_EDGE_ENABLED=1` and an `ANTHROPIC_API_KEY`.
    With neither, `estimate()` returns None and the bot behaves exactly as
    before — no new dependency is imported, no network call is made.
  * **Never fabricate confidence.**  A weak, stale, or unparseable estimate
    returns None rather than nudging a trade.  Silence is safer than noise.
  * **The gating logic is pure and unit-tested.**  The LLM is just one provider
    of an estimate; the decision of whether an estimate justifies a trade is
    deterministic and lives in `qualifies()` / `external_prob_for`.

NOTE: This is a *mechanism* for a real edge, not a proven money-maker.  It must
be validated in paper mode over many resolved markets before it is trusted with
real capital.  Do not read "it runs" as "it profits".
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# ── Gating thresholds ──────────────────────────────────────────────────────
# An external estimate only informs a bet when we are BOTH confident in it AND
# it disagrees with the market by more than costs.  These are deliberately
# strict: the whole point is to trade rarely and only on real information.
MIN_CONFIDENCE = 0.65          # 0-1; below this the estimate is ignored
MIN_DIVERGENCE = Decimal("0.08")  # |our_prob − market_price| must exceed this

# LLM settings (only used when the engine is enabled and a key is present).
DEFAULT_MODEL = os.environ.get("INFO_EDGE_MODEL", "claude-sonnet-5")


@dataclass(frozen=True)
class ExternalEstimate:
    """An independent probability estimate for one market."""
    prob: Decimal          # estimated P(YES resolves true), 0-1
    confidence: float      # 0-1, the estimator's self-assessed reliability
    rationale: str         # short human-readable justification
    source: str            # e.g. "claude+news"


# ── Pure decision layer (unit-tested, no I/O) ──────────────────────────────

def qualifies(est: Optional[ExternalEstimate], market_price: Decimal) -> bool:
    """True if this estimate is strong enough AND divergent enough to act on."""
    if est is None:
        return False
    if est.confidence < MIN_CONFIDENCE:
        return False
    if not (Decimal("0") < est.prob < Decimal("1")):
        return False
    return abs(est.prob - market_price) >= MIN_DIVERGENCE


def external_prob_for(est: Optional[ExternalEstimate], market_price: Decimal) -> Optional[Decimal]:
    """The value to feed into `analyze_binary_market(external_prob=...)`.

    Returns the independent estimate only when it qualifies; otherwise None,
    which makes the edge engine fall back to its market-derived estimate (i.e.
    no information edge is claimed).
    """
    return est.prob if qualifies(est, market_price) else None


# ── Targeted news retrieval (real, parse logic is testable) ────────────────

def build_news_query_url(question: str) -> str:
    """A Google-News RSS *search* URL scoped to this market's question.

    Unlike the dashboard's broad category feeds, this pulls headlines about the
    specific event, which is what an estimate actually needs.
    """
    q = urllib.parse.quote(question.strip())
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def parse_news_items(xml_bytes: bytes, limit: int = 8) -> list[dict]:
    """Parse a Google-News RSS payload into [{title, source, published}]."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return items
    for item in root.findall(".//item")[:limit]:
        items.append({
            "title": item.findtext("title", ""),
            "source": item.findtext("source", ""),
            "published": item.findtext("pubDate", ""),
        })
    return items


def fetch_market_news(question: str, limit: int = 8, timeout: float = 8.0) -> list[dict]:
    """Fetch recent headlines about a specific market. Best-effort; [] on error."""
    try:
        req = urllib.request.Request(
            build_news_query_url(question),
            headers={"User-Agent": "RogerBot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return parse_news_items(resp.read(), limit=limit)
    except Exception as e:
        logger.debug(f"news fetch failed for {question[:40]!r}: {e}")
        return []


# ── LLM-backed estimator (optional, off by default, graceful) ──────────────

_PROMPT = """You are a calibrated forecaster pricing a prediction market.

Market question (resolves YES or NO): {question}
Current market-implied probability of YES: {price:.0%}

Recent headlines (may be irrelevant, stale, or empty):
{headlines}

Estimate the TRUE probability that this resolves YES, using the headlines only
where they are clearly relevant. Be honest about uncertainty: if you don't have
information that gives you an edge over the market price, say so with low
confidence. Do NOT just echo the market price.

Respond with ONLY a JSON object, no prose:
{{"prob": <0..1>, "confidence": <0..1>, "rationale": "<one sentence>"}}"""


class InformationEngine:
    """Produces `ExternalEstimate`s from news + an LLM. Inert unless enabled."""

    def __init__(self, enabled: Optional[bool] = None, model: str = DEFAULT_MODEL):
        env_on = os.environ.get("INFO_EDGE_ENABLED", "").lower() in ("1", "true", "yes")
        self.enabled = env_on if enabled is None else enabled
        self.model = model
        self._api_key = os.environ.get("ANTHROPIC_API_KEY")

    @property
    def available(self) -> bool:
        """Ready to produce real estimates (enabled + key + SDK importable)."""
        if not (self.enabled and self._api_key):
            return False
        try:
            import anthropic  # noqa: F401
            return True
        except Exception:
            logger.warning("INFO_EDGE enabled but `anthropic` SDK not installed — staying inert")
            return False

    async def estimate(self, question: str, market_price: Decimal) -> Optional[ExternalEstimate]:
        """Independent probability estimate, or None if unavailable/uncertain."""
        if not self.available:
            return None
        try:
            import anthropic

            headlines = fetch_market_news(question)
            hl_text = "\n".join(f"- {h['title']}" for h in headlines) or "(none found)"
            prompt = _PROMPT.format(question=question, price=float(market_price), headlines=hl_text)

            client = anthropic.AsyncAnthropic(api_key=self._api_key)
            msg = await client.messages.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(getattr(b, "text", "") for b in msg.content).strip()
            return self._parse(text, source="claude+news")
        except Exception as e:
            logger.warning(f"information estimate failed for {question[:40]!r}: {e}")
            return None

    async def external_prob_for_market(self, market: dict) -> Optional[Decimal]:
        """End-to-end: from a Gamma market dict → a qualifying external_prob.

        Returns a Decimal to pass to `analyze_market_data(external_prob=...)`,
        or None when the engine is inert or the estimate doesn't qualify. Safe
        to call unconditionally — it no-ops unless the engine is available.
        """
        if not self.available:
            return None
        question = str(market.get("question") or market.get("title") or "").strip()
        if not question:
            return None
        try:
            price = Decimal(str(market.get("bestAsk"))) if market.get("bestAsk") else None
        except Exception:
            price = None
        if price is None:
            return None
        est = await self.estimate(question, price)
        return external_prob_for(est, price)

    @staticmethod
    def _parse(text: str, source: str) -> Optional[ExternalEstimate]:
        """Parse the model's JSON reply into an estimate, defensively."""
        try:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                return None
            data = json.loads(text[start:end + 1])
            prob = Decimal(str(data["prob"]))
            conf = float(data["confidence"])
            if not (Decimal("0") <= prob <= Decimal("1")) or not (0.0 <= conf <= 1.0):
                return None
            return ExternalEstimate(
                prob=prob, confidence=conf,
                rationale=str(data.get("rationale", ""))[:200], source=source,
            )
        except Exception:
            return None
