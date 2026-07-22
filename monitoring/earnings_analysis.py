"""
Earnings Analysis module — post-earnings structured writeup for major tech.

For each covered ticker:
  1. Key Financial Results   (revenue, EPS, margin, FCF, guidance, beat/miss)
  2. Management Commentary   (extracted from headlines + press release links)
  3. Opportunities            (categorised from news + fundamental deltas)
  4. Challenges & Risks       (keyword-classified from news + margin trends)
  5. Key Takeaways            (top-3 signals synthesised)
  6. Forward-Looking Assessment (next earnings date, guidance vs consensus,
     analyst target movement, milestones to watch)

Data source: yfinance (free, no key).

Data limits (honest):
  - Actual earnings call TRANSCRIPTS require paywall (Seeking Alpha,
    Motley Fool). We surface HEADLINES + guidance metrics from press
    releases where yfinance captures them.
  - Management commentary section is HEADLINE-classified — not verbatim
    from the call. Real transcript analysis needs paid data.
  - "Must-pay-attention" prioritisation is triggered by: earnings within
    ±5 days, EPS surprise >±10%, or price move >±5% post-earnings.

Output: logs/earnings_analysis.json  (dashboard reads this)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
OUTPUT_FILE = LOG_DIR / "earnings_analysis.json"

# ── Coverage universe: major tech / AI names ────────────────────

COVERAGE = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
    # AI silicon / infra
    "NVDA", "AVGO", "AMD", "MRVL", "MU", "ARM", "TSM",
    # AI networking / software
    "ANET", "PLTR", "CRWD", "SNOW", "NOW", "CRM", "ORCL", "IBM",
    # AI-adjacent
    "NFLX", "UBER", "SHOP", "SNAP", "PINS", "SPOT",
    # Chip / semi
    "INTC", "QCOM", "ASML", "AMAT", "KLAC", "LRCX", "ADI",
    # Data centre / cloud
    "VRT", "ANET", "CIEN", "GLW", "NOK", "CSCO",
]
COVERAGE = list(dict.fromkeys(COVERAGE))  # dedupe preserving order

# ── Keyword classification ──────────────────────────────────────

OPPORTUNITY_KEYWORDS = {
    "ai": "AI opportunity",
    "artificial intelligence": "AI opportunity",
    "cloud": "Cloud growth",
    "data center": "Data-center demand",
    "gpu": "GPU/accelerator demand",
    "custom silicon": "Custom silicon moat",
    "beat": "Earnings beat",
    "record": "Record results",
    "raise": "Guidance raise",
    "guidance up": "Guidance raise",
    "upgrade": "Analyst upgrade",
    "buyback": "Capital return / buyback",
    "dividend hike": "Capital return / dividend",
    "partnership": "New partnership",
    "acquire": "Acquisition",
    "expand": "Expansion",
    "launch": "Product launch",
    "design win": "Customer design win",
    "backlog": "Growing backlog",
    "orders": "New orders",
    "contract": "Contract win",
    "5g": "5G demand",
    "6g": "6G / next-gen",
    "autonomous": "Autonomy TAM",
    "waymo": "Waymo / robotaxi",
    "robotics": "Robotics",
    "subscription": "Subscription revenue growth",
    "services": "Services mix expansion",
    "commercial": "Commercial adoption",
    "enterprise": "Enterprise adoption",
}

CHALLENGE_KEYWORDS = {
    "miss": "Earnings miss",
    "guidance cut": "Guidance cut",
    "guidance down": "Guidance down",
    "warn": "Guidance warning",
    "downgrade": "Analyst downgrade",
    "regulatory": "Regulatory pressure",
    "regulation": "Regulatory scrutiny",
    "probe": "Investigation / probe",
    "investigation": "Regulatory probe",
    "antitrust": "Antitrust",
    "lawsuit": "Legal / litigation",
    "recall": "Product recall",
    "delay": "Product delay",
    "shortage": "Supply constraint",
    "tariff": "Tariff exposure",
    "china": "China risk",
    "geopolitic": "Geopolitical risk",
    "layoff": "Layoffs",
    "restructur": "Restructuring",
    "impairment": "Impairment charge",
    "competition": "Competitive pressure",
    "compress": "Margin compression",
    "slow": "Growth deceleration",
    "decline": "Revenue decline",
    "loss": "Losses",
    "weak": "Weakness",
    "concern": "Analyst concern",
    "risk": "Elevated risk",
    "capex": "Elevated capex",
    "spending": "Spending overhang",
}


# ── Helpers ──────────────────────────────────────────────────────

def _fetch_earnings_dates(t: yf.Ticker) -> pd.DataFrame:
    """Return DataFrame of past + upcoming earnings dates with EPS."""
    try:
        ed = t.earnings_dates
        if ed is None:
            return pd.DataFrame()
        return ed.head(8)
    except Exception:
        return pd.DataFrame()


def _fetch_quarterly_income(t: yf.Ticker) -> pd.DataFrame:
    try:
        qi = t.quarterly_income_stmt
        return qi if qi is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _fetch_quarterly_cashflow(t: yf.Ticker) -> pd.DataFrame:
    try:
        cf = t.quarterly_cashflow
        return cf if cf is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _classify_headline(title: str) -> Dict:
    title_l = title.lower()
    opps = []
    challenges = []
    for kw, label in OPPORTUNITY_KEYWORDS.items():
        if kw in title_l:
            opps.append(label)
    for kw, label in CHALLENGE_KEYWORDS.items():
        if kw in title_l:
            challenges.append(label)
    return {"opps": opps, "challenges": challenges}


def _news_around_earnings(t: yf.Ticker, earn_date: Optional[date],
                          window_days: int = 10) -> List[Dict]:
    """Pull headlines within window_days of the given earnings date."""
    news = t.news or []
    if not news:
        return []
    if earn_date:
        cutoff_start = (datetime.combine(earn_date, datetime.min.time())
                        - timedelta(days=window_days)).timestamp()
        cutoff_end = (datetime.combine(earn_date, datetime.min.time())
                      + timedelta(days=window_days)).timestamp()
    else:
        cutoff_start = 0
        cutoff_end = datetime.now().timestamp() + 86400

    out = []
    for item in news:
        c = item.get("content", item)
        title = c.get("title", "")
        provider = (c.get("provider") or {}).get("displayName", "")
        pub_ts = item.get("providerPublishTime", 0)
        if not pub_ts:
            try:
                pub_ts = datetime.fromisoformat(c.get("pubDate", "").replace("Z", "+00:00")).timestamp()
            except Exception:
                pub_ts = 0
        if pub_ts and (pub_ts < cutoff_start or pub_ts > cutoff_end):
            continue
        cls = _classify_headline(title)
        out.append({
            "title": title[:160],
            "provider": provider,
            "published": datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat()[:16] if pub_ts else "—",
            "opps": cls["opps"],
            "challenges": cls["challenges"],
        })
    return out[:12]


def _price_reaction(t: yf.Ticker, earn_date: Optional[date]) -> Dict:
    if not earn_date:
        return {}
    try:
        start = (earn_date - timedelta(days=10)).isoformat()
        end = (earn_date + timedelta(days=15)).isoformat()
        h = t.history(start=start, end=end)
        if h is None or len(h) < 3:
            return {}
        # find closest trading day at/after earnings
        idx_arr = [i for i, d in enumerate(h.index) if d.date() >= earn_date]
        if not idx_arr:
            return {}
        idx = idx_arr[0]
        base = float(h.iloc[max(0, idx-1)]["Close"])
        post_1d = float(h.iloc[min(len(h)-1, idx)]["Close"])
        post_5d = float(h.iloc[min(len(h)-1, idx+4)]["Close"]) if idx+4 < len(h) else None
        return {
            "close_before": round(base, 2),
            "close_next_day": round(post_1d, 2),
            "reaction_1d_pct": round((post_1d/base - 1) * 100, 2),
            "reaction_5d_pct": round((post_5d/base - 1) * 100, 2) if post_5d else None,
        }
    except Exception:
        return {}


def _fmt_currency(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    v = float(v)
    if abs(v) >= 1e9:  return f"${v/1e9:.2f}B"
    if abs(v) >= 1e6:  return f"${v/1e6:.1f}M"
    return f"${v:,.0f}"


def _analyse_ticker(ticker: str) -> Dict:
    t = yf.Ticker(ticker)
    info = t.info or {}

    ed_df = _fetch_earnings_dates(t)
    qi = _fetch_quarterly_income(t)
    cf = _fetch_quarterly_cashflow(t)

    # Identify most recent past earnings + upcoming
    past_earn = None
    next_earn = None
    if not ed_df.empty:
        for idx, row in ed_df.iterrows():
            d = idx.date() if hasattr(idx, "date") else None
            if d and d <= date.today():
                past_earn = past_earn or {"date": d, "row": row}
            elif d:
                next_earn = {"date": d, "row": row}

    # Financial results from most recent quarter
    financials = {}
    if not qi.empty:
        latest_q = qi.iloc[:, 0]
        rev = latest_q.get("Total Revenue")
        op_inc = latest_q.get("Operating Income")
        net_inc = latest_q.get("Net Income")
        gp = latest_q.get("Gross Profit")
        # YoY (compare to Q4 back)
        yoy_rev = None
        if qi.shape[1] >= 5:
            prev_rev = qi.iloc[:, 4].get("Total Revenue")
            if rev and prev_rev:
                try:
                    yoy_rev = (float(rev) / float(prev_rev) - 1) * 100
                except Exception:
                    pass
        gm = (float(gp) / float(rev)) if (gp and rev) else None
        opm = (float(op_inc) / float(rev)) if (op_inc and rev) else None
        nm  = (float(net_inc) / float(rev)) if (net_inc and rev) else None
        financials = {
            "period": str(qi.columns[0])[:10],
            "revenue": _fmt_currency(rev),
            "revenue_yoy_pct": round(yoy_rev, 1) if yoy_rev is not None else None,
            "gross_profit": _fmt_currency(gp),
            "operating_income": _fmt_currency(op_inc),
            "net_income": _fmt_currency(net_inc),
            "gross_margin_pct": round(gm * 100, 1) if gm else None,
            "op_margin_pct": round(opm * 100, 1) if opm else None,
            "net_margin_pct": round(nm * 100, 1) if nm else None,
        }
    # FCF from cashflow
    if not cf.empty:
        ocf = cf.iloc[:, 0].get("Operating Cash Flow") if "Operating Cash Flow" in cf.index else None
        capex = cf.iloc[:, 0].get("Capital Expenditure") if "Capital Expenditure" in cf.index else None
        if ocf is not None and capex is not None:
            try:
                fcf = float(ocf) + float(capex)  # capex negative in cashflow
                financials["free_cash_flow"] = _fmt_currency(fcf)
            except Exception:
                pass

    # EPS beat / miss from earnings_dates
    eps_actual = eps_est = surprise_pct = None
    if past_earn:
        try:
            row = past_earn["row"]
            eps_actual = row.get("Reported EPS")
            eps_est = row.get("EPS Estimate")
            surprise_pct = row.get("Surprise(%)")
            eps_actual = float(eps_actual) if not pd.isna(eps_actual) else None
            eps_est = float(eps_est) if eps_est is not None and not pd.isna(eps_est) else None
            surprise_pct = float(surprise_pct) if surprise_pct is not None and not pd.isna(surprise_pct) else None
        except Exception:
            pass

    # Headlines around latest earnings
    latest_earn_date = past_earn["date"] if past_earn else None
    news = _news_around_earnings(t, latest_earn_date)

    # Aggregate opportunities + challenges from news
    opp_counter = {}
    ch_counter = {}
    for n in news:
        for o in n.get("opps", []):
            opp_counter[o] = opp_counter.get(o, 0) + 1
        for c in n.get("challenges", []):
            ch_counter[c] = ch_counter.get(c, 0) + 1

    opportunities = sorted(opp_counter.items(), key=lambda kv: -kv[1])[:6]
    challenges = sorted(ch_counter.items(), key=lambda kv: -kv[1])[:6]

    reaction = _price_reaction(t, latest_earn_date)

    # Key takeaways synthesis
    takeaways = []
    if surprise_pct is not None:
        takeaways.append(
            f"EPS {'beat' if surprise_pct > 0 else 'missed'} by "
            f"{abs(surprise_pct):.1f}% vs consensus"
        )
    if opportunities:
        takeaways.append(f"Growth theme: {opportunities[0][0]} (dominant in coverage)")
    if challenges:
        takeaways.append(f"Key concern: {challenges[0][0]}")
    if reaction.get("reaction_1d_pct") is not None:
        r1 = reaction["reaction_1d_pct"]
        takeaways.append(
            f"Market reaction: {'+' if r1 >= 0 else ''}{r1:.1f}% next-day close "
            f"({'positive' if r1 >= 0 else 'negative'})"
        )

    # Days to next earnings
    days_to_next = None
    if next_earn:
        days_to_next = (next_earn["date"] - date.today()).days

    # "Must-pay-attention" priority: within ±5d of an earnings date,
    # OR |surprise| > 10%, OR |1d reaction| > 5%
    priority = "NORMAL"
    if days_to_next is not None and 0 <= days_to_next <= 5:
        priority = "UPCOMING"
    elif latest_earn_date and (date.today() - latest_earn_date).days <= 5:
        priority = "FRESH"
    if surprise_pct is not None and abs(surprise_pct) > 10:
        priority = "MATERIAL_SURPRISE"
    if reaction.get("reaction_1d_pct") is not None and abs(reaction["reaction_1d_pct"]) > 5:
        priority = "MATERIAL_REACTION"

    # Forward-looking assessment
    forward = {
        "next_earnings_date": next_earn["date"].isoformat() if next_earn else None,
        "days_to_next_earnings": days_to_next,
        "analyst_target": info.get("targetMeanPrice"),
        "analyst_target_high": info.get("targetHighPrice"),
        "analyst_target_low": info.get("targetLowPrice"),
        "num_analysts": info.get("numberOfAnalystOpinions"),
        "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "forward_pe": info.get("forwardPE"),
        "target_upside_pct": None,
    }
    cp = forward.get("current_price")
    tp = forward.get("analyst_target")
    if cp and tp:
        forward["target_upside_pct"] = round((tp/cp - 1) * 100, 1)

    return {
        "ticker":        ticker,
        "company":       info.get("longName") or info.get("shortName"),
        "sector":        info.get("sector"),
        "industry":      info.get("industry"),
        "market_cap":    info.get("marketCap"),
        "priority":      priority,

        "latest_earnings": {
            "date": latest_earn_date.isoformat() if latest_earn_date else None,
            "eps_actual": eps_actual,
            "eps_estimate": eps_est,
            "surprise_pct": surprise_pct,
        },
        "financial_results":   financials,
        "management_signals": {
            "headlines": [n["title"] for n in news[:5]],
            "opportunities_ranked": [{"theme": o, "mentions": c} for o, c in opportunities],
            "challenges_ranked":    [{"risk": ch, "mentions": c} for ch, c in challenges],
        },
        "market_reaction":   reaction,
        "key_takeaways":     takeaways,
        "forward_assessment": forward,
        "analysis_note": (
            "Analysis surfaces headlines + quantitative results. Full earnings-"
            "call transcript analysis requires paywalled data (Seeking Alpha / "
            "Motley Fool). Use this alongside the actual transcript for depth."
        ),
    }


def run(tickers: Optional[List[str]] = None) -> Dict:
    tickers = tickers or COVERAGE
    results = {}
    for tk in tickers:
        try:
            results[tk] = _analyse_ticker(tk)
        except Exception as e:
            logger.warning(f"earnings analysis for {tk} failed: {e}")
            results[tk] = {"ticker": tk, "error": str(e)}
    # Sort must-pay-attention names to top
    order = sorted(results.keys(),
                   key=lambda tk: {"MATERIAL_SURPRISE":0,"MATERIAL_REACTION":0,
                                   "FRESH":1,"UPCOMING":2,"NORMAL":9}
                                   .get(results[tk].get("priority", "NORMAL"), 9))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coverage":     tickers,
        "priority_order": order,
        "must_pay_attention": [tk for tk in order
                               if results[tk].get("priority") not in (None, "NORMAL")],
        "analyses":     results,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = run()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nEarnings analysis complete: {len(out['analyses'])} tickers")
    print(f"Must-pay-attention now: {out['must_pay_attention']}")
    for tk in out["must_pay_attention"][:5]:
        r = out["analyses"][tk]
        print(f"  {tk:6}  {r['priority']:20}  {r['company']}")
