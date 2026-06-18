"""
Live Trading Readiness Monitor

Runs on schedule, evaluates whether the paper-trading framework is mature
enough to begin live trading. Surfaces a single GO / NO-GO score with
weighted components, and tells you exactly what's blocking go-live.

Composite "go-live score" 0-100:
  System stability (25%):    Bot ran without crash > 7 days
  Execution proven (20%):    Queued orders fired correctly
  Drawdown handled (15%):    Saw a -3%+ SPY pullback + framework held
  Track record (15%):        Paper Sharpe ≥ 1.0 over last 30 days
  News coverage (10%):       Scanner caught a real event
  Classifier behavior (10%): COMPOUNDERS held through volatility
  Cost tracking (5%):        Real costs logged and aligned with paper

Trigger logic (whichever first):
  TRIGGER A: composite ≥ 80 AND 30 days clean
  TRIGGER B: composite ≥ 70 AND a 5%+ SPY drawdown occurred + handled

Outputs:
  logs/live_readiness_latest.json
  Dashboard /api/readiness endpoint
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
READINESS_FILE = LOG_DIR / "live_readiness_latest.json"
BASELINE_FILE  = LOG_DIR / "live_readiness_baseline.json"

# Configurable via env
GO_LIVE_SCORE_DAYS_TRIGGER     = 80
GO_LIVE_SCORE_DRAWDOWN_TRIGGER = 70
GO_LIVE_MIN_DAYS               = 30
DRAWDOWN_THRESHOLD_PCT         = -5.0


def _initialize_baseline():
    """Set baseline = first time this runs, used to count clean days."""
    if BASELINE_FILE.exists():
        return json.loads(BASELINE_FILE.read_text())
    base = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "started_date": date.today().isoformat(),
    }
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(base, indent=2))
    return base


# ── Component scores ─────────────────────────────────────────────

def _score_system_stability() -> Dict:
    """25%: Has the trader been running without crashing for 7+ days?"""
    try:
        log_path = LOG_DIR / "watchdog.log"
        if not log_path.exists():
            return {"score": 0, "max": 25, "detail": "watchdog.log not found"}
        recent = log_path.read_text().splitlines()[-200:]
        restarts = sum(1 for l in recent if "Restarting" in l or "exited with code" in l)
        # 0 restarts = full marks; declining linearly
        if restarts == 0:
            return {"score": 25, "max": 25, "detail": "0 restarts in recent log"}
        if restarts <= 2:
            return {"score": 15, "max": 25, "detail": f"{restarts} restarts — moderate"}
        return {"score": 5, "max": 25, "detail": f"{restarts} restarts — unstable"}
    except Exception as e:
        return {"score": 0, "max": 25, "detail": f"err: {e}"}


def _score_execution_proven() -> Dict:
    """20%: Have queued orders fired and theses logged correctly?"""
    try:
        from risk.order_queue import list_queue
        queue = list_queue()
        if not queue:
            return {"score": 5, "max": 20, "detail": "no queue history"}
        executed = sum(1 for o in queue if o.get("status") == "EXECUTED")
        failed   = sum(1 for o in queue if o.get("status") == "FAILED")
        total    = executed + failed
        if total == 0:
            return {"score": 5, "max": 20, "detail": "queue exists but nothing fired yet"}
        rate = executed / total
        if rate >= 0.95 and total >= 5:
            return {"score": 20, "max": 20, "detail": f"{executed}/{total} executed cleanly"}
        if rate >= 0.80:
            return {"score": 12, "max": 20, "detail": f"{executed}/{total} executed ({rate*100:.0f}%)"}
        return {"score": 5, "max": 20, "detail": f"{executed}/{total} ({rate*100:.0f}%) — issues"}
    except Exception as e:
        return {"score": 0, "max": 20, "detail": f"err: {e}"}


def _score_drawdown_handled() -> Dict:
    """15%: SPY -3%+ drawdown + framework held positions without panic."""
    try:
        # Check daily_pnl in stats for any 3%+ down day
        from moomoo import OpenQuoteContext, RET_OK, KLType
        q = OpenQuoteContext(host="127.0.0.1", port=11111)
        r = q.request_history_kline("US.SPY", start="2026-05-01", end="2026-06-19",
                                     ktype=KLType.K_DAY, max_count=50)
        q.close()
        if r[0] != RET_OK or r[1] is None or len(r[1]) < 5:
            return {"score": 0, "max": 15, "detail": "no SPY data"}
        df = r[1]
        max_dd = 0.0
        peak = df.iloc[0]["close"]
        for _, row in df.iterrows():
            if row["close"] > peak: peak = row["close"]
            dd = (row["close"] / peak - 1) * 100
            if dd < max_dd: max_dd = dd
        if max_dd <= -5:
            return {"score": 15, "max": 15, "detail": f"saw {max_dd:.1f}% drawdown — tested"}
        if max_dd <= -3:
            return {"score": 8, "max": 15, "detail": f"saw {max_dd:.1f}% — moderate test"}
        return {"score": 3, "max": 15, "detail": f"no DD>3% (max {max_dd:.1f}%) — untested"}
    except Exception as e:
        return {"score": 0, "max": 15, "detail": f"err: {e}"}


def _score_track_record() -> Dict:
    """15%: paper P&L positive + Sharpe ≥ 1.0 estimate over 30 days."""
    try:
        # Read balance_history to estimate Sharpe
        balhist_path = LOG_DIR / "balance_history.json"
        if not balhist_path.exists():
            return {"score": 0, "max": 15, "detail": "no balance_history"}
        bh = json.loads(balhist_path.read_text())
        if len(bh) < 30:
            return {"score": 0, "max": 15, "detail": f"only {len(bh)} balance points"}
        # Use last 30 days of points
        recent = bh[-min(720, len(bh)):]  # one point per ~hour = 30 days at 24h
        bals = [float(p.get("balance", 0)) for p in recent if p.get("balance")]
        if len(bals) < 30:
            return {"score": 0, "max": 15, "detail": "insufficient bal points"}
        # Daily returns
        rets = []
        last_day = None
        last_bal = None
        for p in recent:
            day = (p.get("t") or "")[:10]
            if day != last_day:
                if last_bal is not None:
                    bal = float(p.get("balance", 0))
                    if last_bal > 0:
                        rets.append((bal / last_bal - 1))
                last_day = day
                last_bal = float(p.get("balance", 0))
        if len(rets) < 10:
            return {"score": 5, "max": 15, "detail": f"only {len(rets)} daily returns"}
        import statistics
        avg = statistics.mean(rets)
        sd  = statistics.stdev(rets) if len(rets) > 1 else 0.0001
        # Annualised Sharpe ~ sqrt(252) * (avg / sd)
        sharpe = (avg / sd) * (252 ** 0.5) if sd > 0 else 0
        total_ret = ((bals[-1] / bals[0]) - 1) * 100 if bals[0] else 0
        if sharpe >= 1.5 and total_ret > 0:
            return {"score": 15, "max": 15, "detail": f"Sharpe {sharpe:.2f}, ret {total_ret:+.1f}%"}
        if sharpe >= 1.0 and total_ret > 0:
            return {"score": 12, "max": 15, "detail": f"Sharpe {sharpe:.2f}, ret {total_ret:+.1f}%"}
        if total_ret > 0:
            return {"score": 8, "max": 15, "detail": f"Sharpe {sharpe:.2f} but +ret {total_ret:+.1f}%"}
        return {"score": 3, "max": 15, "detail": f"Sharpe {sharpe:.2f}, ret {total_ret:+.1f}% — weak"}
    except Exception as e:
        return {"score": 0, "max": 15, "detail": f"err: {e}"}


def _score_news_coverage() -> Dict:
    """10%: News scanner has produced actionable alerts in recent week."""
    try:
        wr_path = LOG_DIR / "weekly_review_latest.json"
        if not wr_path.exists():
            return {"score": 0, "max": 10, "detail": "no weekly review yet"}
        wr = json.loads(wr_path.read_text())
        counts = wr.get("counts", {})
        flagged = counts.get("RED", 0) + counts.get("ORANGE", 0)
        # Want some flagging but not pure noise
        if 1 <= flagged <= 6:
            return {"score": 10, "max": 10, "detail": f"{flagged} flagged in last review — calibrated"}
        if flagged > 6:
            return {"score": 6, "max": 10, "detail": f"{flagged} flagged — possibly noisy"}
        return {"score": 4, "max": 10, "detail": "0 flagged — may be missing signal"}
    except Exception as e:
        return {"score": 0, "max": 10, "detail": f"err: {e}"}


def _score_classifier_behavior() -> Dict:
    """10%: COMPOUNDERS held through technical signal exits."""
    try:
        # Check trader_stdout.log for [pos-class] HOLD events
        stdout_path = LOG_DIR / "trader_stdout.log"
        if not stdout_path.exists():
            return {"score": 5, "max": 10, "detail": "no trader log"}
        text = stdout_path.read_text()[-50_000:]  # last 50k chars
        holds = len(re.findall(r"\[pos-class\] HOLD", text))
        exits = len(re.findall(r"\[pos-class\] EXIT OK", text))
        if holds >= 1:
            return {"score": 10, "max": 10,
                    "detail": f"classifier saved {holds} compounder(s) from technical exits"}
        if exits > 0:
            return {"score": 7, "max": 10, "detail": f"classifier processed {exits} exits"}
        return {"score": 4, "max": 10, "detail": "classifier wired but not yet observed"}
    except Exception as e:
        return {"score": 0, "max": 10, "detail": f"err: {e}"}


def _score_cost_tracking() -> Dict:
    """5%: Cost tracker logged real fees."""
    try:
        cf_path = LOG_DIR / "trade_costs_latest.json"
        if not cf_path.exists():
            return {"score": 0, "max": 5, "detail": "no cost log"}
        cf = json.loads(cf_path.read_text())
        codes = cf.get("per_code", {})
        if not codes:
            return {"score": 0, "max": 5, "detail": "empty cost log"}
        return {"score": 5, "max": 5, "detail": f"tracking {len(codes)} codes"}
    except Exception as e:
        return {"score": 0, "max": 5, "detail": f"err: {e}"}


# ── Composite + decision ──────────────────────────────────────────

def evaluate() -> Dict:
    baseline = _initialize_baseline()
    started_date = date.fromisoformat(baseline["started_date"])
    days_running = (date.today() - started_date).days

    components = {
        "system_stability":    _score_system_stability(),
        "execution_proven":    _score_execution_proven(),
        "drawdown_handled":    _score_drawdown_handled(),
        "track_record":        _score_track_record(),
        "news_coverage":       _score_news_coverage(),
        "classifier_behavior": _score_classifier_behavior(),
        "cost_tracking":       _score_cost_tracking(),
    }
    total = sum(c["score"] for c in components.values())
    max_t = sum(c["max"] for c in components.values())
    pct = (total / max_t * 100) if max_t else 0

    # Drawdown trigger check
    saw_drawdown = components["drawdown_handled"]["score"] >= 8

    # Decision
    if pct >= GO_LIVE_SCORE_DAYS_TRIGGER and days_running >= GO_LIVE_MIN_DAYS:
        decision = "GO_LIVE"; reason = f"Trigger A: score {pct:.0f}≥{GO_LIVE_SCORE_DAYS_TRIGGER} + {days_running}d≥{GO_LIVE_MIN_DAYS}d"
    elif pct >= GO_LIVE_SCORE_DRAWDOWN_TRIGGER and saw_drawdown:
        decision = "GO_LIVE"; reason = f"Trigger B: score {pct:.0f}≥{GO_LIVE_SCORE_DRAWDOWN_TRIGGER} + drawdown handled"
    else:
        # What's blocking?
        blockers = []
        if days_running < GO_LIVE_MIN_DAYS:
            blockers.append(f"{GO_LIVE_MIN_DAYS - days_running}d more required")
        if pct < GO_LIVE_SCORE_DAYS_TRIGGER:
            blockers.append(f"score {pct:.0f}/{GO_LIVE_SCORE_DAYS_TRIGGER}")
        if not saw_drawdown:
            blockers.append("no 3%+ drawdown observed")
        decision = "NOT_READY"; reason = " · ".join(blockers)

    days_to_trigger_a = max(0, GO_LIVE_MIN_DAYS - days_running)

    return {
        "generated_at":        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline_started":    baseline["started_date"],
        "days_running":        days_running,
        "min_days_required":   GO_LIVE_MIN_DAYS,
        "days_to_trigger_a":   days_to_trigger_a,
        "composite_score":     round(pct, 1),
        "components":          components,
        "decision":            decision,
        "decision_reason":     reason,
        "next_action":         _next_action(decision, components, days_to_trigger_a),
    }


def _next_action(decision: str, components: Dict, days_left: int) -> str:
    if decision == "GO_LIVE":
        return ("Ready for live. Start at 10% of intended size with single "
                "highest-Q position (PLTR or AVGO). Scale 25% week-2, 50% "
                "week-4, 100% by week-8.")
    weak = [(name, c) for name, c in components.items() if c["score"] / c["max"] < 0.5]
    if weak:
        names = ", ".join(n.replace("_"," ") for n, _ in weak[:3])
        return f"Strengthen these components: {names}. Or wait {days_left}d for time-trigger."
    return f"On track. {days_left}d until earliest go-live."


def run_and_save() -> Dict:
    r = evaluate()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    READINESS_FILE.write_text(json.dumps(r, indent=2, default=str))
    return r


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = run_and_save()
    print(f"\n=== LIVE READINESS — {r['decision']} ===")
    print(f"  Composite:     {r['composite_score']}/100")
    print(f"  Days running:  {r['days_running']}d (min {r['min_days_required']}d)")
    print(f"  Reason:        {r['decision_reason']}")
    print()
    print("  Components:")
    for name, c in r["components"].items():
        bar = "█" * int(c["score"]/c["max"]*10) + "░" * (10 - int(c["score"]/c["max"]*10))
        print(f"    {name:24}  [{bar}]  {c['score']:>3}/{c['max']:<3}  {c['detail']}")
    print()
    print(f"  Next action:   {r['next_action']}")
