"""
Business Quality Score — ranks companies on fundamental strength.

Uses only backward-looking data (trailing financials, not forward estimates)
to avoid lookahead bias.  Data comes from yfinance's info dict which provides
TTM financials available as of today.

Quality dimensions:
  1. Profitability: ROE, operating margin, FCF margin
  2. Stability:     Revenue growth consistency, margin stability
  3. Balance sheet: Debt/equity, current ratio, interest coverage
  4. Scale:         Market cap (liquidity proxy)

Each dimension scores 0.0–1.0.  Final quality = weighted average.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger("fundamental.quality")


@dataclass
class QualityScore:
    """Composite business quality assessment."""
    ticker: str
    profitability: float = 0.0   # 0–1
    stability: float = 0.0      # 0–1
    balance_sheet: float = 0.0  # 0–1
    scale: float = 0.0          # 0–1
    composite: float = 0.0      # weighted avg
    grade: str = "C"            # A/B/C/D
    details: Dict = None

    def __post_init__(self):
        if self.details is None:
            self.details = {}


# Weight each dimension — profitability matters most
QUALITY_WEIGHTS = {
    "profitability": 0.35,
    "stability": 0.25,
    "balance_sheet": 0.25,
    "scale": 0.15,
}

# Grade thresholds
GRADE_MAP = [(0.80, "A"), (0.60, "B"), (0.40, "C"), (0.0, "D")]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _score_profitability(info: Dict) -> tuple:
    """Score profitability from TTM financials."""
    roe = info.get("returnOnEquity") or 0
    op_margin = info.get("operatingMargins") or 0
    fcf = info.get("freeCashflow") or 0
    revenue = info.get("totalRevenue") or 1
    fcf_margin = fcf / revenue if revenue > 0 else 0

    # Score each: map to 0–1 range using reasonable benchmarks
    # ROE: 0% = 0, 20%+ = 1
    roe_score = _clamp(roe / 0.20)
    # Operating margin: 0% = 0, 30%+ = 1
    opm_score = _clamp(op_margin / 0.30)
    # FCF margin: 0% = 0, 20%+ = 1
    fcf_score = _clamp(fcf_margin / 0.20)

    score = 0.40 * roe_score + 0.35 * opm_score + 0.25 * fcf_score
    return round(_clamp(score), 3), {
        "roe": round(roe, 4), "op_margin": round(op_margin, 4),
        "fcf_margin": round(fcf_margin, 4),
    }


def _score_stability(info: Dict) -> tuple:
    """Score earnings stability — penalise erratic revenue/margins.

    NOTE: Uses only backward-looking data (revenueGrowth, earningsQuarterlyGrowth).
    The yfinance 'earningsGrowth' field is a FORWARD analyst estimate and must NOT
    be used here — it would introduce look-ahead bias into the backtest.
    """
    rev_growth = info.get("revenueGrowth") or 0
    # Use earningsQuarterlyGrowth (trailing YoY) instead of earningsGrowth (forward estimate)
    earnings_growth = info.get("earningsQuarterlyGrowth") or 0

    # Stable growth = moderate positive (5–25% is ideal)
    # Extreme growth (>50%) or shrinkage (<0%) scores lower
    def growth_score(g):
        if g < 0:
            return _clamp(0.3 + g)  # negative growth penalised
        if g <= 0.25:
            return _clamp(0.5 + g * 2)  # sweet spot
        return _clamp(1.0 - (g - 0.25))  # too hot — lower confidence

    rev_s = growth_score(rev_growth)
    earn_s = growth_score(earnings_growth)
    score = 0.5 * rev_s + 0.5 * earn_s
    return round(_clamp(score), 3), {
        "rev_growth": round(rev_growth, 4),
        "earnings_quarterly_growth": round(earnings_growth, 4),
    }


def _score_balance_sheet(info: Dict) -> tuple:
    """Score balance sheet health."""
    debt_eq = info.get("debtToEquity") or 0
    current = info.get("currentRatio") or 1.0

    # Debt/equity: 0 = perfect (1.0), >200 = terrible (0.0)
    # (debtToEquity is a percentage in yfinance, e.g., 150 = 150%)
    de_score = _clamp(1.0 - debt_eq / 200)
    # Current ratio: <1 = bad, >2 = great
    cr_score = _clamp((current - 0.5) / 2.0)

    score = 0.60 * de_score + 0.40 * cr_score
    return round(_clamp(score), 3), {
        "debt_equity": round(debt_eq, 2), "current_ratio": round(current, 2),
    }


def _score_scale(info: Dict) -> tuple:
    """Score on market cap — proxy for liquidity and analyst coverage."""
    mcap = info.get("marketCap") or 0
    # <$2B = small (0.3), $2–10B = mid (0.6), $10–100B = large (0.8), >$100B = mega (1.0)
    if mcap >= 100e9:
        score = 1.0
    elif mcap >= 10e9:
        score = 0.7 + 0.3 * (mcap - 10e9) / 90e9
    elif mcap >= 2e9:
        score = 0.4 + 0.3 * (mcap - 2e9) / 8e9
    else:
        score = max(0.1, 0.4 * mcap / 2e9)
    return round(_clamp(score), 3), {"market_cap": mcap}


def compute_quality(ticker: str, info: Dict) -> QualityScore:
    """Compute composite quality score from yfinance info dict.

    Args:
        ticker: e.g. "AAPL"
        info:   yfinance Ticker(ticker).info dict (TTM financials)

    Returns:
        QualityScore dataclass with 0–1 scores and letter grade.
    """
    prof, prof_d = _score_profitability(info)
    stab, stab_d = _score_stability(info)
    bs, bs_d = _score_balance_sheet(info)
    sc, sc_d = _score_scale(info)

    composite = (
        QUALITY_WEIGHTS["profitability"] * prof
        + QUALITY_WEIGHTS["stability"] * stab
        + QUALITY_WEIGHTS["balance_sheet"] * bs
        + QUALITY_WEIGHTS["scale"] * sc
    )
    grade = "D"
    for threshold, g in GRADE_MAP:
        if composite >= threshold:
            grade = g
            break

    details = {**prof_d, **stab_d, **bs_d, **sc_d}
    return QualityScore(
        ticker=ticker,
        profitability=prof, stability=stab,
        balance_sheet=bs, scale=sc,
        composite=round(composite, 3), grade=grade,
        details=details,
    )
