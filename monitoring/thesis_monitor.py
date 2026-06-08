"""
Thesis Monitor — runs on a schedule, detects thesis deterioration.

For every active thesis it:
  1. Pulls fresh fundamentals via yfinance
  2. Compares to thesis-entry fundamentals (rev growth, margins, P/E)
  3. Checks current price vs. stop (proximity warning)
  4. Checks earnings date (within 5 days → flag)
  5. Checks today's economic calendar (FOMC/CPI/NFP → flag)
  6. Computes a deterioration_score 0-100
  7. Emits structured alerts to logs/thesis_alerts.jsonl
  8. Updates logs/thesis_alerts_latest.json (dashboard reads this)

Severity tiers:
  GREEN   — all good, no flags
  YELLOW  — soft warning (earnings near, minor margin compression)
  ORANGE  — meaningful deterioration (rev growth fell 30%+, near stop)
  RED     — thesis-breaker hit, immediate review

Use:
  ./venv/bin/python -m monitoring.thesis_monitor
  → writes alerts file, prints summary

Cron-friendly: run hourly during market hours, daily after close.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from data.fundamentals import get_fundamentals
from data.trade_thesis import get_active_theses
from monitoring.econ_calendar import upcoming_events, events_today

PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
ALERTS_LOG = LOG_DIR / "thesis_alerts.jsonl"
ALERTS_LATEST = LOG_DIR / "thesis_alerts_latest.json"

logger = logging.getLogger(__name__)


# ── Deterioration rules ────────────────────────────────────────────

DETERIORATION_RULES = [
    # name, weight, fn(entry_q, current_q, price_chg_pct) -> (triggered_bool, detail_str)
]


def check_rev_growth_decay(entry: Dict, curr: Dict) -> Optional[Dict]:
    """Revenue growth fell >30% relative."""
    e = entry.get("rev_growth")
    c = curr.get("rev_growth")
    if e is None or c is None or abs(e) < 0.05:
        return None
    decay = (e - c) / abs(e)
    if decay > 0.30:
        return {
            "rule": "rev_growth_decay",
            "severity": "ORANGE" if decay < 0.5 else "RED",
            "weight": 25,
            "detail": f"Rev growth {e*100:.1f}% → {c*100:.1f}% ({decay*100:.0f}% drop)",
        }
    return None


def check_margin_compression(entry: Dict, curr: Dict) -> Optional[Dict]:
    """Operating margin compressed >5pp."""
    e = entry.get("op_margin")
    c = curr.get("op_margin")
    if e is None or c is None:
        return None
    drop = e - c
    if drop > 0.05:
        return {
            "rule": "margin_compression",
            "severity": "ORANGE" if drop < 0.10 else "RED",
            "weight": 20,
            "detail": f"Op margin {e*100:.1f}% → {c*100:.1f}% (−{drop*100:.1f}pp)",
        }
    return None


def check_gross_margin_compression(entry: Dict, curr: Dict) -> Optional[Dict]:
    """Gross margin compressed >3pp (slower-moving, so tighter threshold)."""
    e = entry.get("gross_margin")
    c = curr.get("gross_margin")
    if e is None or c is None:
        return None
    drop = e - c
    if drop > 0.03:
        return {
            "rule": "gross_margin_compression",
            "severity": "YELLOW" if drop < 0.06 else "ORANGE",
            "weight": 15,
            "detail": f"Gross margin {e*100:.1f}% → {c*100:.1f}% (−{drop*100:.1f}pp)",
        }
    return None


def check_pe_rerate_up(entry: Dict, curr: Dict) -> Optional[Dict]:
    """Forward P/E rose >50% — thesis maybe priced in."""
    e = entry.get("forward_pe")
    c = curr.get("forward_pe")
    if e is None or c is None or e <= 0:
        return None
    rerate = (c - e) / e
    if rerate > 0.5:
        return {
            "rule": "pe_rerate_up",
            "severity": "YELLOW",
            "weight": 10,
            "detail": f"Fwd P/E {e:.1f} → {c:.1f} (+{rerate*100:.0f}%). Multiple expansion may be done.",
        }
    return None


def check_analyst_target_drop(entry: Dict, curr: Dict) -> Optional[Dict]:
    """Analyst price target dropped >10%."""
    e = entry.get("analyst_price_target")
    c = curr.get("analyst_price_target")
    if e is None or c is None or e <= 0:
        return None
    drop = (e - c) / e
    if drop > 0.10:
        return {
            "rule": "analyst_target_drop",
            "severity": "YELLOW" if drop < 0.20 else "ORANGE",
            "weight": 10,
            "detail": f"Analyst tgt ${e:.0f} → ${c:.0f} (−{drop*100:.0f}%)",
        }
    return None


def check_quality_score_drop(entry: Dict, curr_q: float) -> Optional[Dict]:
    """Quality score (composite) fell >15pp."""
    from data.fundamentals import compute_quality_score
    e = compute_quality_score(entry) if entry else None
    if e is None or curr_q is None:
        return None
    drop = e - curr_q
    if drop > 0.15:
        return {
            "rule": "quality_score_drop",
            "severity": "ORANGE",
            "weight": 15,
            "detail": f"Quality {e:.2f} → {curr_q:.2f} (−{drop:.2f})",
        }
    return None


def check_stop_proximity(thesis: Dict, current_price: Optional[float]) -> Optional[Dict]:
    """Price within 3% of stop — high-risk."""
    if current_price is None:
        return None
    stop = (thesis.get("invalidation") or {}).get("stop_price")
    if not stop:
        return None
    side = thesis.get("side", "LONG")
    distance_pct = (current_price - stop) / stop * 100 if side == "LONG" else (stop - current_price) / stop * 100
    if -3 < distance_pct < 3:  # within 3% of stop in either direction
        return {
            "rule": "near_stop",
            "severity": "RED" if distance_pct < 1 else "ORANGE",
            "weight": 30,
            "detail": f"Price ${current_price:.2f} is {abs(distance_pct):.1f}% from stop ${stop:.2f}",
        }
    return None


def check_earnings_proximity(thesis: Dict, days: int = 5) -> Optional[Dict]:
    """Earnings within N days — heightened risk."""
    next_earn = (thesis.get("quantitative") or {}).get("next_earnings_date")
    if not next_earn:
        # Re-pull from yfinance
        from data.fundamentals import get_fundamentals
        f = get_fundamentals(thesis["code"])
        next_earn = (f or {}).get("next_earnings_date")
    if not next_earn:
        return None
    try:
        # next_earn may be a list-ish string from yfinance
        ne_str = next_earn.split(" ")[0] if isinstance(next_earn, str) else str(next_earn)
        ne_date = datetime.fromisoformat(ne_str[:10]).date()
    except Exception:
        return None
    days_to = (ne_date - date.today()).days
    if 0 <= days_to <= days:
        return {
            "rule": "earnings_imminent",
            "severity": "YELLOW",
            "weight": 15,
            "detail": f"Earnings on {ne_date} ({days_to}d away)",
        }
    return None


def check_thesis_breaker(thesis: Dict, curr: Dict) -> Optional[Dict]:
    """Crude keyword scan against thesis-breaker conditions vs current state.

    e.g. if thesis says 'rev growth deceleration' breaks it AND rev growth fell.
    Light heuristic — flags for review, not auto-action."""
    breakers = (thesis.get("qualitative") or {}).get("thesis_break_conditions", [])
    if not breakers:
        return None
    hits = []
    rg_e = thesis.get("quantitative", {}).get("rev_growth")
    rg_c = curr.get("rev_growth")
    if rg_e and rg_c and (rg_e - rg_c) > 0.20:
        for b in breakers:
            if any(k in b.lower() for k in ("decelerat", "slow", "miss")):
                hits.append(b)
    if hits:
        return {
            "rule": "thesis_breaker_keyword_match",
            "severity": "ORANGE",
            "weight": 20,
            "detail": "Possible thesis-breaker conditions matched: " + " · ".join(hits),
        }
    return None


# ── Per-position evaluation ────────────────────────────────────────

def evaluate_position(thesis: Dict, current_price: Optional[float] = None) -> Dict:
    """Run all rules against one position, return alert dict."""
    code = thesis["code"]
    entry_q = thesis.get("quantitative") or {}
    curr_f = get_fundamentals(code) or {}

    from data.fundamentals import compute_quality_score
    curr_quality = compute_quality_score(curr_f) if curr_f else None

    rules_triggered = []
    for fn in [
        lambda: check_rev_growth_decay(entry_q, curr_f),
        lambda: check_margin_compression(entry_q, curr_f),
        lambda: check_gross_margin_compression(entry_q, curr_f),
        lambda: check_pe_rerate_up(entry_q, curr_f),
        lambda: check_analyst_target_drop(entry_q, curr_f),
        lambda: check_quality_score_drop(entry_q, curr_quality),
        lambda: check_stop_proximity(thesis, current_price),
        lambda: check_earnings_proximity(thesis),
        lambda: check_thesis_breaker(thesis, curr_f),
    ]:
        try:
            r = fn()
            if r:
                rules_triggered.append(r)
        except Exception as e:
            logger.debug(f"Rule failed for {code}: {e}")

    # Aggregate
    weight = sum(r["weight"] for r in rules_triggered)
    has_red = any(r["severity"] == "RED" for r in rules_triggered)
    has_orange = any(r["severity"] == "ORANGE" for r in rules_triggered)
    if has_red:
        severity = "RED"
    elif has_orange:
        severity = "ORANGE"
    elif rules_triggered:
        severity = "YELLOW"
    else:
        severity = "GREEN"

    return {
        "code": code,
        "side": thesis.get("side"),
        "sector": thesis.get("sector"),
        "severity": severity,
        "deterioration_score": min(weight, 100),
        "rules_triggered": rules_triggered,
        "current_quality_score": curr_quality,
        "entry_quality_score": compute_quality_score(entry_q) if entry_q else None,
        "current_price": current_price,
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ── Macro monitor (economic + earnings calendar) ───────────────────

def macro_snapshot() -> Dict:
    """Today's macro environment + upcoming catalysts."""
    today_events = events_today()
    upcoming = upcoming_events(days_ahead=14)
    return {
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "events_today": today_events,
        "tier1_today": [e for e in today_events if e["tier"] == 1],
        "upcoming_14d": upcoming,
        "next_tier1_event": next(
            (e for e in upcoming if e["tier"] == 1), None
        ),
    }


