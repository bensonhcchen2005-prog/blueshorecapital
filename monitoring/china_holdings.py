"""
China A-share holdings monitor.

Reads user's mainland Chinese equity holdings from
logs/china_holdings_input.json, enriches each with:
  - live CNY price (via yfinance .SS / .SZ suffixes)
  - USD equivalent
  - day / 5d / 30d / YTD moves
  - fundamentals (fwd P/E, market cap, dividend, ROE)
  - latest news headlines
  - **alert flags** for material moves or actionable news

Output: logs/china_holdings_enriched.json (dashboard reads this)

Alert triggers:
  🔴 URGENT: price −7%+ single day, negative regulatory/lawsuit keywords
  🟠 WATCH:  price −4% or +7%+ single day, sector-wide move
  🟡 NEWS:   fresh headline (< 24h) even without price move
  🟢 OK:     nothing material

Data limits:
  - Shenzhen/Shanghai news via yfinance is thinner than US names
  - Fundamentals updated ~daily by yfinance (not real-time)
  - CNY→USD FX read from input file, refresh manually
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import yfinance as yf

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
INPUT_FILE  = LOG_DIR / "china_holdings_input.json"
OUTPUT_FILE = LOG_DIR / "china_holdings_enriched.json"


# ── Alert keyword lists (English + limited zh) ────────────────────

URGENT_KEYWORDS = [
    "delisting", "delisted", "halt", "suspend", "fraud", "investigation",
    "raid", "arrest", "indicted", "sanction", "ban", "audit failure",
    "warning letter", "csrc probe", "sec probe", "recall", "reject",
    "退市", "调查", "处罚", "警示", "停牌",
]
WATCH_KEYWORDS = [
    "downgrade", "cut", "miss", "warning", "guidance", "loss",
    "profit warning", "layoff", "restructure", "impairment",
    "下调", "亏损", "减持", "裁员", "预警",
]
POSITIVE_KEYWORDS = [
    "upgrade", "beat", "raise", "record", "order", "contract win",
    "buyback", "dividend hike", "partnership", "acquire",
    "提升", "增持", "订单", "回购", "分红", "合作",
]


# ── Enrichment helpers ────────────────────────────────────────────

def _price_block(yf_ticker: str) -> Dict:
    """Live price + day/5d/30d/YTD change."""
    try:
        t = yf.Ticker(yf_ticker)
        h30 = t.history(period="60d")
        if h30 is None or len(h30) < 2:
            return {"price": None}
        last = float(h30.iloc[-1]["Close"])
        prev = float(h30.iloc[-2]["Close"])
        d1   = (last / prev - 1) * 100
        d5   = (last / float(h30.iloc[-6]["Close"]) - 1) * 100 if len(h30) >= 6 else None
        d30  = (last / float(h30.iloc[0]["Close"]) - 1) * 100
        ytd = None
        try:
            hy = t.history(start="2026-01-02", end="2026-12-31")
            if hy is not None and len(hy) >= 2:
                ytd = (last / float(hy.iloc[0]["Close"]) - 1) * 100
        except Exception:
            pass
        return {
            "price":       round(last, 2),
            "day_chg_pct": round(d1, 2),
            "d5_pct":      round(d5, 2) if d5 is not None else None,
            "d30_pct":     round(d30, 2),
            "ytd_pct":     round(ytd, 1) if ytd is not None else None,
        }
    except Exception as e:
        logger.debug(f"price block failed for {yf_ticker}: {e}")
        return {"price": None}


def _fundamentals_block(yf_ticker: str) -> Dict:
    try:
        info = yf.Ticker(yf_ticker).info
        return {
            "market_cap_cny":  info.get("marketCap"),
            "forward_pe":      info.get("forwardPE"),
            "trailing_pe":     info.get("trailingPE"),
            "ps":              info.get("priceToSalesTrailing12Months"),
            "pb":              info.get("priceToBook"),
            "dividend_yield":  info.get("dividendYield"),
            "roe":             info.get("returnOnEquity"),
            "rev_growth":      info.get("revenueGrowth"),
            "op_margin":       info.get("operatingMargins"),
            "beta":            info.get("beta"),
            "analyst_target":  info.get("targetMeanPrice"),
            "sector":          info.get("sector"),
            "industry":        info.get("industry"),
            "currency":        info.get("currency"),
        }
    except Exception:
        return {}


def _news_block(yf_ticker: str) -> List[Dict]:
    try:
        t = yf.Ticker(yf_ticker)
        news = (t.news or [])[:8]
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
            pub_iso = datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat()[:16] if pub_ts else "—"

            title_l = title.lower()
            sentiment = None
            for kw in URGENT_KEYWORDS:
                if kw.lower() in title_l:
                    sentiment = ("urgent", kw); break
            if not sentiment:
                for kw in WATCH_KEYWORDS:
                    if kw.lower() in title_l:
                        sentiment = ("watch", kw); break
            if not sentiment:
                for kw in POSITIVE_KEYWORDS:
                    if kw.lower() in title_l:
                        sentiment = ("positive", kw); break

            out.append({
                "title":     title[:140],
                "provider":  provider,
                "published": pub_iso,
                "sentiment": sentiment[0] if sentiment else None,
                "keyword":   sentiment[1] if sentiment else None,
                "age_hours": (datetime.now(timezone.utc).timestamp() - pub_ts) / 3600 if pub_ts else None,
            })
        return out
    except Exception:
        return []


# ── Alert flag generation ─────────────────────────────────────────

def _generate_alerts(pb: Dict, news: List[Dict], code: str, company: str) -> List[Dict]:
    alerts = []
    d1 = pb.get("day_chg_pct")

    # Price-move alerts
    if d1 is not None:
        if d1 <= -7:
            alerts.append({
                "severity": "URGENT",
                "type": "price_drop",
                "detail": f"{code} ({company}) dropped {d1:.1f}% today — investigate immediately",
            })
        elif d1 <= -4:
            alerts.append({
                "severity": "WATCH",
                "type": "price_drop",
                "detail": f"{code} down {d1:.1f}% today — review reason",
            })
        elif d1 >= 7:
            alerts.append({
                "severity": "WATCH",
                "type": "price_spike",
                "detail": f"{code} up {d1:.1f}% today — check for news / consider trim if speculative",
            })

    # News-based alerts (only fresh <24h news matters for "act immediately")
    for n in news[:5]:
        if n.get("sentiment") == "urgent" and (n.get("age_hours") or 999) < 48:
            alerts.append({
                "severity": "URGENT",
                "type": "urgent_news",
                "detail": f"{code}: {n['title']}",
                "keyword": n.get("keyword"),
            })
        elif n.get("sentiment") == "watch" and (n.get("age_hours") or 999) < 24:
            alerts.append({
                "severity": "WATCH",
                "type": "negative_news",
                "detail": f"{code}: {n['title']}",
                "keyword": n.get("keyword"),
            })

    return alerts


# ── Main enrichment ───────────────────────────────────────────────

def enrich_position(h: Dict, fx: float) -> Dict:
    yf_t = h["yf_ticker"]
    shares = h.get("shares")
    cost_cny = h.get("cost_basis_cny")

    pb = _price_block(yf_t)
    fund = _fundamentals_block(yf_t)
    news = _news_block(yf_t)
    alerts = _generate_alerts(pb, news, h["code"], h["company"])

    px = pb.get("price")
    mv_cny = px * shares if (px and shares) else None
    mv_usd = mv_cny / fx if mv_cny else None
    pnl_cny = (px - cost_cny) * shares if (px and cost_cny and shares) else None
    pnl_pct = ((px / cost_cny - 1) * 100) if (px and cost_cny) else None

    overall_sev = "GREEN"
    if any(a["severity"] == "URGENT" for a in alerts):
        overall_sev = "URGENT"
    elif any(a["severity"] == "WATCH" for a in alerts):
        overall_sev = "WATCH"
    elif any(n.get("age_hours", 999) < 24 for n in news):
        overall_sev = "NEWS"

    return {
        "code":       h["code"],
        "yf_ticker":  yf_t,
        "company":    h["company"],
        "name_zh":    h.get("name_zh"),
        "category":   h.get("category"),
        "shares":     shares,
        "cost_basis_cny": cost_cny,
        "current_price_cny": px,
        "day_chg_pct":       pb.get("day_chg_pct"),
        "d5_pct":            pb.get("d5_pct"),
        "d30_pct":           pb.get("d30_pct"),
        "ytd_pct":           pb.get("ytd_pct"),
        "market_val_cny":    round(mv_cny, 2) if mv_cny else None,
        "market_val_usd":    round(mv_usd, 2) if mv_usd else None,
        "unrealized_pnl_cny": round(pnl_cny, 2) if pnl_cny is not None else None,
        "unrealized_pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
        "fundamentals":      fund,
        "latest_news":       news[:5],
        "alerts":            alerts,
        "overall_severity":  overall_sev,
    }


def run() -> Dict:
    if not INPUT_FILE.exists():
        return {"error": "no input file"}
    inp = json.loads(INPUT_FILE.read_text())
    fx = inp.get("fx_cny_per_usd", 7.15)

    positions = [enrich_position(h, fx) for h in inp.get("holdings", [])]

    # Roll-up
    total_mv_cny = sum(p.get("market_val_cny") or 0 for p in positions)
    total_mv_usd = total_mv_cny / fx if total_mv_cny else 0

    all_alerts = []
    for p in positions:
        for a in p.get("alerts", []):
            a["code"] = p["code"]
            a["company"] = p["company"]
            all_alerts.append(a)

    # Sort alerts by severity: URGENT > WATCH > NEWS
    sev_rank = {"URGENT": 0, "WATCH": 1, "NEWS": 2, "GREEN": 3}
    all_alerts.sort(key=lambda a: sev_rank.get(a["severity"], 9))

    counts = {
        "URGENT": sum(1 for p in positions if p["overall_severity"] == "URGENT"),
        "WATCH":  sum(1 for p in positions if p["overall_severity"] == "WATCH"),
        "NEWS":   sum(1 for p in positions if p["overall_severity"] == "NEWS"),
        "GREEN":  sum(1 for p in positions if p["overall_severity"] == "GREEN"),
    }

    return {
        "generated_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account_label":  inp.get("account_label", "China A-shares"),
        "fx_cny_per_usd": fx,
        "total_mv_cny":   round(total_mv_cny, 2),
        "total_mv_usd":   round(total_mv_usd, 2),
        "position_count": len(positions),
        "severity_counts": counts,
        "alerts":         all_alerts[:20],  # top 20 alerts
        "positions":      positions,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = run()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nChina holdings: {out.get('position_count')} positions")
    print(f"Severity: 🔴 {out['severity_counts']['URGENT']} · 🟠 {out['severity_counts']['WATCH']} · 🟡 {out['severity_counts']['NEWS']} · 🟢 {out['severity_counts']['GREEN']}")
    print(f"Total alerts: {len(out.get('alerts',[]))}")
    for a in out.get('alerts', [])[:5]:
        print(f"  [{a['severity']}] {a['detail'][:120]}")
