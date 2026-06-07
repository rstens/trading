"""Traffic-light assessment for each analysis section.

Produces a ``"green" | "amber" | "red"`` signal per tab so the analysis
page can show a colored dot after each agent's title — a quick visual
read of how constructive each section is about the stock.

Two strategies:

* **Rating-based** (high confidence) — the synthesis agents emit an
  explicit rating in their rendered markdown: the Research Manager and
  Portfolio Manager a 5-tier ``**Recommendation**`` / ``**Rating**``,
  the Trader a ``**Action**``, the Hedging Agent a weakness level. These
  are parsed directly.
* **Lexicon-based** (heuristic) — the free-text analysts and debaters
  have no rating, so a small financial lexicon scores bullish vs bearish
  language.

Both lean pessimistic per product requirement: an ambiguous or mixed
read resolves to amber, and a slight bearish lean resolves to red —
green requires a clear constructive majority. A colored dot is a
summary, not a substitute for reading the section.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

GREEN, AMBER, RED = "green", "amber", "red"


# ── Rating-based parsing ──────────────────────────────────────────────

# Research Manager / Portfolio Manager 5-tier rating. Pessimism bias:
# Hold is amber (not green), Underweight joins Sell as red.
_FIVE_TIER = {
    "buy": GREEN,
    "overweight": GREEN,
    "hold": AMBER,
    "underweight": RED,
    "sell": RED,
}
_FIVE_TIER_RE = re.compile(
    r"\*\*(?:Recommendation|Rating)\*\*:\s*([A-Za-z]+)", re.IGNORECASE
)

# Trader 3-tier action.
_ACTION = {"buy": GREEN, "hold": AMBER, "sell": RED}
_ACTION_RE = re.compile(r"\*\*Action\*\*:\s*([A-Za-z]+)", re.IGNORECASE)
_FINAL_PROPOSAL_RE = re.compile(
    r"FINAL TRANSACTION PROPOSAL:\s*\*\*([A-Za-z]+)\*\*", re.IGNORECASE
)

# Hedging Agent weakness rating — inverted (low weakness is good).
_WEAKNESS = {"low": GREEN, "moderate": AMBER, "elevated": RED, "severe": RED}
_WEAKNESS_LABELLED_RE = re.compile(
    r"weakness\b[^A-Za-z]{0,8}(low|moderate|elevated|severe)", re.IGNORECASE
)
_WEAKNESS_LEVELS = ("severe", "elevated", "moderate", "low")  # most→least severe


def _five_tier(text: str) -> Optional[str]:
    m = _FIVE_TIER_RE.search(text)
    return _FIVE_TIER.get(m.group(1).lower()) if m else None


def _trader(text: str) -> Optional[str]:
    m = _ACTION_RE.search(text) or _FINAL_PROPOSAL_RE.search(text)
    return _ACTION.get(m.group(1).lower()) if m else None


# The Summary states its call as a bare uppercase BUY / HOLD / SELL token.
_SUMMARY_REC_RE = re.compile(r"\b(BUY|SELL|HOLD)\b")


def _summary(text: str) -> Optional[str]:
    # Prefer a structured rating if the summary echoed one; else the bare
    # uppercase recommendation token.
    color = _trader(text) or _five_tier(text)
    if color is not None:
        return color
    m = _SUMMARY_REC_RE.search(text)
    return _ACTION.get(m.group(1).lower()) if m else None


def _hedging(text: str) -> Optional[str]:
    m = _WEAKNESS_LABELLED_RE.search(text)
    if m:
        return _WEAKNESS[m.group(1).lower()]
    # No explicit "weakness: X" — fall back to the most severe level named
    # anywhere (pessimism bias).
    for level in _WEAKNESS_LEVELS:
        if re.search(rf"\b{level}\b", text, re.IGNORECASE):
            return _WEAKNESS[level]
    return None


# ── Lexicon-based scoring ─────────────────────────────────────────────

_BULL_RE = re.compile(
    r"\b(?:bullish|undervalued|upside|outperform|outperforms?|"
    r"strength|strong(?:er|est)?|robust|healthy|attractive|"
    r"accelerat\w*|expand\w*|momentum|breakout|tailwinds?|"
    r"upgrade[ds]?|beat|beats|beating|recover\w*|resilient|"
    r"constructive|favorable|favourable|outsized|compelling)\b",
    re.IGNORECASE,
)
_BEAR_RE = re.compile(
    r"\b(?:bearish|overvalued|downside|underperform\w*|weak(?:ness|er|est)?|"
    r"declin\w*|deteriorat\w*|miss(?:ed|es)?|cut|cuts|guidance cut|"
    r"headwinds?|sell-?off|plunge[ds]?|tumbl\w*|slump\w*|"
    r"downgrade[ds]?|overbought|recession|bubble|fragile|"
    r"warning|red flags?|overextended|contraction|slowdown|"
    r"shortfall|disappoint\w*|concerns?)\b",
    re.IGNORECASE,
)
# Strong distress signals — if present and the read isn't clearly
# constructive, force at least amber and lean red.
_DISTRESS_RE = re.compile(
    r"\b(?:dilution|dilutive|going concern|bankrupt\w*|insolven\w*|"
    r"default(?:ed|s)?|delist\w*|fraud|investigation|lawsuit|"
    r"liquidity crisis|cash burn|covenant breach)\b",
    re.IGNORECASE,
)


def _lexicon(text: str) -> str:
    pos = len(_BULL_RE.findall(text))
    neg = len(_BEAR_RE.findall(text))
    distress = bool(_DISTRESS_RE.search(text))

    if pos + neg == 0:
        # No signal language at all → don't claim it's healthy.
        return RED if distress else AMBER

    ratio = (pos - neg) / (pos + neg)

    if distress and ratio < 0.30:
        return RED
    # Asymmetric thresholds = pessimism bias: green needs a clear positive
    # majority; a slight negative lean already trips red.
    if ratio >= 0.30:
        return GREEN
    if ratio <= -0.05:
        return RED
    return AMBER


# ── Public API ────────────────────────────────────────────────────────

# Tab keys whose section carries an explicit, parseable rating.
_RATING_PARSERS = {
    "research": _five_tier,
    "portfolio": _five_tier,
    "summary": _summary,
    "trader": _trader,
    "hedging": _hedging,
}


def assess_section(section_key: str, text: Optional[str]) -> Optional[str]:
    """Traffic-light color for one section, or None when there's no content."""
    if not text or not text.strip():
        return None
    parser = _RATING_PARSERS.get(section_key)
    if parser is not None:
        color = parser(text)
        if color is not None:
            return color
    # No rating (or unparseable) → lexicon. Pessimism handled inside.
    return _lexicon(text)


def assess_job(
    partial_state: Optional[Dict[str, str]],
    tab_sections: List[Tuple[str, str, str]],
) -> Dict[str, str]:
    """Map ``tab key → color`` for every section that has content."""
    state = partial_state or {}
    out: Dict[str, str] = {}
    for key, _label, src in tab_sections:
        color = assess_section(key, state.get(src))
        if color is not None:
            out[key] = color
    return out