# ── Main run ────────────────────────────────────────────────────────

def run_monitor() -> Dict:
    """Evaluate all active theses, write alerts, return summary."""
    theses = get_active_theses()
    if not theses:
        logger.info("No active theses to monitor.")
        return {"alerts": [], "macro": macro_snapshot()}

    # Try to fetch current prices via moomoo
    prices: Dict[str, float] = {}
    try:
        from moomoo import OpenQuoteContext, RET_OK, KLType
        q = OpenQuoteContext(host="127.0.0.1", port=11111)
        for code in theses:
            r = q.request_history_kline(code, ktype=KLType.K_DAY, max_count=3)
            if r[0] == RET_OK and r[1] is not None and len(r[1]) > 0:
                prices[code] = float(r[1].iloc[-1]["close"])
        q.close()
    except Exception as e:
        logger.warning(f"Could not fetch live prices: {e}")

    alerts = []
    for code, thesis in theses.items():
        try:
            alert = evaluate_position(thesis, prices.get(code))
            alerts.append(alert)
        except Exception as e:
            logger.error(f"Monitor failed for {code}: {e}")

    macro = macro_snapshot()
    summary = {
        "alerts": alerts,
        "macro": macro,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            "RED":    sum(1 for a in alerts if a["severity"] == "RED"),
            "ORANGE": sum(1 for a in alerts if a["severity"] == "ORANGE"),
            "YELLOW": sum(1 for a in alerts if a["severity"] == "YELLOW"),
            "GREEN":  sum(1 for a in alerts if a["severity"] == "GREEN"),
        },
    }

    # Write alerts
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with ALERTS_LOG.open("a") as f:
        for a in alerts:
            f.write(json.dumps(a, default=str) + "\n")
    ALERTS_LATEST.write_text(json.dumps(summary, indent=2, default=str))

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = run_monitor()
    print(f"\n=== Thesis Monitor Run ===")
    c = s["counts"]
    print(f"  RED: {c['RED']}  ORANGE: {c['ORANGE']}  YELLOW: {c['YELLOW']}  GREEN: {c['GREEN']}")
    print()
    for a in sorted(s["alerts"], key=lambda x: {"RED":0,"ORANGE":1,"YELLOW":2,"GREEN":3}[x["severity"]]):
        if a["severity"] == "GREEN":
            continue
        print(f"  [{a['severity']:6}] {a['code']:10} score={a['deterioration_score']:>3}")
        for r in a["rules_triggered"]:
            print(f"          • {r['rule']:30} ({r['severity']}): {r['detail']}")
    print()
    print(f"=== Macro context (today + 14d) ===")
    print(f"  Today: {len(s['macro']['events_today'])} events")
    for e in s["macro"]["events_today"]:
        print(f"    T{e['tier']} {e['event']}: {e['impact']}")
    nxt = s["macro"].get("next_tier1_event")
    if nxt:
        print(f"  Next Tier-1 event: {nxt['date']} — {nxt['event']}")
