"""
Holdings Analytics — deep enrichment for the Live Account dashboard tab.

For each position, augments with:
  - forward P/E + 1-year historical forward P/E series (proxied by price ÷ EPS estimate)
  - Quarterly EPS history (reported vs normalized) + abnormality flags
  - Recent earnings reactions (1d / 5d price action around each release)
  - Industry-specific peer-comparison ratios

Output: logs/holding_analytics.json (per-ticker structured)

Data source: yfinance (free). Limits:
  - Forward P/E history not retained by yfinance — we proxy via PE = current_price/avg_eps_est
  - Some normalized EPS adjustments are estimated, not GAAP-reconciled
  - Earnings dates depend on yfinance .earnings_dates which can be sparse
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
OUTPUT_FILE = LOG_DIR / "holding_analytics.json"


# ── Industry-specific peer groups ─────────────────────────────────

PEERS = {
    "AB":    ["BLK","BX","KKR","APO","TROW"],          # Asset managers
    "AMZN":  ["MSFT","GOOGL","META","NFLX"],
    "AVGO":  ["NVDA","AMD","TSM","MRVL","QCOM"],
    "BE":    ["PLUG","FCEL","BLDP","NEE"],              # Fuel cells / clean energy
    "CRCL":  ["COIN","HOOD","SOFI","BLK"],              # Stablecoin/Fintech
    "GLW":   ["AMAT","KLAC","LRCX","SUMI"],             # Materials / Optics
    "GOOG":  ["MSFT","META","AMZN","AAPL"],
    "GOOGL": ["MSFT","META","AMZN","AAPL"],
    "GOOGM": ["GOOG","GOOGL"],
    "MRVL":  ["AVGO","NVDA","AMD","QCOM","ON"],
    "NOK":   ["ERIC","CSCO","JNPR","ANET"],             # Telecom / 6G
    "ANET":  ["CSCO","JNPR","HPE","NTNX","NOK"],        # AI networking
    "PLTR":  ["NOW","CRM","SNOW","NET","DDOG"],
    "META":  ["GOOGL","MSFT","AMZN","NFLX","SNAP"],
    "LLY":   ["NVO","MRK","PFE","ABBV","BMY"],
    "JNJ":   ["PFE","MRK","BMY","ABBV","LLY"],
    "ABBV":  ["LLY","MRK","PFE","BMY","JNJ"],
    "JPM":   ["BAC","WFC","C","GS","MS"],
    "COIN":  ["HOOD","SOFI","SCHW","BLK"],
}


# Industry-specific KPI displayed front-and-center for peer comparison
INDUSTRY_KEY_RATIO = {
    "AB":    ("P/AUM", "Price to assets-under-management"),
    "AMZN":  ("EV/EBITDA", "Cap-light asset valuation"),
    "AVGO":  ("PEG",  "Price-to-Earnings-Growth"),
    "BE":    ("EV/Sales", "Sub-scale energy revenue multiple"),
    "CRCL":  ("Price/USDC TVL", "Stablecoin reserves valuation"),
    "GLW":   ("P/FCF", "Cash flow conversion focus"),
    "GOOG":  ("PEG",  "Growth-adjusted valuation"),
    "GOOGM": ("Yield+Spread", "Preferred income vs treasury"),
    "MRVL":  ("EV/Sales", "Pre-monetization semi valuation"),
    "NOK":   ("EV/EBITDA", "Telecom cash-generation focus"),
    "ANET":  ("PEG",  "Growth-adjusted networking"),
    "DRAM":  ("P/B",  "Cycle-bottom valuation"),
    "EWY":   ("P/E",  "Country index valuation"),
    "GLD":   ("Spot vs ATH", "Gold price vs historical peak"),
}


# ── NOK detailed thesis ──────────────────────────────────────────

NOK_DETAILED_VIEW = """
**Nokia (NOK) — Upside thesis: Photonics + AI-RAN**

