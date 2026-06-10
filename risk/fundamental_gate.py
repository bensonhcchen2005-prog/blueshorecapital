"""
Fundamental Gate — every NEW long position must pass a fundamental
underwrite before any technical signal is acted on. Technicals can
ONLY decide the timing of entry/exit within names that have already
passed the fundamental bar.

Rule order:
  1. SYMBOL must be in the universe (US or curated HK whitelist)
  2. Fundamentals must be fetchable (yfinance returns data)
  3. Quality score >= MIN_QUALITY (default 0.50)
  4. Revenue growth >= MIN_REV_GROWTH (default 5%)
  5. Either profitable OR clear path to profitability (op_margin > -10%)
  6. Forward P/E not absurd (< 100 for unprofitable, < 60 for profitable)
  7. Insider ownership > 1% (skin in the game)
  8. Not in earnings blackout window (< 3 days to earnings)

If any rule fails → REJECT. Bot must not place an entry order.
For exit/close decisions, this gate does NOT apply — exits are
purely risk + technical driven.

For HK names: yfinance data is often missing, so we use a curated
whitelist of pre-vetted compounders. Whitelist is maintained manually
in HK_FUND_WHITELIST.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

from data.fundamentals import get_fundamentals, compute_quality_score

logger = logging.getLogger(__name__)

# ─── Thresholds (env-overridable) ─────────────────────────────────

import os

MIN_QUALITY      = float(os.environ.get("FUND_GATE_MIN_QUALITY", "0.50"))
MIN_REV_GROWTH   = float(os.environ.get("FUND_GATE_MIN_REV_GROWTH", "0.05"))
MIN_OP_MARGIN    = float(os.environ.get("FUND_GATE_MIN_OP_MARGIN", "-0.10"))
MAX_FWD_PE_PROFIT = float(os.environ.get("FUND_GATE_MAX_PE_PROFITABLE", "60"))
MAX_FWD_PE_UNPROFIT = float(os.environ.get("FUND_GATE_MAX_PE_UNPROFITABLE", "100"))
MIN_INSIDER_OWN  = float(os.environ.get("FUND_GATE_MIN_INSIDER", "0.01"))
EARNINGS_BLACKOUT_DAYS = int(os.environ.get("FUND_GATE_EARNINGS_DAYS", "3"))


# ─── Curated HK whitelist (manually fundamentally underwritten) ──

HK_FUND_WHITELIST = {
    "HK.00700": {"name":"Tencent",      "thesis":"network_effects + gaming recovery + ad acceleration"},
    "HK.01810": {"name":"Xiaomi",       "thesis":"EV ramp + handset margin expansion + IoT scale"},
    "HK.02269": {"name":"WuXi Bio",     "thesis":"CDMO scale leader + biotech recovery rotation"},
    "HK.02318": {"name":"Ping An",      "thesis":"China insurance recovery + dividend yield"},
    "HK.02388": {"name":"BOC HK",       "thesis":"HK dividend banker + HIBOR-sensitive NIM"},
    "HK.01299": {"name":"AIA",          "thesis":"pan-Asia insurance compounder + ASEAN growth"},
    "HK.00388": {"name":"HKEX",         "thesis":"monopoly exchange + ADR repatriation"},
    "HK.09618": {"name":"JD.com",       "thesis":"logistics moat + grocery vertical"},
    "HK.00005": {"name":"HSBC",         "thesis":"global bank + dividend recovery"},
    "HK.09988": {"name":"Alibaba",      "thesis":"e-commerce moat + AI cloud + dividend"},
    "HK.09999": {"name":"NetEase",      "thesis":"gaming franchises + dividend grower"},
    "HK.00939": {"name":"CCB",          "thesis":"big-4 bank + dividend"},
    "HK.00003": {"name":"HK & China Gas","thesis":"utility + dividend"},
    "HK.00016": {"name":"SHK Property", "thesis":"HK property recovery"},
}


# ─── Universe whitelist (US — curated high-quality compounders) ──

US_FUND_WHITELIST = {
    # Mega-cap quality compounders (Q ~70+)
    "US.NVDA","US.AVGO","US.GOOGL","US.META","US.AAPL","US.MSFT",
    "US.AMZN","US.LLY","US.NVO","US.UNH","US.JNJ","US.ABBV",
    # AI infrastructure picks-and-shovels
    "US.ANET","US.VRT","US.PLTR","US.AMD","US.MU","US.SMCI","US.ARM",
    # Compounders + secular growth
    "US.COST","US.WMT","US.SPGI","US.MA","US.V","US.NOW","US.CRM",
    "US.CRWD","US.SHOP","US.UBER","US.NFLX","US.ISRG","US.TMUS",
    # Healthcare picks
    "US.REGN","US.VRTX","US.AMGN","US.BMY","US.MRK",
    # Cyclicals / quality value
    "US.JPM","US.GS","US.HD","US.TSM","US.QCOM","US.CAT",
    # Financials / fintech
    "US.SCHW","US.AXP","US.BLK","US.PYPL","US.HOOD",
    # Energy / materials picks
    "US.XOM","US.OXY","US.CVX","US.FCX",
    # ETFs allowed for hedging/exposure (no fundamentals required)
    "US.SPY","US.QQQ","US.IWM","US.XLK","US.XLF","US.XLE","US.GLD","US.TLT",
}


# ─── Gate decision ──────────────────────────────────────────────

def evaluate_fundamentals(code: str) -> Tuple[bool, str, Dict]:
    """
    Run the fundamental gate.

    Returns (passed, reason, fund_snapshot).
    """
    sym = code.split(".", 1)[1] if "." in code else code

    # 1. Universe check
    if code.startswith("HK."):
        if code not in HK_FUND_WHITELIST:
            return False, f"HK whitelist: {code} not approved", {}
        return True, f"HK whitelist: {HK_FUND_WHITELIST[code]['thesis']}", {
            "whitelisted": True, "thesis": HK_FUND_WHITELIST[code]["thesis"],
        }

    # ETFs bypass fundamentals (used for hedging / exposure)
    if code in US_FUND_WHITELIST and code in {"US.SPY","US.QQQ","US.IWM","US.XLK","US.XLF","US.XLE","US.GLD","US.TLT"}:
        return True, "ETF — fundamentals not applicable", {"whitelisted": True}

    if code not in US_FUND_WHITELIST:
        return False, f"US universe: {code} not in whitelist", {}

    # 2. Fundamentals fetchable
    f = get_fundamentals(code)
    if not f:
        return False, "no yfinance fundamentals returned", {}

    # 3. Quality score
    q = compute_quality_score(f)
    if q is None:
        return False, "quality score not computable", f
    if q < MIN_QUALITY:
        return False, f"quality {q:.2f} < min {MIN_QUALITY}", f

    # 4. Revenue growth
    rg = f.get("rev_growth")
    if rg is not None and rg < MIN_REV_GROWTH:
        # Exception: mature compounders w/ high ROE can have lower growth
        roe = f.get("roe") or 0
        if roe < 0.15:
            return False, f"rev growth {rg*100:.1f}% < min {MIN_REV_GROWTH*100:.0f}% (and ROE {roe*100:.1f}%)", f

    # 5. Operating margin floor
    om = f.get("op_margin")
    if om is not None and om < MIN_OP_MARGIN:
        return False, f"op margin {om*100:.1f}% < floor {MIN_OP_MARGIN*100:.0f}%", f

    # 6. Forward P/E sanity (with high-quality, high-growth exception)
    fpe = f.get("forward_pe")
    if fpe is not None and fpe > 0:
        is_profitable = (om or 0) > 0
        cap = MAX_FWD_PE_PROFIT if is_profitable else MAX_FWD_PE_UNPROFIT
        # High-conviction exception: Q ≥ 0.80 + rev growth ≥ 30% earns +25 P/E room
        if q >= 0.80 and (rg or 0) >= 0.30:
            cap = cap + 25
        if fpe > cap:
            return False, f"fwd P/E {fpe:.0f} > cap {cap:.0f}", f

    # 7. Insider ownership (mega-cap exception)
    ins = f.get("insider_ownership")
    if ins is not None and ins < MIN_INSIDER_OWN:
        # Exception for mega-caps (>$200B): insider ownership naturally low
        mc = f.get("market_cap") or 0
        if mc < 200_000_000_000:
            return False, f"insider ownership {ins*100:.1f}% < min {MIN_INSIDER_OWN*100:.0f}%", f

    # 8. Earnings blackout
    next_earn = f.get("next_earnings_date")
    if next_earn:
        try:
            ne_str = next_earn.split(" ")[0] if isinstance(next_earn, str) else str(next_earn)
            ne_date = datetime.fromisoformat(ne_str[:10]).date()
            days_to = (ne_date - date.today()).days
            if 0 <= days_to <= EARNINGS_BLACKOUT_DAYS:
                return False, f"earnings blackout: {days_to}d to next report", f
        except Exception:
            pass

    return True, f"PASSED — Q={q:.2f}, rev_g={rg*100 if rg else 0:.0f}%, om={om*100 if om else 0:.0f}%", f


def should_enter(code: str, side: str = "LONG") -> Tuple[bool, str]:
    """
    Convenience wrapper: should the bot enter this position based on
    fundamentals?

    SHORTS bypass the gate (shorts are typically hedges/index-based and
    don't need 'quality' — they need a thesis-break or hedge mandate).
    """
    if side != "LONG":
        return True, "SHORT — fundamental gate bypassed (hedge/short trade)"
    passed, reason, _ = evaluate_fundamentals(code)
    return passed, reason
