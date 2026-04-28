"""
Live Order Gate — shadow-mode logger + human-approval queue.

Every would-be live order flows through this gate. The gate's behavior
depends on StageController.state.live_mode:

- live_mode = False (SHADOW):  order is logged to logs/live_shadow.jsonl
  and dropped. Nothing hits the broker. This is the default and the mode
  Stage 1 runs in until scripts/enable_live.py is run.

- live_mode = True (LIVE):     order is written to logs/live_pending_orders.json
  with a 5-minute expiry. A human must call approve_order(trade_id) from the
  dashboard (or CLI) before the auto_trader will actually submit it. Expired
  orders are dropped automatically.

This module NEVER calls broker.place_order() directly. The caller (auto_trader)
is responsible for the actual submission, and only does so for orders that
have status == "approved".
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger("live_gate")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PENDING_PATH = PROJECT_ROOT / "logs" / "live_pending_orders.json"
SHADOW_LOG = PROJECT_ROOT / "logs" / "live_shadow.jsonl"
APPROVED_LOG = PROJECT_ROOT / "logs" / "live_approved.jsonl"
FILLS_LOG = PROJECT_ROOT / "logs" / "live_fills.jsonl"

APPROVAL_TIMEOUT_MIN = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_pending() -> List[Dict[str, Any]]:
    if PENDING_PATH.exists():
        try:
            return json.loads(PENDING_PATH.read_text())
        except Exception as e:
            logger.error(f"Corrupt pending file: {e}")
    return []


def _write_pending(rows: List[Dict[str, Any]]) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(rows, indent=2, default=str))


def _prune_expired(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    now = _now()
    out = []
    for r in rows:
        if r.get("status") != "pending":
            out.append(r)
            continue
        try:
            exp = datetime.fromisoformat(r["expires_at"])
        except Exception:
            exp = now
        if exp > now:
            out.append(r)
        else:
            r["status"] = "expired"
            out.append(r)
            logger.info(f"Order {r['trade_id']} expired (un-approved)")
    return out


def queue_order(
    controller,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    strategy: str,
    thesis: str = "",
    is_option: bool = False,
    option_detail: Optional[Dict[str, Any]] = None,
    signal_score: Optional[float] = None,
) -> Dict[str, Any]:
    """Queue an order. In shadow mode returns the shadow record; in live mode
    returns the pending record. Caller does not see a difference."""
    trade_id = f"LO-{uuid.uuid4().hex[:8]}"
    row = {
        "trade_id": trade_id,
        "ts": _now().isoformat(),
        "expires_at": (_now() + timedelta(minutes=APPROVAL_TIMEOUT_MIN)).isoformat(),
        "symbol": symbol,
        "side": side,
        "qty": float(qty),
        "price": float(price),
        "notional": round(float(qty) * float(price), 2),
        "strategy": strategy,
        "signal_score": signal_score,
        "thesis": thesis,
        "is_option": is_option,
        "option_detail": option_detail or {},
        "stage": controller.state.stage,
        "live_mode": controller.state.live_mode,
        "status": "pending",
    }

    if not controller.state.live_mode:
        # SHADOW: log and drop
        row["status"] = "shadow"
        SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SHADOW_LOG.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        logger.info(f"[SHADOW] {symbol} {side} {qty:g}@{price:.2f} "
                    f"({strategy}) notional=${row['notional']:,.0f} id={trade_id}")
        return row

    # LIVE: enqueue for approval
    rows = _prune_expired(_read_pending())
    rows.append(row)
    _write_pending(rows)
    logger.warning(f"[LIVE-PENDING] {symbol} {side} {qty:g}@{price:.2f} "
                   f"({strategy}) notional=${row['notional']:,.0f} id={trade_id} "
                   f"— awaiting approval (expires in {APPROVAL_TIMEOUT_MIN}min)")
    return row


def list_pending() -> List[Dict[str, Any]]:
    rows = _prune_expired(_read_pending())
    _write_pending(rows)
    now = _now()
    out = []
    for r in rows:
        if r.get("status") == "pending":
            try:
                exp = datetime.fromisoformat(r["expires_at"])
                r["expires_in_s"] = max(0, int((exp - now).total_seconds()))
            except Exception:
                r["expires_in_s"] = 0
            out.append(r)
    return out


def list_approved_ready() -> List[Dict[str, Any]]:
    """Approved orders the executor hasn't picked up yet."""
    rows = _read_pending()
    return [r for r in rows if r.get("status") == "approved"]


def approve_order(trade_id: str, reviewer: str = "dashboard") -> tuple[bool, Optional[Dict[str, Any]]]:
    rows = _prune_expired(_read_pending())
    for r in rows:
        if r["trade_id"] == trade_id and r["status"] == "pending":
            r["status"] = "approved"
            r["approved_by"] = reviewer
            r["approved_at"] = _now().isoformat()
            _write_pending(rows)
            APPROVED_LOG.parent.mkdir(parents=True, exist_ok=True)
            with APPROVED_LOG.open("a") as f:
                f.write(json.dumps(r, default=str) + "\n")
            logger.warning(f"[LIVE-APPROVED] {trade_id} by {reviewer}")
            return True, r
    _write_pending(rows)
    return False, None


def reject_order(trade_id: str, reviewer: str = "dashboard") -> bool:
    rows = _prune_expired(_read_pending())
    hit = False
    for r in rows:
        if r["trade_id"] == trade_id and r["status"] == "pending":
            r["status"] = "rejected"
            r["rejected_by"] = reviewer
            r["rejected_at"] = _now().isoformat()
            hit = True
            break
    _write_pending(rows)
    if hit:
        logger.warning(f"[LIVE-REJECTED] {trade_id} by {reviewer}")
    return hit


def mark_submitted(trade_id: str, broker_order_id: str) -> bool:
    rows = _read_pending()
    for r in rows:
        if r["trade_id"] == trade_id and r["status"] == "approved":
            r["status"] = "submitted"
            r["broker_order_id"] = broker_order_id
            r["submitted_at"] = _now().isoformat()
            _write_pending(rows)
            FILLS_LOG.parent.mkdir(parents=True, exist_ok=True)
            with FILLS_LOG.open("a") as f:
                f.write(json.dumps(r, default=str) + "\n")
            return True
    return False
