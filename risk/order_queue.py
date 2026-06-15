"""
Order Queue — conditional orders that fire when a named gate clears.

Use cases:
  - "Buy $60K GOOGL post-FOMC if non-hawkish"
  - "Close JNJ after Jun 17 macro event"
  - "Add UVIX 200 shares if VIX < 18"

Each queued order has:
  - id (slug)
  - code, side, qty
  - trigger_type: AFTER_DATE | AFTER_EVENT | CONDITIONAL
  - trigger_payload: date / event name / condition expression
  - status: PENDING | TRIGGERED | EXECUTED | CANCELLED | FAILED
  - rationale, target_price, stop_price (carried into thesis on fill)

Storage: logs/order_queue.json (one canonical state file)
Audit:   logs/order_queue.jsonl (append-only event log)
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
QUEUE_FILE = PROJECT_ROOT / "logs" / "order_queue.json"
QUEUE_LOG  = PROJECT_ROOT / "logs" / "order_queue.jsonl"


@dataclass
class QueuedOrder:
    id:               str
    code:             str
    side:             str          # BUY | SELL | SHORT | COVER
    qty:              int
    trigger_type:     str          # AFTER_DATE | AFTER_EVENT | CONDITIONAL
    trigger_payload:  Dict
    status:           str = "PENDING"
    rationale:        str = ""
    target_price:     Optional[float] = None
    stop_price:       Optional[float] = None
    horizon_days:     int = 90
    classification:   str = "CATALYST"  # COMPOUNDER | CATALYST | TACTICAL
    created_at:       str = ""
    triggered_at:     Optional[str] = None
    executed_at:      Optional[str] = None
    fill_price:       Optional[float] = None
    notes:            str = ""


# ── Persistence ──────────────────────────────────────────────────

def _load_queue() -> List[Dict]:
    if not QUEUE_FILE.exists():
        return []
    try:
        return json.loads(QUEUE_FILE.read_text())
    except Exception:
        return []


def _save_queue(orders: List[Dict]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(orders, indent=2, default=str))


def _audit(event: str, order: Dict) -> None:
    QUEUE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE_LOG.open("a") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "order": order,
        }, default=str) + "\n")


# ── Public API ───────────────────────────────────────────────────

def enqueue(order: QueuedOrder) -> Dict:
    """Add a new order to the queue."""
    queue = _load_queue()
    d = asdict(order)
    if not d["created_at"]:
        d["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    queue.append(d)
    _save_queue(queue)
    _audit("enqueued", d)
    return d


def list_queue(status_filter: Optional[str] = None) -> List[Dict]:
    queue = _load_queue()
    if status_filter:
        return [o for o in queue if o.get("status") == status_filter]
    return queue


def cancel(order_id: str, reason: str = "") -> bool:
    queue = _load_queue()
    for o in queue:
        if o["id"] == order_id and o["status"] == "PENDING":
            o["status"] = "CANCELLED"
            o["notes"] = (o.get("notes","") + f" | cancelled: {reason}").strip(" |")
            _save_queue(queue)
            _audit("cancelled", o)
            return True
    return False


# ── Trigger evaluation ────────────────────────────────────────────

def _check_after_date(payload: Dict) -> bool:
    """trigger_payload = {after: 'YYYY-MM-DD'}"""
    after = payload.get("after")
    if not after:
        return False
    try:
        target = datetime.fromisoformat(after).date()
        return date.today() > target
    except Exception:
        return False


def _check_after_event(payload: Dict) -> bool:
    """
    trigger_payload = {event: 'FOMC', date: 'YYYY-MM-DD', requires: 'non_hawkish'?}

    Event must be past + optional condition.
    """
    event_date = payload.get("date")
    if not event_date:
        return False
    try:
        target = datetime.fromisoformat(event_date).date()
        if date.today() <= target:
            return False
    except Exception:
        return False
    # Optional requires check (e.g. SPY > X% move post-event)
    requires = payload.get("requires")
    if requires == "non_hawkish":
        # Heuristic: if SPY closed >0 on event day, treat as non-hawkish
        try:
            from moomoo import OpenQuoteContext, KLType, RET_OK
            q = OpenQuoteContext(host="127.0.0.1", port=11111)
            r = q.request_history_kline("US.SPY",
                                        start=event_date, end=event_date,
                                        ktype=KLType.K_DAY, max_count=5)
            q.close()
            if r[0] == RET_OK and r[1] is not None and len(r[1]) > 0:
                df = r[1]
                day_chg = (df.iloc[-1]["close"] / df.iloc[-1]["open"] - 1) * 100
                return day_chg >= 0  # SPY up = non-hawkish read
        except Exception:
            pass
        return True  # default to trigger if can't evaluate
    return True


def _check_conditional(payload: Dict) -> bool:
    """trigger_payload = {expression: '...'} — extended later."""
    return False


def _can_trigger(order: Dict) -> bool:
    payload = order.get("trigger_payload", {})
    t = order.get("trigger_type")
    if t == "AFTER_DATE":
        return _check_after_date(payload)
    if t == "AFTER_EVENT":
        return _check_after_event(payload)
    if t == "CONDITIONAL":
        return _check_conditional(payload)
    return False


# ── Execution ────────────────────────────────────────────────────

def execute_pending(dry_run: bool = False) -> Dict:
    """
    Walk the queue, fire any orders whose triggers have cleared.
    Returns summary of what was triggered / executed / failed.
    """
    queue = _load_queue()
    summary = {"checked": 0, "triggered": 0, "executed": 0,
               "failed": 0, "skipped": 0, "details": []}

    triggered = []
    for o in queue:
        summary["checked"] += 1
        if o["status"] != "PENDING":
            summary["skipped"] += 1
            continue
        if not _can_trigger(o):
            summary["skipped"] += 1
            continue
        triggered.append(o)
        summary["triggered"] += 1

    if not triggered:
        return summary

    if dry_run:
        for o in triggered:
            summary["details"].append({
                "id": o["id"], "code": o["code"], "side": o["side"],
                "qty": o["qty"], "action": "DRY_RUN — would execute",
            })
        return summary

    # Real execution
    try:
        from moomoo import (OpenSecTradeContext, SecurityFirm, TrdMarket,
                            TrdSide, OrderType, TrdEnv, RET_OK, Currency)
        from data.trade_thesis import build_thesis, log_thesis
    except ImportError as e:
        logger.error(f"Cannot execute — moomoo import failed: {e}")
        return summary

    pwd = os.environ.get("MOOMOO_TRADE_PASSWORD", "")

    # Build per-market ctx
    us_ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US,
                                 host="127.0.0.1", port=11111,
                                 security_firm=SecurityFirm.FUTUINC)
    us_ctx.unlock_trade(pwd)

    for o in triggered:
        try:
            code = o["code"]
            side_map = {"BUY": TrdSide.BUY, "SELL": TrdSide.SELL,
                        "SHORT": TrdSide.SELL, "COVER": TrdSide.BUY}
            trd_side = side_map.get(o["side"])
            if not trd_side:
                o["status"] = "FAILED"
                o["notes"] = f"unknown side: {o['side']}"
                summary["failed"] += 1
                continue

            time.sleep(2.5)
            ret, data = us_ctx.place_order(
                price=0, qty=int(o["qty"]), code=code,
                trd_side=trd_side, order_type=OrderType.MARKET,
                trd_env=TrdEnv.SIMULATE,
                remark=f"queue:{o['id'][:30]}",
            )
            if ret != RET_OK:
                o["status"] = "FAILED"
                o["notes"] = f"order rejected: {data}"
                summary["failed"] += 1
                _audit("execution_failed", o)
                continue

            o["status"] = "EXECUTED"
            o["triggered_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            o["executed_at"]  = o["triggered_at"]
            summary["executed"] += 1
            _audit("executed", o)

            # Log thesis for new long entries
            if o["side"] in ("BUY", "SHORT") and o.get("rationale"):
                try:
                    th = build_thesis(
                        code=code,
                        side="LONG" if o["side"] == "BUY" else "SHORT",
                        qty=int(o["qty"]), entry_price=0,  # will be filled by snapshot
                        signals=[{"name":"queue","detail":o["rationale"][:120]}],
                        qualitative={
                            "thesis_summary": o["rationale"],
                            "moat":[], "tailwinds_narrative":"",
                            "kpis_to_watch":[], "thesis_break_conditions":[],
                        },
                        target_price=o.get("target_price"),
                        stop_price=o.get("stop_price"),
                        time_horizon_days=o.get("horizon_days", 90),
                    )
                    log_thesis(th)
                except Exception as e:
                    logger.warning(f"thesis log failed for {code}: {e}")
        except Exception as e:
            o["status"] = "FAILED"
            o["notes"] = f"exception: {e}"
            summary["failed"] += 1

    us_ctx.close()
    _save_queue(queue)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = execute_pending(dry_run=False)
    print(json.dumps(out, indent=2))
