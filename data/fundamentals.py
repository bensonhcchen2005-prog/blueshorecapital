"""
Fundamental data layer (Yahoo Finance via yfinance).

Why this exists:
  Moomoo OpenD gives us prices, candles, and order routing — but no
  fundamentals. To score "is this stock undervalued / is the growth story
  intact?", we need P/E, P/S, EV/EBITDA, margin trajectory, insider
  holdings, etc. yfinance is free, rate-limited (~2k req/hr), and good
  enough for nightly refresh.

Use:
  >>> from data.fundamentals import get_fundamentals, screen_candidates
  >>> f = get_fundamentals("NVDA")
  >>> f["forward_pe"], f["rev_growth"], f["gross_margin"]

  >>> winners = screen_candidates(["NVDA","AMD","PLTR"], min_rev_growth=0.30)

Cache: data_cache/fundamentals/{SYMBOL}.json — refreshed once per 24h.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import yfinance as yf

PROJECT_ROOT = Path(__file__).parent.parent
CACHE_DIR = PROJECT_ROOT / "data_cache" / "fundamentals"
CACHE_TTL = timedelta(hours=24)

logger = logging.getLogger(__name__)


def _strip_moomoo_prefix(code: str) -> str:
    """US.NVDA -> NVDA. yfinance uses plain tickers."""
    return code.split(".", 1)[1] if "." in code else code


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}.json"


def _is_fresh(p: Path) -> bool:
    if not p.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)
    return age < CACHE_TTL


def get_fundamentals(code: str, force_refresh: bool = False) -> Optional[Dict]:
    """
    Pull a normalised fundamental snapshot for one ticker.

    Returns a dict with consistent keys regardless of yfinance's flaky
    field naming. Returns None on hard failure.

    Keys returned:
      symbol, name, sector, industry,
      market_cap, enterprise_value, shares_out,
      pe, forward_pe, ps, pb, ev_ebitda, ev_revenue,
      rev_growth (yoy), earnings_growth (yoy),
      gross_margin, op_margin, net_margin, fcf_margin,
      roe, roic, debt_to_equity,
      cash, total_debt, fcf_ttm, rev_ttm, eps_ttm,
      analyst_price_target, num_analysts, recommendation,
      insider_ownership, institutional_ownership,
      short_pct_float, beta,
      next_earnings_date, fetched_at
    """
    symbol = _strip_moomoo_prefix(code)
    cp = _cache_path(symbol)

    if not force_refresh and _is_fresh(cp):
        try:
            return json.loads(cp.read_text())
        except Exception:
            pass

    try:
        t = yf.Ticker(symbol)
        info = t.info or {}
        if not info or not info.get("symbol"):
            logger.warning(f"yfinance returned empty for {symbol}")
            return None

        fcf = info.get("freeCashflow") or 0
        rev = info.get("totalRevenue") or 0

        # Next earnings
        next_earn = None
        try:
            cal = t.calendar
            if cal is not None and len(cal) > 0:
                ev = cal.get("Earnings Date") if hasattr(cal, "get") else None
                if ev:
                    next_earn = str(ev[0]) if isinstance(ev, list) else str(ev)
        except Exception:
            pass

        snap = {
            "symbol":             symbol,
            "name":               info.get("longName") or info.get("shortName"),
            "sector":             info.get("sector"),
            "industry":           info.get("industry"),
            "market_cap":         info.get("marketCap"),
            "enterprise_value":   info.get("enterpriseValue"),
            "shares_out":         info.get("sharesOutstanding"),
            "pe":                 info.get("trailingPE"),
            "forward_pe":         info.get("forwardPE"),
            "ps":                 info.get("priceToSalesTrailing12Months"),
            "pb":                 info.get("priceToBook"),
            "ev_ebitda":          info.get("enterpriseToEbitda"),
            "ev_revenue":         info.get("enterpriseToRevenue"),
            "rev_growth":         info.get("revenueGrowth"),         # YoY quarterly
            "earnings_growth":    info.get("earningsGrowth"),
            "gross_margin":       info.get("grossMargins"),
            "op_margin":          info.get("operatingMargins"),
            "net_margin":         info.get("profitMargins"),
            "fcf_margin":         (fcf / rev) if (fcf and rev) else None,
            "roe":                info.get("returnOnEquity"),
            "roic":               info.get("returnOnAssets"),  # proxy when ROIC absent
            "debt_to_equity":     info.get("debtToEquity"),
            "cash":               info.get("totalCash"),
            "total_debt":         info.get("totalDebt"),
            "fcf_ttm":            fcf,
            "rev_ttm":            rev,
            "eps_ttm":            info.get("trailingEps"),
            "analyst_price_target": info.get("targetMeanPrice"),
            "num_analysts":       info.get("numberOfAnalystOpinions"),
            "recommendation":     info.get("recommendationKey"),
            "insider_ownership":  info.get("heldPercentInsiders"),
            "institutional_ownership": info.get("heldPercentInstitutions"),
            "short_pct_float":    info.get("shortPercentOfFloat"),
            "beta":               info.get("beta"),
            "next_earnings_date": next_earn,
            "current_price":      info.get("currentPrice") or info.get("regularMarketPrice"),
            "fetched_at":         datetime.now().isoformat(timespec="seconds"),
        }

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(snap, indent=2, default=str))
        return snap

    except Exception as e:
        logger.warning(f"get_fundamentals({symbol}) failed: {e}")
        return None


def batch_fundamentals(codes: List[str], sleep_sec: float = 0.4) -> Dict[str, Dict]:
    """Fetch fundamentals for a batch with throttling."""
    out = {}
    for c in codes:
        f = get_fundamentals(c)
        if f:
            out[c] = f
        time.sleep(sleep_sec)
    return out


# ─── Screening helpers ────────────────────────────────────────────────

def screen_candidates(codes: List[str],
                      min_rev_growth: float = 0.20,
                      min_gross_margin: float = 0.40,
                      max_ps: Optional[float] = None,
                      max_market_cap: Optional[float] = None) -> List[Dict]:
    """
    Quick screen for high-growth, high-quality names.

    Default thresholds biased toward 5x-20x candidates:
      - revenue growth ≥ 20% YoY (compounder territory)
      - gross margin ≥ 40% (durable economics)
      - optional: P/S cap, market-cap cap
    """
    results = []
    for c in codes:
        f = get_fundamentals(c)
        if not f:
            continue
        rg = f.get("rev_growth") or 0
        gm = f.get("gross_margin") or 0
        ps = f.get("ps") or 0
        mc = f.get("market_cap") or 0
        if rg < min_rev_growth:
            continue
        if gm < min_gross_margin:
            continue
        if max_ps and ps > max_ps:
            continue
        if max_market_cap and mc > max_market_cap:
            continue
        results.append(f)
    return sorted(results, key=lambda f: -(f.get("rev_growth") or 0))


def compute_quality_score(f: Dict) -> float:
    """
    A 0-1 composite of business quality for ranking candidates.

    Weights:
      30%  revenue growth (YoY)
      20%  gross margin
      15%  operating margin
      15%  FCF margin
      10%  ROE
      10%  insider ownership (skin in the game)

    All inputs normalised so each contributes 0-1.
    """
    def clamp(x, lo=0.0, hi=1.0):
        return max(lo, min(hi, x or 0))

    rev_g = clamp((f.get("rev_growth") or 0) / 0.5)            # 50% growth = 1.0
    gm    = clamp((f.get("gross_margin") or 0) / 0.8)          # 80% gross = 1.0
    om    = clamp((f.get("op_margin") or 0) / 0.4)             # 40% op = 1.0
    fcf   = clamp((f.get("fcf_margin") or 0) / 0.3)            # 30% fcf = 1.0
    roe   = clamp((f.get("roe") or 0) / 0.3)                   # 30% roe = 1.0
    ins   = clamp((f.get("insider_ownership") or 0) / 0.15)    # 15%+ insider = 1.0

    return round(0.30*rev_g + 0.20*gm + 0.15*om + 0.15*fcf +
                 0.10*roe + 0.10*ins, 3)
