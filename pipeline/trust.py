"""Source-level trust tier -- the counterweight to a vendor-blog-heavy
candidate pool.

Every source is classified once into a tier, cached in JSON, and
human-correctable (edit cache/trust_tiers.json). The tier is fed into the
score prompt so that vendor-published content (a company writing about its
own products) has to clear a HIGHER bar for org/strategy relevance than
independent analysis or journalism about the same topic. This is what the
original design meant by "separate signal from vendor noise" at the source
level -- the per-item vendor_marketing flag is the item-level complement.

Tiers (most→least trusted for delivery-economics *strategy*):
  - independent_analysis: independent analysts/researchers writing about
    market, economic, and strategic shifts (Stratechery, Exponential View,
    Benedict Evans, Marginal Revolution, Ben Thompson-style commentary).
  - independent_news: independent journalism, newsletters, and aggregators
    (Bay Area Times, TechMeme, Hacker News, The Neuron). Reports on the
    market rather than being an interested party in it.
  - vendor: a company (or investor) publishing about its own products,
    portfolio, or theses (GCP, Azure, AWS, OpenAI, Anthropic, LangChain,
    a16z). Often genuinely useful for *fluency*, but its adoption/impact
    claims are self-interested, so its bar for *org/strategy* relevance is
    higher.

Note: tier affects org (strategy) weighting, NOT fluency. A vendor
engineering post can be highly fluency-relevant on its technical merits;
the tier's job is to stop vendor how-tos from masquerading as strategy.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from pipeline.config import CONFIG

logger = logging.getLogger(__name__)

INDEPENDENT_ANALYSIS = "independent_analysis"
INDEPENDENT_NEWS = "independent_news"
VENDOR = "vendor"

# Neutral middle for a source we haven't classified -- don't accidentally
# over-trust an unknown as strategy, nor nuke it as vendor.
DEFAULT_TIER = INDEPENDENT_NEWS

# Human-readable phrasing injected into the score prompt per tier.
TIER_DESCRIPTION = {
    INDEPENDENT_ANALYSIS: "an independent analysis/research source (writes about the market, not an interested party in it)",
    INDEPENDENT_NEWS: "an independent news/aggregator source",
    VENDOR: "a vendor-published source (a company or investor writing about its own products, portfolio, or theses -- treat its adoption/impact claims skeptically)",
}

# Seed classification for the known sources in data/sources.tsv. Written to
# the cache on first use so it can be hand-corrected there; edits win over
# this seed.
SEED_TIERS: dict[str, str] = {
    # independent analysis / research
    "Stratechery": INDEPENDENT_ANALYSIS,
    "Exponential View": INDEPENDENT_ANALYSIS,
    "One Useful Thing": INDEPENDENT_ANALYSIS,
    "Benedict Evans": INDEPENDENT_ANALYSIS,
    "Marginal Revolution": INDEPENDENT_ANALYSIS,
    "Simon Willison Blog": INDEPENDENT_ANALYSIS,
    "Paul Graham Essays": INDEPENDENT_ANALYSIS,
    "Economist": INDEPENDENT_ANALYSIS,
    "Economist (Business)": INDEPENDENT_ANALYSIS,
    "Economist (Science & Technology)": INDEPENDENT_ANALYSIS,
    "Economist (Finance & Economics)": INDEPENDENT_ANALYSIS,
    "WSJ (Business)": INDEPENDENT_ANALYSIS,
    "WSJ (Technology)": INDEPENDENT_ANALYSIS,
    "WSJ (Economy)": INDEPENDENT_ANALYSIS,
    "Value Investing World": INDEPENDENT_ANALYSIS,
    "Lab Notes Blog": INDEPENDENT_ANALYSIS,
    "Sebastian Mallaby": INDEPENDENT_ANALYSIS,
    "Harvard Business Review": INDEPENDENT_ANALYSIS,
    # independent news / newsletters / aggregators
    "The Bay Area Times": INDEPENDENT_NEWS,
    "TechMeme": INDEPENDENT_NEWS,
    "The Neuron": INDEPENDENT_NEWS,
    "AI Newsletter": INDEPENDENT_NEWS,
    "Ben's Bites": INDEPENDENT_NEWS,
    "TBPN": INDEPENDENT_NEWS,
    "Every": INDEPENDENT_NEWS,
    "Hacker News": INDEPENDENT_NEWS,
    "Lit-Quidity": INDEPENDENT_NEWS,
    "WSJ CIO Journal": INDEPENDENT_NEWS,
    "WSJ Print": INDEPENDENT_NEWS,
    "Intelligence Squared": INDEPENDENT_NEWS,
    # vendor / interested party (down-weighted for org/strategy)
    "Azure": VENDOR,
    "GCP": VENDOR,
    "AWS": VENDOR,
    "Google": VENDOR,
    "OpenAI": VENDOR,
    "Anthropic": VENDOR,
    "LangChain": VENDOR,
    "PineCone": VENDOR,
    "A16Z": VENDOR,
    "Gartner Insights": VENDOR,
    "iShares": VENDOR,
    "AI Courses": VENDOR,
}


class TrustStore:
    """JSON-cached, human-correctable source→tier map. Same convention as
    StrategyCache / SeenStore: stable keys, survives re-runs, an edited cache
    entry always wins over the built-in seed."""

    def __init__(self, path: Path | None = None):
        self.path = path or CONFIG.trust_cache
        self._overrides: dict[str, str] = {}
        if self.path.exists():
            self._overrides = json.loads(self.path.read_text(encoding="utf-8"))

    def get_tier(self, source_name: str) -> str:
        return self._overrides.get(source_name) or SEED_TIERS.get(source_name, DEFAULT_TIER)

    def describe(self, source_name: str) -> str:
        return TIER_DESCRIPTION[self.get_tier(source_name)]

    def materialize_seed(self) -> None:
        """Write the full seed map to the cache file (without clobbering any
        existing hand-edits) so the user has a complete file to correct."""
        merged = dict(SEED_TIERS)
        merged.update(self._overrides)  # existing edits win
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
        self._overrides = merged