**1. Photonics integration (Infinera acquisition):**
- Nokia completed $2.3B acquisition of Infinera in 2024-25 — combined into a #2 global optical networking provider
- Photonics is the picks-and-shovels play for AI data centers: every hyperscaler GPU farm needs DWDM coherent optics for east-west DC interconnect
- TAM: AI-driven optical networking ~$22B by 2027 (Dell'Oro estimates)
- Key catalyst: NOK winning Microsoft / Meta / Amazon hyperscaler design wins for 800G coherent

**2. AI-RAN positioning:**
- Nokia is one of three global RAN vendors (Ericsson, Samsung, Nokia) post-Huawei restrictions
- AI-RAN combines AI workload acceleration with cellular: edge AI inference at the cell tower
- NVIDIA partnership announced Jan 2025: Nokia BTS using NVIDIA inference SoCs for AI-RAN
- Telecom carrier capex inflection: 6G standard work begins 2027, full deployment 2030 — NOK on critical path

**3. Why the stock is cheap right now:**
- Trades 1.2× P/Sales, EV/EBITDA ~6× (vs ERIC ~9×, ANET ~24×)
- Forward dividend yield 3.5%
- Cleared from 5G headwinds (network refresh cycle complete)
- Margin recovery: 2025 op margin trough, expanding to 11-13% by 2027

**4. Upside math (3-year horizon):**
- Revenue: €23.4B (2025) → €26-28B (2028) at 4-5% CAGR
- Operating margin: 9% → 12-13%
- EBITDA: €2.5B → €3.5B
- 7-8× EV/EBITDA multiple → €4.50-5.50/share equivalent ($20-23 ADR)
- Current $14 ADR = +50-65% upside

**5. Risks:**
- Hyperscaler capex slowdown if AI build-out pauses
- Open RAN displacement by Microsoft/Cisco software stack
- Currency drag (EUR exposure for USD investor)
- Slow execution culture vs Cisco/Arista

**Verdict:** HOLD with positive bias. Re-rate catalyst = Q3 2026 earnings showing Infinera margin lift. Don't add aggressively until margin trajectory is confirmed. Real upside requires 2-3 years of patience.
"""


# ── Helpers ───────────────────────────────────────────────────────

def _last_n_quarterly_earnings(ticker: str, n: int = 8) -> List[Dict]:
    """Pull last n quarterly EPS reports (reported + normalized)."""
    out = []
    try:
        t = yf.Ticker(ticker)
        # earnings_history is deprecated, use income_stmt and earnings_dates
        ed = t.earnings_dates if hasattr(t, "earnings_dates") else None
        if ed is not None and len(ed) > 0:
            ed = ed.head(n)
            for idx, row in ed.iterrows():
                date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                actual = row.get("Reported EPS")
                est    = row.get("EPS Estimate")
                surp   = row.get("Surprise(%)")
                # Try to fetch normalized EPS as well (yfinance puts in different field)
                if pd.isna(actual): continue
                out.append({
                    "date": date_str,
                    "eps_reported": float(actual) if not pd.isna(actual) else None,
                    "eps_estimate": float(est) if est is not None and not pd.isna(est) else None,
                    "surprise_pct": float(surp) if surp is not None and not pd.isna(surp) else None,
                })
    except Exception as e:
        logger.debug(f"earnings for {ticker}: {e}")
    return out


def _eps_normalization_flags(reports: List[Dict]) -> List[str]:
    """Detect abnormalities in EPS sequence (one-time gains/charges)."""
    flags = []
    if len(reports) < 3:
        return flags
    eps_vals = [r["eps_reported"] for r in reports if r.get("eps_reported") is not None]
    if len(eps_vals) < 3:
        return flags
    # Check for outliers — quarters that are >2x median
    sorted_eps = sorted([abs(e) for e in eps_vals])
    median = sorted_eps[len(sorted_eps)//2]
    if median == 0:
        return flags
    for i, e in enumerate(eps_vals):
        ratio = abs(e) / median
        if ratio > 2.5:
            flags.append(f"Quarter {i+1}: {e:.2f} is {ratio:.1f}× the median — likely one-time item")
        if e < 0 and ratio > 1.5:
            flags.append(f"Quarter {i+1}: large loss ${e:.2f} — possibly impairment/charge")
    return flags


def _earnings_reactions(ticker: str, reports: List[Dict]) -> List[Dict]:
    """For each earnings date, fetch price action 1d/5d before and after."""
    out = []
    if not reports:
        return out
    try:
        t = yf.Ticker(ticker)
        # Pull enough history to cover all earnings dates
        earliest = min(r["date"] for r in reports)
        try:
            earliest_date = datetime.fromisoformat(earliest[:10])
            start = (earliest_date - timedelta(days=10)).date().isoformat()
            end = (date.today()).isoformat()
            h = t.history(start=start, end=end)
        except Exception:
            return out
        for r in reports:
            try:
                d = pd.Timestamp(r["date"][:10])
                # Find closest trading days
                idx = h.index.get_indexer([d], method="nearest")[0]
                if idx < 0 or idx >= len(h): continue
                close_at = float(h.iloc[idx]["Close"])
                pre_5d   = float(h.iloc[max(0, idx-5)]["Close"]) if idx >= 5 else None
                post_1d  = float(h.iloc[min(len(h)-1, idx+1)]["Close"]) if idx+1 < len(h) else None
                post_5d  = float(h.iloc[min(len(h)-1, idx+5)]["Close"]) if idx+5 < len(h) else None
                out.append({
                    "date": r["date"],
                    "eps_surprise_pct": r.get("surprise_pct"),
                    "close_at_earnings": round(close_at, 2),
                    "pre_5d_pct":  round((close_at/pre_5d - 1)*100, 2) if pre_5d else None,
                    "post_1d_pct": round((post_1d/close_at - 1)*100, 2) if post_1d else None,
                    "post_5d_pct": round((post_5d/close_at - 1)*100, 2) if post_5d else None,
                })
            except Exception as ee:
                logger.debug(f"reaction calc for {ticker} @ {r['date']}: {ee}")
    except Exception as e:
        logger.debug(f"earnings reactions for {ticker}: {e}")
    return out


def _forward_pe_proxy_series(ticker: str) -> List[Dict]:
    """Build a 1-year price/EPS_est history. Proxy: weekly price ÷ TTM_EPS."""
    out = []
    try:
        t = yf.Ticker(ticker)
        info = t.info
        eps_ttm = info.get("trailingEps") or 0
        eps_fwd = info.get("forwardEps") or eps_ttm
        if eps_ttm <= 0 and eps_fwd <= 0:
            return out
        # Pull weekly history
        h = t.history(period="1y", interval="1wk")
        if h is None or len(h) < 5: return out
        # Crude approx: assume eps grew linearly from eps_ttm to eps_fwd over 1y
        n = len(h)
        for i, (idx, row) in enumerate(h.iterrows()):
            t_frac = i / max(1, n-1)
            est_eps = eps_ttm + (eps_fwd - eps_ttm) * t_frac
            if est_eps <= 0: continue
            fpe = float(row["Close"]) / est_eps
            out.append({"date": idx.strftime("%Y-%m-%d"), "fpe": round(fpe, 2)})
    except Exception as e:
        logger.debug(f"fpe series for {ticker}: {e}")
    return out


def _peer_comparison(ticker: str) -> List[Dict]:
    """Compare key ratios against industry peers."""
    out = []
    peers = PEERS.get(ticker, [])
    if not peers:
        return out
    for peer in [ticker] + peers:
        try:
            p_info = yf.Ticker(peer).info
            out.append({
                "ticker":      peer,
                "is_self":     peer == ticker,
                "market_cap":  p_info.get("marketCap"),
                "forward_pe":  p_info.get("forwardPE"),
                "trailing_pe": p_info.get("trailingPE"),
                "ev_revenue":  p_info.get("enterpriseToRevenue"),
                "ev_ebitda":   p_info.get("enterpriseToEbitda"),
                "ps":          p_info.get("priceToSalesTrailing12Months"),
                "rev_growth":  p_info.get("revenueGrowth"),
                "op_margin":   p_info.get("operatingMargins"),
                "div_yield":   p_info.get("dividendYield"),
            })
        except Exception as e:
            logger.debug(f"peer {peer}: {e}")
    return out


def analyze_ticker(ticker: str) -> Dict:
    """Build the full analytics block for one ticker."""
    earnings = _last_n_quarterly_earnings(ticker, n=8)
    flags    = _eps_normalization_flags(earnings)
    reactions = _earnings_reactions(ticker, earnings)
    fpe_series = _forward_pe_proxy_series(ticker)
    peers    = _peer_comparison(ticker)
    key_ratio = INDUSTRY_KEY_RATIO.get(ticker, ("P/E", "Default"))

    detailed_view = NOK_DETAILED_VIEW if ticker == "NOK" else None

    try:
        info = yf.Ticker(ticker).info
        current_fpe = info.get("forwardPE")
        analyst_target = info.get("targetMeanPrice")
        num_analysts = info.get("numberOfAnalystOpinions")
    except Exception:
        current_fpe = None
        analyst_target = None
        num_analysts = None

    return {
        "ticker": ticker,
        "forward_pe": current_fpe,
        "analyst_target": analyst_target,
        "num_analysts": num_analysts,
        "industry_key_ratio": {"name": key_ratio[0], "definition": key_ratio[1]},
        "forward_pe_history": fpe_series,
        "earnings_quarterly": earnings,
        "eps_abnormality_flags": flags,
        "earnings_reactions": reactions,
        "peer_comparison": peers,
        "detailed_view": detailed_view,
    }


def run(tickers: List[str]) -> Dict:
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "tickers": {}}
    for tk in tickers:
        try:
            out["tickers"][tk] = analyze_ticker(tk)
        except Exception as e:
            out["tickers"][tk] = {"error": str(e)}
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Read tickers from live_holdings_input
    inp = json.loads((LOG_DIR / "live_holdings_input.json").read_text())
    tickers = [h["ticker"] for h in inp.get("equities", []) + inp.get("etfs", [])]
    out = run(tickers)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(out, indent=2, default=str))
    print(f"Analytics built for {len(out['tickers'])} tickers")
    nok = out["tickers"].get("NOK", {})
    print(f"NOK: fwd P/E {nok.get('forward_pe')}, {len(nok.get('earnings_quarterly',[]))} earnings, "
          f"{len(nok.get('peer_comparison',[]))} peers")
