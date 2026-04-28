"""
Sleeve Classifier — decides whether a stock belongs in Core, Tactical, or Neither.

Architecture:
  ┌─────────────────────────────────────────────────────────┐
  │                   SLEEVE DECISION ENGINE                │
  │                                                         │
  │  Fundamental Layer (quality + valuation)                │
  │       ↓                                                 │
  │  Sleeve Assignment: CORE / TACTICAL / NONE              │
  │       ↓                                                 │
  │  Strategy Mapping:                                      │
  │    CORE    → buy stock, sell CSP, hold + CC, wait       │
  │    TACTICAL → existing signal engine + spreads/options   │
  │    NONE    → skip entirely                              │
  │       ↓                                                 │
  │  Technical Timing (existing regime + signal engine)     │
  │       ↓                                                 │
  │  Portfolio Construction (allocation, overlap, risk)     │
  └─────────────────────────────────────────────────────────┘

Rules to prevent bias/confusion:
  1. Fundamental scores use ONLY trailing data (no forward estimates)
  2. Sleeve assignment runs DAILY, not per-signal (stable, not reactive)
  3. Same ticker CANNOT be in both sleeves simultaneously
  4. Core sleeve decisions are slow (weekly rebalance cadence)
  5. Tactical sleeve uses existing signal engine (no new signal sources)
  6. No score boosting between layers — each layer is independent
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

from fundamental.quality import QualityScore, compute_quality
from fundamental.valuation import ValuationScore, compute_valuation

logger = logging.getLogger("fundamental.sleeve")

LOG_DIR = Path("logs")


@dataclass
class SleeveAssignment:
    """Classification result for a single ticker."""
    ticker: str
    sleeve: str                # "core" | "tactical" | "none"
    quality: QualityScore = None
    valuation: ValuationScore = None
    strategy: str = "wait"     # "buy_stock" | "sell_csp" | "hold_cc" | "wait" | "tactical"
    confidence: float = 0.0    # 0–1 composite confidence
    reasoning: str = ""
    details: Dict = field(default_factory=dict)


# ── Sleeve classification thresholds ──────────────────────────
# These are NOT fitted to backtest results — they reflect
# conservative institutional quality standards.

CORE_THRESHOLDS = {
    "min_quality": 0.55,        # B grade or better
    "min_valuation": 0.40,      # at least fair value
    "min_combined": 1.00,       # quality + valuation must sum to this
}

TACTICAL_THRESHOLDS = {
    "min_quality": 0.30,        # D grade OK for tactical (defined risk)
    "max_quality": 0.55,        # if quality is higher, it's core, not tactical
    "min_scale": 0.40,          # must be liquid enough for options
}


# ── Sector peer groups for peer comparison ────────────────────
SECTOR_PEERS = {
    "tech_mega":   ["AAPL", "MSFT", "GOOGL", "AMZN", "META"],
    "semis":       ["NVDA", "AMD", "AVGO", "ARM"],
    "financials":  ["JPM", "GS", "XLF"],
    "healthcare":  ["UNH"],
    "energy":      ["XOM", "XLE"],
    "industrials": ["BA"],
    "growth_spec": ["TSLA", "PLTR", "COIN", "SMCI", "MSTR", "CRM"],
}

def _get_peer_group(ticker: str) -> List[str]:
    """Find peers for a ticker from sector map."""
    for group, members in SECTOR_PEERS.items():
        if ticker in members:
            return [m for m in members if m != ticker]
    return []


# ── Core sleeve strategy mapping ─────────────────────────────

def _map_core_strategy(quality: QualityScore, valuation: ValuationScore,
                       has_position: bool, regime: str) -> str:
    """Decide the core sleeve expression for this name.

    Returns: "buy_stock" | "sell_csp" | "hold_cc" | "wait"

    Logic:
      - If valuation is very attractive (>0.65) AND quality is high → buy stock
      - If valuation is mildly attractive (0.45-0.65) → sell CSP (enter at discount)
      - If already holding AND valuation neutral → hold + covered call
      - Otherwise → wait
      - In bear regime → only CSP or wait (don't buy outright)
    """
    q = quality.composite
    v = valuation.composite
    mos = valuation.dcf_margin_of_safety

    if has_position:
        if v < 0.30:
            return "wait"  # overvalued — don't add, consider trimming
        return "hold_cc"   # hold + write covered calls for income

    if regime == "bear":
        if v >= 0.55 and q >= 0.60:
            return "sell_csp"  # enter via put selling at discount in bear
        return "wait"

    # Bull or flat regime
    if v >= 0.65 and q >= 0.60:
        return "buy_stock"
    if v >= 0.45 and q >= 0.55:
        return "sell_csp"
    return "wait"


def classify_ticker(ticker: str, info: Dict,
                    peer_infos: Optional[Dict[str, Dict]] = None,
                    historical_pe_median: Optional[float] = None,
                    has_position: bool = False,
                    regime: str = "bull",
                    technical_score: Optional[float] = None) -> SleeveAssignment:
    """Classify a single ticker into core/tactical/none.

    Args:
        ticker: e.g. "AAPL"
        info: yfinance Ticker.info dict
        peer_infos: {peer_ticker: info_dict} for sector peers
        historical_pe_median: 5-year median PE (optional)
        has_position: whether we already hold this name
        regime: current market regime ("bull"/"flat"/"bear")
        technical_score: latest signal score from tactical engine (0–1, optional)

    Returns:
        SleeveAssignment with sleeve, strategy, and reasoning.
    """
    quality = compute_quality(ticker, info)
    valuation = compute_valuation(ticker, info, peer_infos, historical_pe_median)

    q = quality.composite
    v = valuation.composite
    combined = q + v

    # ── Determine sleeve ──────────────────────────────────────
    sleeve = "none"
    strategy = "wait"
    reasoning_parts = []

    # Core sleeve: high quality + at least fair value
    if (q >= CORE_THRESHOLDS["min_quality"]
            and v >= CORE_THRESHOLDS["min_valuation"]
            and combined >= CORE_THRESHOLDS["min_combined"]):
        sleeve = "core"
        strategy = _map_core_strategy(quality, valuation, has_position, regime)
        reasoning_parts.append(
            f"CORE: quality={q:.2f} ({quality.grade}), "
            f"valuation={v:.2f}, combined={combined:.2f}"
        )
        if valuation.dcf_fair_value:
            reasoning_parts.append(
                f"DCF fair value=${valuation.dcf_fair_value:.0f}, "
                f"margin of safety={valuation.dcf_margin_of_safety:+.1%}"
            )

    # Tactical sleeve: liquid enough for options, has a technical catalyst
    elif (q >= TACTICAL_THRESHOLDS["min_quality"]
          and quality.scale >= TACTICAL_THRESHOLDS["min_scale"]
          and (technical_score is not None and technical_score >= 0.45)):
        sleeve = "tactical"
        strategy = "tactical"
        reasoning_parts.append(
            f"TACTICAL: quality={q:.2f}, tech_score={technical_score:.2f}, "
            f"uses existing signal engine"
        )

    # Neither: low quality or no catalyst
    else:
        sleeve = "none"
        strategy = "wait"
        reasons = []
        if q < CORE_THRESHOLDS["min_quality"]:
            reasons.append(f"quality too low ({q:.2f} < {CORE_THRESHOLDS['min_quality']})")
        if v < CORE_THRESHOLDS["min_valuation"]:
            reasons.append(f"valuation unattractive ({v:.2f})")
        if technical_score is None or technical_score < 0.45:
            reasons.append("no tactical catalyst")
        reasoning_parts.append(f"NONE: {'; '.join(reasons)}")

    # ── Technical sanity check for core sleeve ────────────────
    # Even in core, don't buy into a crashing chart
    if sleeve == "core" and strategy in ("buy_stock", "sell_csp"):
        if technical_score is not None and technical_score < 0.20:
            strategy = "wait"
            reasoning_parts.append(
                "Technical sanity fail: deferring entry (tech_score too low)"
            )

    confidence = min(combined / 2.0, 1.0)  # normalise 0–2 range to 0–1

    return SleeveAssignment(
        ticker=ticker,
        sleeve=sleeve,
        quality=quality,
        valuation=valuation,
        strategy=strategy,
        confidence=round(confidence, 3),
        reasoning=" | ".join(reasoning_parts),
        details={
            "quality_composite": q,
            "valuation_composite": v,
            "combined": round(combined, 3),
            "regime": regime,
            "has_position": has_position,
            "technical_score": technical_score,
        },
    )


def classify_universe(tickers: List[str], regime: str = "bull",
                      existing_positions: Optional[List[str]] = None,
                      technical_scores: Optional[Dict[str, float]] = None,
                      ) -> Dict[str, SleeveAssignment]:
    """Classify entire universe into sleeves.

    Fetches fundamental data via yfinance (rate-limited).
    Returns dict of {ticker: SleeveAssignment}.
    """
    import time
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed — cannot fetch fundamentals")
        return {}

    existing = set(existing_positions or [])
    tech_scores = technical_scores or {}
    results = {}

    # First pass: fetch all info dicts
    infos = {}
    for tk in tickers:
        try:
            t = yf.Ticker(tk)
            infos[tk] = t.info or {}
            time.sleep(0.3)  # rate limit
        except Exception as e:
            logger.debug(f"Failed to fetch info for {tk}: {e}")
            infos[tk] = {}

    # Second pass: classify with peer context
    for tk in tickers:
        info = infos.get(tk, {})
        if not info or not info.get("marketCap"):
            results[tk] = SleeveAssignment(
                ticker=tk, sleeve="none", strategy="wait",
                reasoning="No fundamental data available",
            )
            continue

        peers = _get_peer_group(tk)
        peer_infos = {p: infos[p] for p in peers if p in infos and infos[p].get("marketCap")}

        results[tk] = classify_ticker(
            ticker=tk, info=info, peer_infos=peer_infos,
            has_position=(tk in existing),
            regime=regime,
            technical_score=tech_scores.get(tk),
        )

    return results


def save_sleeve_assignments(assignments: Dict[str, SleeveAssignment]):
    """Persist sleeve classifications to disk for dashboard/backtest."""
    out = {}
    for tk, sa in assignments.items():
        out[tk] = {
            "ticker": sa.ticker,
            "sleeve": sa.sleeve,
            "strategy": sa.strategy,
            "confidence": sa.confidence,
            "reasoning": sa.reasoning,
            "quality_grade": sa.quality.grade if sa.quality else None,
            "quality_composite": sa.quality.composite if sa.quality else None,
            "valuation_composite": sa.valuation.composite if sa.valuation else None,
            "dcf_fair_value": sa.valuation.dcf_fair_value if sa.valuation else None,
            "dcf_mos": sa.valuation.dcf_margin_of_safety if sa.valuation else None,
        }
    path = LOG_DIR / "sleeve_assignments.json"
    path.write_text(json.dumps(out, indent=2, default=str))
    logger.info(f"Saved sleeve assignments to {path}")
    return path
