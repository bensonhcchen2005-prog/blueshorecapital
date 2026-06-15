"""
Weekly Review — Mondays at 7am ET, pull news + earnings + sector data
for every active holding, generate a narrative report flagging anything
material.

Sources:
  - yfinance .news per ticker (last 7 days)
  - yfinance .calendar (upcoming earnings)
  - Quote data (1w/1mo price change)
  - Sector ETF performance (XLK/XLF/XLV/etc.) for relative context

Output:
  - logs/weekly_review_latest.json (machine-readable)
  - logs/weekly_review_<date>.md (human-readable narrative)

Flagged conditions per holding:
  - News headline matches thesis-break keywords (downgrade, miss, recall, fraud)
  - Earnings within 7 days
  - Price down > 10% in week
  - Sector underperforming SPY by > 5%
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yfinance as yf

from data.trade_thesis import get_active_theses
from data.fundamentals import get_fundamentals

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LATEST_JSON = LOG_DIR / "weekly_review_latest.json"

# Sector ETF map (US tickers)
SECTOR_ETF = {
    "Tech":         "XLK",
    "Healthcare":   "XLV",
    "Financials":   "XLF",
    "Energy":       "XLE",
    "Industrials":  "XLI",
    "Consumer":     "XLP",
    "Communication":"XLC",
    "Real estate":  "XLRE",
    "Utilities":    "XLU",
    "Materials":    "XLB",
}

# Map sector tags in thesis → ETF
SECTOR_TO_ETF = {
    "Tech": "XLK", "Healthcare": "XLV", "Financials": "XLF",
    "Fintech": "XLF", "Crypto": "XLK", "Other": "SPY",
    "ETF": "SPY", "Commodity": "SPY",
}

# Thesis-break keywords (lowercase scan)
NEGATIVE_KEYWORDS = [
    "downgrade", "cut to sell", "miss", "warning", "delay",
    "recall", "fraud", "investigation", "lawsuit", "guidance cut",
    "layoff", "ceo departure", "ceo steps down", "indictment",
    "halt", "fda reject", "trial fail", "data disappoint",
    "subpoena", "sec investigation", "antitrust", "tariff",
    "patent loss", "competitor wins",
]
POSITIVE_KEYWORDS = [
    "upgrade", "beat", "raise guidance", "buyback", "dividend hike",
    "merger announce", "acquisition", "fda approve",
    "design win", "contract win", "earnings beat",
    "guidance raise", "outperform", "patent grant",
    "spin off", "split",
]


def _normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip() if s else ""


def _scan_news(news_items: List[Dict]) -> Dict:
    """Score news by sentiment + extract headlines."""
    if not news_items:
        return {"count": 0, "headlines": [], "negative_hits": [], "positive_hits": []}

    headlines = []
    negative = []
    positive = []
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()

    for item in news_items[:20]:
        # yfinance has evolved — try both old and new shape
        content = item.get("content", item)
        title = content.get("title", "")
        # provider
        provider = (content.get("provider") or {}).get("displayName", "")
        # published timestamp
        pub_ts = item.get("providerPublishTime")
        if not pub_ts:
            pub_iso = content.get("pubDate", "")
            try:
                pub_ts = datetime.fromisoformat(pub_iso.replace("Z","+00:00")).timestamp()
            except Exception:
                pub_ts = None
        if pub_ts and pub_ts < cutoff_ts:
            continue

        title_l = _normalize_text(title)
        headlines.append({
            "title": title[:160], "provider": provider,
            "published": pub_ts,
        })

        for kw in NEGATIVE_KEYWORDS:
            if kw in title_l:
                negative.append({"title": title[:120], "keyword": kw})
                break
        for kw in POSITIVE_KEYWORDS:
            if kw in title_l:
                positive.append({"title": title[:120], "keyword": kw})
                break

    return {
        "count": len(headlines),
        "headlines": headlines[:10],
        "negative_hits": negative,
        "positive_hits": positive,
    }


def _get_earnings_date(t: yf.Ticker) -> Optional[date]:
    try:
        cal = t.calendar
        if cal is None:
            return None
        ed = None
        if hasattr(cal, "get"):
            ev = cal.get("Earnings Date")
            if isinstance(ev, list) and ev:
                ed = ev[0]
            elif ev:
                ed = ev
        if ed:
            if hasattr(ed, "date"):
                return ed.date()
            return datetime.fromisoformat(str(ed)[:10]).date()
    except Exception:
        return None
    return None


def _get_price_change(t: yf.Ticker, days: int) -> Optional[float]:
    try:
        hist = t.history(period=f"{days+2}d")
        if hist is None or len(hist) < 2:
            return None
        return (hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100
    except Exception:
        return None


def review_holding(code: str, thesis: Dict) -> Dict:
    """Single-holding weekly review."""
    sym = code.split(".", 1)[1] if "." in code else code

    # HK: skip yfinance news (sparse)
    if code.startswith("HK."):
        return {
            "code": code, "sector": thesis.get("sector"),
            "skip_reason": "HK — limited yfinance coverage",
        }

    t = yf.Ticker(sym)
    news_block = _scan_news(getattr(t, "news", []) or [])

    earnings_date = _get_earnings_date(t)
    days_to_earnings = (earnings_date - date.today()).days if earnings_date else None

    pct_1w = _get_price_change(t, 7)
    pct_1m = _get_price_change(t, 30)

    # Sector relative perf
    sector = thesis.get("sector") or "Other"
    sector_etf = SECTOR_TO_ETF.get(sector, "SPY")
    sector_pct = None
    spy_pct = None
    rel_perf = None
    try:
        sector_t = yf.Ticker(sector_etf)
        sector_hist = sector_t.history(period="9d")
        if sector_hist is not None and len(sector_hist) >= 2:
            sector_pct = (sector_hist["Close"].iloc[-1] / sector_hist["Close"].iloc[0] - 1) * 100
        spy_hist = yf.Ticker("SPY").history(period="9d")
        if spy_hist is not None and len(spy_hist) >= 2:
            spy_pct = (spy_hist["Close"].iloc[-1] / spy_hist["Close"].iloc[0] - 1) * 100
        if sector_pct is not None and spy_pct is not None:
            rel_perf = sector_pct - spy_pct
    except Exception:
        pass

    # Build flags
    flags = []
    if news_block["negative_hits"]:
        flags.append({
            "severity": "ORANGE",
            "tag": "negative_news",
            "detail": f"{len(news_block['negative_hits'])} negative headline(s)",
        })
    if days_to_earnings is not None and 0 <= days_to_earnings <= 7:
        flags.append({
            "severity": "YELLOW",
            "tag": "earnings_imminent",
            "detail": f"earnings in {days_to_earnings}d ({earnings_date})",
        })
    if pct_1w is not None and pct_1w < -10:
        flags.append({
            "severity": "RED" if pct_1w < -15 else "ORANGE",
            "tag": "price_drop_week",
            "detail": f"1w price {pct_1w:+.1f}%",
        })
    if rel_perf is not None and rel_perf < -5:
        flags.append({
            "severity": "YELLOW",
            "tag": "sector_underperform",
            "detail": f"sector {sector_etf} {rel_perf:+.1f}% vs SPY",
        })

    overall = "GREEN"
    if any(f["severity"] == "RED" for f in flags):    overall = "RED"
    elif any(f["severity"] == "ORANGE" for f in flags):overall = "ORANGE"
    elif any(f["severity"] == "YELLOW" for f in flags):overall = "YELLOW"

    return {
        "code": code, "sector": sector, "classification": thesis.get("target",{}).get("horizon_class"),
        "overall_severity": overall,
        "flags": flags,
        "news": news_block,
        "earnings_date": str(earnings_date) if earnings_date else None,
        "days_to_earnings": days_to_earnings,
        "price_1w_pct": round(pct_1w, 2) if pct_1w is not None else None,
        "price_1m_pct": round(pct_1m, 2) if pct_1m is not None else None,
        "sector_1w_pct": round(sector_pct, 2) if sector_pct is not None else None,
        "spy_1w_pct": round(spy_pct, 2) if spy_pct is not None else None,
        "sector_relative_perf_pct": round(rel_perf, 2) if rel_perf is not None else None,
    }


def run_review() -> Dict:
    """Run weekly review across all active holdings. Write outputs."""
    theses = get_active_theses()
    reviews = []
    for code, thesis in theses.items():
        try:
            r = review_holding(code, thesis)
            reviews.append(r)
        except Exception as e:
            logger.warning(f"review failed for {code}: {e}")
            reviews.append({"code": code, "error": str(e)})

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "week_of": date.today().isoformat(),
        "total_reviewed": len(reviews),
        "counts": {
            "RED":    sum(1 for r in reviews if r.get("overall_severity") == "RED"),
            "ORANGE": sum(1 for r in reviews if r.get("overall_severity") == "ORANGE"),
            "YELLOW": sum(1 for r in reviews if r.get("overall_severity") == "YELLOW"),
            "GREEN":  sum(1 for r in reviews if r.get("overall_severity") == "GREEN"),
        },
        "reviews": reviews,
    }

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_JSON.write_text(json.dumps(summary, indent=2, default=str))

    # Markdown narrative
    md_path = LOG_DIR / f"weekly_review_{date.today().isoformat()}.md"
    with md_path.open("w") as f:
        f.write(f"# Weekly Portfolio Review — {date.today().isoformat()}\n\n")
        f.write(f"**Total holdings reviewed:** {len(reviews)}  \n")
        c = summary["counts"]
        f.write(f"**Severity:** 🔴 {c['RED']} · 🟠 {c['ORANGE']} · 🟡 {c['YELLOW']} · 🟢 {c['GREEN']}\n\n")
        f.write("---\n\n")
        for r in sorted(reviews, key=lambda x: {"RED":0,"ORANGE":1,"YELLOW":2,"GREEN":3,None:4}.get(x.get("overall_severity"), 4)):
            if r.get("skip_reason"):
                continue
            f.write(f"## {r['code']} ({r.get('sector','?')}) — {r.get('overall_severity','?')}\n\n")
            if r.get("flags"):
                for fl in r["flags"]:
                    f.write(f"- **{fl['tag']}** ({fl['severity']}): {fl['detail']}\n")
            if r.get("price_1w_pct") is not None:
                f.write(f"- Price: 1w {r['price_1w_pct']:+.1f}% · 1m {r['price_1m_pct']:+.1f}% · "
                        f"Sector {r.get('sector_1w_pct',0):+.1f}% · SPY {r.get('spy_1w_pct',0):+.1f}%\n")
            if r.get("earnings_date"):
                f.write(f"- Next earnings: {r['earnings_date']} ({r.get('days_to_earnings','?')}d away)\n")
            if r.get("news",{}).get("headlines"):
                f.write(f"- Recent headlines:\n")
                for h in r["news"]["headlines"][:5]:
                    f.write(f"  - {h['title']} ({h.get('provider','')})\n")
            f.write("\n")

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = run_review()
    print(f"\n=== Weekly Review ===")
    print(f"  Holdings reviewed: {s['total_reviewed']}")
    c = s["counts"]
    print(f"  RED: {c['RED']}  ORANGE: {c['ORANGE']}  YELLOW: {c['YELLOW']}  GREEN: {c['GREEN']}")
    for r in s["reviews"]:
        sev = r.get("overall_severity")
        if sev in ("RED","ORANGE","YELLOW"):
            print(f"\n  [{sev:6}] {r['code']}")
            for fl in r.get("flags",[]):
                print(f"     • {fl['tag']:25}: {fl['detail']}")
