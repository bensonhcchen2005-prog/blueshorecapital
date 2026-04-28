"""
Valuation Score — compare current valuation to peers and history.

Uses only trailing metrics (TTM P/E, EV/EBITDA, P/S, P/FCF) to avoid
lookahead bias from forward estimates.

Three sub-scores:
  1. Absolute: is the stock cheap vs reasonable absolute benchmarks?
  2. Peer-relative: is it cheap vs its sector peers?
  3. Historical: is it cheap vs its own historical median?

Conservative DCF provides a sanity-check fair value, NOT a precise target.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("fundamental.valuation")


@dataclass
class ValuationScore:
    ticker: str
    pe_ttm: Optional[float] = None
    ev_ebitda: Optional[float] = None
    ps_ttm: Optional[float] = None
    pfcf: Optional[float] = None
    absolute_score: float = 0.5    # 0=expensive, 1=cheap
    peer_score: float = 0.5        # 0=expensive vs peers, 1=cheap vs peers
    historical_score: float = 0.5  # 0=expensive vs own history, 1=cheap vs own history
    dcf_fair_value: Optional[float] = None
    dcf_margin_of_safety: float = 0.0  # current price vs DCF fair value
    composite: float = 0.5
    details: Dict = field(default_factory=dict)


# Absolute valuation benchmarks (sector-agnostic, deliberately conservative)
# These are NOT fitted to data — they're textbook "reasonable" ranges
ABS_BENCHMARKS = {
    "pe":       {"cheap": 12, "fair": 20, "expensive": 35},
    "ev_ebitda": {"cheap": 8, "fair": 14, "expensive": 25},
    "ps":       {"cheap": 1.5, "fair": 4, "expensive": 10},
    "pfcf":     {"cheap": 12, "fair": 22, "expensive": 40},
}


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _ratio_score(value: Optional[float], bench: Dict) -> float:
    """Score a valuation ratio: cheap → 1.0, expensive → 0.0."""
    if value is None or value <= 0 or math.isnan(value):
        return 0.5  # no data → neutral
    if value <= bench["cheap"]:
        return 1.0
    if value >= bench["expensive"]:
        return 0.0
    if value <= bench["fair"]:
        # cheap → fair: 1.0 → 0.6
        return 1.0 - 0.4 * (value - bench["cheap"]) / (bench["fair"] - bench["cheap"])
    # fair → expensive: 0.6 → 0.0
    return 0.6 * (1 - (value - bench["fair"]) / (bench["expensive"] - bench["fair"]))


def score_absolute(info: Dict) -> tuple:
    """Score current valuation vs absolute benchmarks."""
    pe = info.get("trailingPE")
    ev_ebitda = info.get("enterpriseToEbitda")
    ps = info.get("priceToSalesTrailing12Months")
    # Price to FCF
    mcap = info.get("marketCap") or 0
    fcf = info.get("freeCashflow") or 0
    pfcf = mcap / fcf if fcf > 0 else None

    scores = []
    details = {}
    for name, val, bench_key in [
        ("pe", pe, "pe"), ("ev_ebitda", ev_ebitda, "ev_ebitda"),
        ("ps", ps, "ps"), ("pfcf", pfcf, "pfcf"),
    ]:
        s = _ratio_score(val, ABS_BENCHMARKS[bench_key])
        scores.append(s)
        details[name] = round(val, 2) if val is not None else None
        details[f"{name}_score"] = round(s, 3)

    # Equal-weight the available ratios
    valid = [s for s in scores if s != 0.5 or True]  # include all
    composite = sum(valid) / len(valid) if valid else 0.5
    return round(_clamp(composite), 3), details


def score_vs_peers(ticker: str, info: Dict, peer_infos: Dict[str, Dict]) -> tuple:
    """Score valuation relative to sector peers.

    peer_infos: dict of {ticker: yfinance_info} for peer companies.
    Uses PE, EV/EBITDA, P/S — ticker ranks as percentile among peers.
    """
    if not peer_infos:
        return 0.5, {"note": "no peer data"}

    def _percentile(val, peer_vals):
        """Lower percentile = cheaper = higher score."""
        if val is None or not peer_vals:
            return 0.5
        peer_vals = sorted(peer_vals)
        rank = sum(1 for p in peer_vals if p <= val)
        pctile = rank / len(peer_vals)
        return round(_clamp(1.0 - pctile), 3)  # invert: cheap = high score

    pe = info.get("trailingPE")
    peer_pes = [p.get("trailingPE") for p in peer_infos.values()
                if p.get("trailingPE") and p.get("trailingPE") > 0]

    ev_eb = info.get("enterpriseToEbitda")
    peer_ev = [p.get("enterpriseToEbitda") for p in peer_infos.values()
               if p.get("enterpriseToEbitda") and p.get("enterpriseToEbitda") > 0]

    pe_s = _percentile(pe, peer_pes)
    ev_s = _percentile(ev_eb, peer_ev)

    composite = (pe_s + ev_s) / 2
    return round(composite, 3), {
        "pe_rank_pctile": pe_s, "ev_rank_pctile": ev_s,
        "n_peers": len(peer_infos),
    }


def score_vs_history(ticker: str, current_pe: Optional[float],
                     historical_pe_median: Optional[float]) -> tuple:
    """Score current PE vs own historical median.

    historical_pe_median should come from trailing data (e.g. 5-year median PE).
    We use PE only because historical EV/EBITDA is harder to get reliably.
    """
    if current_pe is None or historical_pe_median is None:
        return 0.5, {"note": "insufficient history"}
    if historical_pe_median <= 0:
        return 0.5, {"note": "invalid historical PE"}

    ratio = current_pe / historical_pe_median
    # ratio < 0.8 → cheap (score 0.9), ratio = 1.0 → fair (0.5), ratio > 1.3 → expensive (0.1)
    if ratio <= 0.8:
        score = 0.9
    elif ratio <= 1.0:
        score = 0.9 - 0.4 * (ratio - 0.8) / 0.2
    elif ratio <= 1.3:
        score = 0.5 - 0.4 * (ratio - 1.0) / 0.3
    else:
        score = max(0.05, 0.1 - 0.1 * (ratio - 1.3))

    return round(_clamp(score), 3), {
        "current_pe": round(current_pe, 2),
        "historical_pe_median": round(historical_pe_median, 2),
        "pe_ratio_vs_history": round(ratio, 3),
    }


def conservative_dcf(info: Dict, discount_rate: float = 0.10,
                     terminal_growth: float = 0.025,
                     growth_haircut: float = 0.50) -> tuple:
    """Ultra-conservative DCF — intentionally pessimistic.

    Uses FCF as base, grows at HALF of analyst growth rate for 5 years,
    then terminal value at 2.5% perpetuity.  High discount rate (10%).

    This is a floor estimate, not a target price.  If market price < DCF,
    the stock is very likely undervalued.

    Args:
        growth_haircut: fraction of analyst growth to use (0.5 = half).
            Deliberately low to avoid being optimistic.
    """
    fcf = info.get("freeCashflow")
    shares = info.get("sharesOutstanding")
    price = info.get("currentPrice") or info.get("previousClose")
    growth = info.get("earningsGrowth") or info.get("revenueGrowth") or 0.05

    if not fcf or not shares or fcf <= 0 or shares <= 0:
        return None, 0.0, {"note": "insufficient data for DCF"}

    # Apply haircut to growth estimate
    g = growth * growth_haircut
    g = max(0.0, min(g, 0.15))  # cap at 15% — no hockey sticks

    # 5-year projected FCF, discounted
    pv_fcfs = 0.0
    projected_fcf = fcf
    for yr in range(1, 6):
        projected_fcf *= (1 + g)
        pv_fcfs += projected_fcf / (1 + discount_rate) ** yr

    # Terminal value: FCF_year5 * (1 + terminal_growth) / (discount - terminal)
    terminal_fcf = projected_fcf * (1 + terminal_growth)
    terminal_value = terminal_fcf / (discount_rate - terminal_growth)
    pv_terminal = terminal_value / (1 + discount_rate) ** 5

    total_value = pv_fcfs + pv_terminal
    fair_per_share = total_value / shares

    margin_of_safety = 0.0
    if price and price > 0:
        margin_of_safety = (fair_per_share - price) / price

    return (
        round(fair_per_share, 2),
        round(margin_of_safety, 3),
        {
            "fcf": fcf, "shares": shares,
            "growth_used": round(g, 4),
            "discount_rate": discount_rate,
            "pv_fcfs": round(pv_fcfs, 0),
            "pv_terminal": round(pv_terminal, 0),
            "fair_per_share": round(fair_per_share, 2),
        }
    )


def compute_valuation(ticker: str, info: Dict,
                      peer_infos: Optional[Dict[str, Dict]] = None,
                      historical_pe_median: Optional[float] = None) -> ValuationScore:
    """Full valuation assessment combining all sub-scores."""
    abs_score, abs_details = score_absolute(info)
    peer_score, peer_details = score_vs_peers(ticker, info, peer_infos or {})
    hist_score, hist_details = score_vs_history(
        ticker, info.get("trailingPE"), historical_pe_median
    )
    dcf_fv, mos, dcf_details = conservative_dcf(info)

    # Weighted composite — absolute most important, history least (most data-dependent)
    composite = 0.40 * abs_score + 0.30 * peer_score + 0.30 * hist_score
    # Bonus if DCF margin of safety > 20%
    if mos > 0.20:
        composite = min(1.0, composite + 0.10)

    return ValuationScore(
        ticker=ticker,
        pe_ttm=info.get("trailingPE"),
        ev_ebitda=info.get("enterpriseToEbitda"),
        ps_ttm=info.get("priceToSalesTrailing12Months"),
        pfcf=abs_details.get("pfcf"),
        absolute_score=abs_score,
        peer_score=peer_score,
        historical_score=hist_score,
        dcf_fair_value=dcf_fv,
        dcf_margin_of_safety=round(mos, 3),
        composite=round(_clamp(composite), 3),
        details={**abs_details, **peer_details, **hist_details, **dcf_details},
    )
