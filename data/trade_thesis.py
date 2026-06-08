"""
Trade Thesis Recorder — every position gets a structured thesis:

  {
    code, side, opened_at, size,
    signals:       [technical triggers that fired],
    quantitative:  {pe, ps, rev_growth, margin, quality_score, ...},
    qualitative:   {moat, tailwind, thesis_summary, risks[]},
    target:        {price, % upside, time_horizon},
    invalidation:  {what kills the thesis},
    monitoring:    [KPIs to track],
  }

Written to logs/theses.jsonl (append-only) and indexed by code in
logs/theses_active.json (latest snapshot per open position).

Both are read by the dashboard's new "Active Theses" tab.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from data.fundamentals import get_fundamentals, compute_quality_score

PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
THESES_LOG = LOG_DIR / "theses.jsonl"
THESES_ACTIVE = LOG_DIR / "theses_active.json"

logger = logging.getLogger(__name__)


# ─── Sector-tailwind classification ─────────────────────────────────────

TAILWIND_TAGS = {
    # Physical AI / robotics
    "TSLA": ["physical_ai", "robotics", "autonomy"],
    "NVDA": ["ai_infra", "physical_ai", "robotics"],
    "ABB.SW": ["robotics"],
    "ISRG": ["robotics", "healthtech"],
    # AI infrastructure
    "AVGO": ["ai_infra", "custom_silicon"],
    "AMD": ["ai_infra"],
    "ORCL": ["ai_infra", "cloud"],
    "VRT": ["ai_infra_picks_shovels", "data_center"],
    "ANET": ["ai_infra", "networking"],
    # Stablecoins / crypto infra
    "COIN": ["crypto_infra", "stablecoins"],
    "CRCL": ["stablecoins"],
    "MSTR": ["btc_proxy"],
    # 6G / connectivity
    "QCOM": ["6g", "edge_ai"],
    "ERIC": ["6g", "infrastructure"],
    "NOK": ["6g"],
    # Mega-cap platforms (compounders, lower upside but quality)
    "AAPL": ["compounder", "ecosystem"],
    "GOOGL": ["compounder", "ai_search"],
    "META": ["compounder", "ai_ads"],
    "MSFT": ["compounder", "ai_enterprise"],
    "AMZN": ["compounder", "cloud", "logistics"],
    # Financials
    "JPM": ["financials_compounder"],
    "GS": ["financials"],
    "MS": ["financials"],
    "BAC": ["financials"],
    "C": ["financials"],
    # Healthcare
    "UNH": ["healthcare"],
    "JNJ": ["healthcare_dividend"],
    "LLY": ["glp1", "biotech"],
    "NVO": ["glp1"],
    # Defensives / dividends
    "WMT": ["consumer_staples", "defensive"],
    "PG": ["consumer_staples"],
    "KO": ["consumer_staples"],
    # Energy
    "XOM": ["energy", "dividend"],
    "OXY": ["energy"],
}

SECTOR_OF = {
    "TSLA":"Tech", "NVDA":"Tech", "AVGO":"Tech", "AMD":"Tech",
    "ORCL":"Tech", "ANET":"Tech", "QCOM":"Tech", "AAPL":"Tech",
    "GOOGL":"Tech", "META":"Tech", "MSFT":"Tech", "AMZN":"Tech", "TSM":"Tech",
    "COIN":"Fintech", "MSTR":"Crypto", "CRCL":"Crypto",
    "JPM":"Financials", "GS":"Financials", "MS":"Financials", "BAC":"Financials", "C":"Financials",
    "UNH":"Healthcare", "JNJ":"Healthcare", "LLY":"Healthcare", "NVO":"Healthcare", "ISRG":"Healthcare",
    "WMT":"Consumer", "PG":"Consumer", "KO":"Consumer",
    "XOM":"Energy", "OXY":"Energy",
    "VRT":"Industrials", "ERIC":"Telecom", "NOK":"Telecom",
    "QQQ":"ETF","SPY":"ETF","IWM":"ETF","XLK":"ETF","XLF":"ETF","XLE":"ETF",
}


def get_sector(code: str) -> str:
    sym = code.split(".", 1)[1] if "." in code else code
    return SECTOR_OF.get(sym, "Other")


def get_tailwinds(code: str) -> List[str]:
    sym = code.split(".", 1)[1] if "." in code else code
    return TAILWIND_TAGS.get(sym, [])


# ─── Thesis record builder ──────────────────────────────────────────────

def build_thesis(code: str, side: str, qty: float,
                 entry_price: float,
                 signals: List[Dict],
                 qualitative: Dict,
                 target_price: Optional[float] = None,
                 stop_price: Optional[float] = None,
                 time_horizon_days: int = 60) -> Dict:
    """
    Build a complete thesis record for a new position.

    Pulls fundamentals automatically. The caller supplies the technical
    signals that triggered entry and a qualitative dict explaining the
    bigger-picture thesis.
    """
    f = get_fundamentals(code) or {}
    quality = compute_quality_score(f) if f else None

    upside_pct = None
    if target_price and entry_price:
        upside_pct = round((target_price / entry_price - 1) * 100, 1)

    thesis = {
        "code": code,
        "side": side.upper(),
        "qty": qty,
        "entry_price": entry_price,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "sector": get_sector(code),
        "tailwinds": get_tailwinds(code),

        "signals": signals,        # what fired technically

        "quantitative": {          # snapshot from yfinance
            "market_cap": f.get("market_cap"),
            "forward_pe": f.get("forward_pe"),
            "pe": f.get("pe"),
            "ps": f.get("ps"),
            "ev_ebitda": f.get("ev_ebitda"),
            "rev_growth": f.get("rev_growth"),
            "gross_margin": f.get("gross_margin"),
            "op_margin": f.get("op_margin"),
            "fcf_margin": f.get("fcf_margin"),
            "roe": f.get("roe"),
            "debt_to_equity": f.get("debt_to_equity"),
            "analyst_price_target": f.get("analyst_price_target"),
            "insider_ownership": f.get("insider_ownership"),
            "beta": f.get("beta"),
            "quality_score": quality,
            "fetched_at": f.get("fetched_at"),
        },

        "qualitative": qualitative,  # caller supplies: moat, thesis, risks

        "target": {
            "price": target_price,
            "upside_pct": upside_pct,
            "horizon_days": time_horizon_days,
        },
        "invalidation": {
            "stop_price": stop_price,
            "drawdown_pct": (round((stop_price/entry_price - 1) * 100, 1)
                             if stop_price and entry_price else None),
            "thesis_break_conditions": qualitative.get("thesis_break_conditions", []),
        },
        "monitoring": qualitative.get("kpis_to_watch", []),
    }
    return thesis


def log_thesis(thesis: Dict) -> None:
    """Append to log + update active index."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Append to JSONL
    with THESES_LOG.open("a") as f:
        f.write(json.dumps(thesis, default=str) + "\n")

    # Update active index
    active = {}
    if THESES_ACTIVE.exists():
        try:
            active = json.loads(THESES_ACTIVE.read_text())
        except Exception:
            active = {}
    active[thesis["code"]] = thesis
    THESES_ACTIVE.write_text(json.dumps(active, indent=2, default=str))


def close_thesis(code: str, exit_price: float, exit_reason: str) -> Optional[Dict]:
    """Mark a thesis closed: remove from active index, append closure event."""
    if not THESES_ACTIVE.exists():
        return None
    active = json.loads(THESES_ACTIVE.read_text())
    thesis = active.pop(code, None)
    if thesis:
        thesis["closed_at"] = datetime.now(timezone.utc).isoformat()
        thesis["exit_price"] = exit_price
        thesis["exit_reason"] = exit_reason
        entry = thesis.get("entry_price", 0)
        if entry:
            pnl_pct = (exit_price - entry) / entry * 100
            if thesis["side"] == "SHORT":
                pnl_pct = -pnl_pct
            thesis["realized_pnl_pct"] = round(pnl_pct, 2)
        with THESES_LOG.open("a") as f:
            f.write(json.dumps(thesis, default=str) + "\n")
        THESES_ACTIVE.write_text(json.dumps(active, indent=2, default=str))
    return thesis


def get_active_theses() -> Dict[str, Dict]:
    if not THESES_ACTIVE.exists():
        return {}
    try:
        return json.loads(THESES_ACTIVE.read_text())
    except Exception:
        return {}
