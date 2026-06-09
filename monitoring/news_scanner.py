"""
Headline-driven thesis-breaker scanner.

Pulls last 24h of news per active position via yfinance, scans headlines
for keywords matching that position's thesis-break conditions, and emits
alerts when a hit is found.

Severity model:
  - Keyword class:    NEG_STRONG > NEG_SOFT > NEUTRAL > POS
  - Headline impact:  strong keyword in headline = RED
                       soft keyword in headline + body = ORANGE
                       analyst downgrade / guidance cut = ORANGE-RED
                       SEC / probe / lawsuit = RED

Output: logs/news_alerts_latest.json + appends to thesis_alerts.jsonl
Read by dashboard via /api/alerts (already wired).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yfinance as yf

from data.trade_thesis import get_active_theses

PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
NEWS_LATEST = LOG_DIR / "news_alerts_latest.json"
NEWS_RAW = LOG_DIR / "news_raw.jsonl"

logger = logging.getLogger(__name__)


# ── Keyword taxonomy ───────────────────────────────────────────────

# Strong negative signals — high-impact thesis breakers
NEG_STRONG = {
    "downgrade", "downgraded", "cut to sell", "sell rating",
    "guidance cut", "guidance lowered", "guidance miss", "guides lower",
    "lawsuit", "subpoena", "sec investigat", "probe", "fraud", "doj",
    "ceo resigns", "ceo steps down", "ceo fired", "executive departure",
    "delisted", "going-concern", "restate", "restated",
    "bankruptcy", "chapter 11", "insolvent",
    "ban", "banned", "sanction", "tariff", "export control",
    "recall", "fda reject", "phase 3 fail", "trial halt",
    "data breach", "ransomware", "cyber attack",
    "accounting irregular", "material weakness",
}

# Soft negative — warrants attention but not auto-RED
NEG_SOFT = {
    "miss", "missed estimates", "below estimates", "below consensus",
    "slowing", "slowed", "decelerat", "weak", "weakness",
    "competition", "competitive pressure", "share loss", "lose share",
    "margin pressure", "margin compress", "headwind",
    "delay", "delayed", "postpone", "production issue",
    "layoff", "job cuts", "restructur",
    "warning", "cautious outlook", "trim forecast",
    "short report", "short seller",
    "departure", "resignation",
}

# Positive — counter-signal; reduces deterioration weight
POS = {
    "upgrade", "upgraded", "buy rating", "raised target",
    "beat", "beats estimates", "above consensus",
    "guidance raised", "raises outlook", "raised guidance",
    "acquisition", "acquires", "partnership", "deal",
    "fda approval", "approved", "expansion", "launch",
    "breakthrough", "innovation", "win", "wins",
}


_BOUNDARY_RE_CACHE: Dict[str, re.Pattern] = {}


def _matches(kw: str, text: str) -> bool:
    """Strict word-boundary match. Prevents 'ban' matching 'Bank'."""
    if kw not in _BOUNDARY_RE_CACHE:
        _BOUNDARY_RE_CACHE[kw] = re.compile(
            r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
    return bool(_BOUNDARY_RE_CACHE[kw].search(text))


def _classify_headline(title: str) -> Tuple[str, List[str]]:
    """
    Returns (severity, matched_keywords).
    Severity: RED | ORANGE | YELLOW | POSITIVE | NEUTRAL
    """
    t = (title or "")
    strong_hits = [kw for kw in NEG_STRONG if _matches(kw, t)]
    soft_hits = [kw for kw in NEG_SOFT if _matches(kw, t)]
    pos_hits = [kw for kw in POS if _matches(kw, t)]

    if strong_hits:
        return ("RED", strong_hits)
    if len(soft_hits) >= 2:
        return ("ORANGE", soft_hits)
    if soft_hits:
        return ("YELLOW", soft_hits)
    if pos_hits:
        return ("POSITIVE", pos_hits)
    return ("NEUTRAL", [])


def _strip_prefix(code: str) -> str:
    return code.split(".", 1)[1] if "." in code else code


def _parse_pub_date(item: Dict) -> Optional[datetime]:
    """yfinance news payload is inconsistent — handle both shapes."""
    c = item.get("content") or item
    pub = c.get("pubDate") or c.get("providerPublishTime")
    if not pub:
        return None
    try:
        if isinstance(pub, (int, float)):
            return datetime.fromtimestamp(pub, tz=timezone.utc)
        s = str(pub).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _parse_title(item: Dict) -> Optional[str]:
    c = item.get("content") or item
    return c.get("title") or item.get("title")


def _parse_source(item: Dict) -> str:
    c = item.get("content") or item
    p = c.get("provider") or {}
    return p.get("displayName") or item.get("publisher") or "?"


def _parse_url(item: Dict) -> Optional[str]:
    c = item.get("content") or item
    cu = c.get("canonicalUrl") or c.get("clickThroughUrl") or {}
    return cu.get("url") or item.get("link")


def scan_news_for(code: str, hours: int = 24, max_items: int = 15) -> Dict:
    """Pull recent news for one ticker, classify, return summary."""
    sym = _strip_prefix(code)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        t = yf.Ticker(sym)
        items = (t.news or [])[:max_items]
    except Exception as e:
        logger.warning(f"news scan {sym} failed: {e}")
        return {"code": code, "items": [], "max_severity": "NEUTRAL", "error": str(e)}

    flagged: List[Dict] = []
    severity_rank = {"RED": 4, "ORANGE": 3, "YELLOW": 2, "POSITIVE": 1, "NEUTRAL": 0}
    max_sev = "NEUTRAL"
    for it in items:
        pub = _parse_pub_date(it)
        if pub and pub < cutoff:
            continue
        title = _parse_title(it)
        if not title:
            continue
        sev, hits = _classify_headline(title)
        entry = {
            "title": title,
            "source": _parse_source(it),
            "url": _parse_url(it),
            "pub_at": pub.isoformat() if pub else None,
            "severity": sev,
            "keywords": hits,
        }
        flagged.append(entry)
        if severity_rank.get(sev, 0) > severity_rank.get(max_sev, 0):
            max_sev = sev

    return {
        "code": code,
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_items": len(flagged),
        "max_severity": max_sev,
        "items": flagged,
    }


def scan_all_active() -> Dict:
    """Scan news for every active thesis."""
    theses = get_active_theses()
    summaries: Dict[str, Dict] = {}
    counts = {"RED": 0, "ORANGE": 0, "YELLOW": 0, "POSITIVE": 0, "NEUTRAL": 0}
    for code in theses:
        s = scan_news_for(code)
        summaries[code] = s
        counts[s.get("max_severity", "NEUTRAL")] = counts.get(s.get("max_severity", "NEUTRAL"), 0) + 1

    # Persist raw scan
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with NEWS_RAW.open("a") as f:
        for s in summaries.values():
            f.write(json.dumps(s, default=str) + "\n")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": counts,
        "by_code": summaries,
        "red_alerts": [
            {"code": c, "headlines": [it for it in s["items"] if it["severity"] == "RED"]}
            for c, s in summaries.items() if s.get("max_severity") == "RED"
        ],
        "orange_alerts": [
            {"code": c, "headlines": [it for it in s["items"] if it["severity"] == "ORANGE"]}
            for c, s in summaries.items() if s.get("max_severity") == "ORANGE"
        ],
    }
    NEWS_LATEST.write_text(json.dumps(payload, indent=2, default=str))
    return payload


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = scan_all_active()
    c = p["counts"]
    print(f"\n=== News scan ===")
    print(f"  RED: {c['RED']}  ORANGE: {c['ORANGE']}  YELLOW: {c['YELLOW']}  POS: {c['POSITIVE']}  NEUTRAL: {c['NEUTRAL']}")
    for tier in ("red_alerts", "orange_alerts"):
        for a in p[tier]:
            print(f"\n  [{tier.upper()}] {a['code']}")
            for h in a["headlines"]:
                print(f"     • {h['title'][:90]}  ({h['source']})")
                if h.get("keywords"):
                    print(f"       keywords: {', '.join(h['keywords'])}")
