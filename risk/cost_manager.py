"""
Cost Manager — pre-trade fee gates + lifetime trade-cost tracking.

Hooks into auto_trader.py at two points:
  1. PRE-ENTRY: should_block_for_cost() — refuses sub-economic orders
     (e.g. HK orders too small for HK$15 platform fee to make sense)
  2. POST-FILL: record_trade_cost() — appends entry/exit fees + borrow
     to logs/trade_costs.jsonl + updates lifetime tally per position.

The dashboard reads logs/trade_costs.jsonl to show:
  - Per-position fee column
  - "Cost burn" alerts when accumulated cost > 20% of unrealised P&L

This gives us full transparency on the cost side of every trade.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from risk.fees import us_stock_fees, hk_stock_fees, short_borrow_cost

PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
COSTS_LOG = LOG_DIR / "trade_costs.jsonl"
COSTS_LATEST = LOG_DIR / "trade_costs_latest.json"

# Configurable thresholds — keep at sensible defaults, override via env
HK_MIN_NOTIONAL_HKD     = 30_000  # HK$30K min so HK$15 platform fee < 5 bps
US_MIN_NOTIONAL_USD     = 5_000   # avoid silly small US orders
MAX_FEE_RATIO_AT_ENTRY  = 0.0050  # cap entry fees at 50 bps of notional
COST_BURN_ALERT_RATIO   = 0.20    # alert if cost > 20% of unrealised P&L
SHORT_BORROW_ALERT_BPS  = 100     # alert if annualised borrow > 100 bps

logger = logging.getLogger(__name__)


# ── Pre-trade gates ────────────────────────────────────────────────

def should_block_for_cost(code: str, qty: int, price: float,
                          side: str = "BUY") -> Tuple[bool, str]:
    """
    Pre-entry check. Returns (block_decision, reason).
    Called from auto_trader.py before placing an order.
    """
    notional = abs(qty) * price

    if code.startswith("HK."):
        # Convert HKD price * HKD_PER_USD to USD-equivalent if needed
        # price here is in HKD already for HK names
        if notional < HK_MIN_NOTIONAL_HKD:
            return True, f"HK order too small: HK${notional:,.0f} < HK${HK_MIN_NOTIONAL_HKD:,} min"
        fees = hk_stock_fees(qty, price, side)
        fee_ratio = fees["total_hkd"] / notional
        if fee_ratio > MAX_FEE_RATIO_AT_ENTRY:
            return True, (f"HK entry fees {fee_ratio*10000:.1f} bps exceed "
                          f"{MAX_FEE_RATIO_AT_ENTRY*10000:.0f} bps cap")
        return False, f"OK (HK entry: HK${fees['total_hkd']:.2f} = {fee_ratio*10000:.1f} bps)"
    else:
        if notional < US_MIN_NOTIONAL_USD:
            return True, f"US order too small: ${notional:,.0f} < ${US_MIN_NOTIONAL_USD:,} min"
        fees = us_stock_fees(qty, price, side)
        fee_ratio = fees["total"] / notional if notional else 0
        # US is so cheap we'd basically never hit this, but include for completeness
        if fee_ratio > MAX_FEE_RATIO_AT_ENTRY:
            return True, f"US fees {fee_ratio*10000:.1f} bps unexpectedly high"
        # Short-borrow rate check
        if side == "SHORT":
            sym = code.split(".",1)[1] if "." in code else code
            borrow = short_borrow_cost(sym, notional, days_held=365)
            ann_bps = borrow["borrow_rate_annual_pct"] * 100
            if ann_bps > SHORT_BORROW_ALERT_BPS:
                return True, (f"Short borrow estimated at {ann_bps:.0f} bps annualised "
                              f"(>{SHORT_BORROW_ALERT_BPS} bps threshold) — likely HTB")
        return False, f"OK (US: ${fees['total']:.2f} = {fee_ratio*10000:.2f} bps)"


# ── Post-fill recording ────────────────────────────────────────────

def record_trade_cost(code: str, qty: int, price: float, side: str,
                      stage: str = "entry",
                      extra: Optional[Dict] = None) -> Dict:
    """
    Log an entry or exit fee event.
    stage: 'entry' | 'exit' | 'borrow_daily'
    """
    if code.startswith("HK."):
        fees = hk_stock_fees(qty, price, side)
        cost_usd = fees["total_usd"]
    else:
        fees = us_stock_fees(qty, price, side)
        cost_usd = fees["total"]

    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code": code, "side": side, "stage": stage,
        "qty": int(abs(qty)), "price": round(price, 4),
        "cost_usd": round(cost_usd, 4),
        "fee_breakdown": fees.get("components", []),
        "extra": extra or {},
    }
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with COSTS_LOG.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")

    _update_latest_index()
    return record


def record_borrow_charge(code: str, notional: float, daily_cost_usd: float) -> Dict:
    """Daily borrow charge for an open short."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code": code, "side": "SHORT", "stage": "borrow_daily",
        "notional": round(notional, 2),
        "cost_usd": round(daily_cost_usd, 4),
    }
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with COSTS_LOG.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    _update_latest_index()
    return record


def _update_latest_index() -> None:
    """Rebuild the per-position cost rollup index."""
    if not COSTS_LOG.exists():
        return
    per_code: Dict[str, Dict] = {}
    with COSTS_LOG.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            code = r.get("code")
            if not code:
                continue
            cur = per_code.setdefault(code, {
                "code": code,
                "entry_cost": 0.0, "exit_cost": 0.0,
                "borrow_cost": 0.0, "events": 0,
            })
            cur["events"] += 1
            cost = r.get("cost_usd", 0)
            stage = r.get("stage", "")
            if stage == "entry":
                cur["entry_cost"] += cost
            elif stage == "exit":
                cur["exit_cost"] += cost
            elif stage == "borrow_daily":
                cur["borrow_cost"] += cost
    # Total
    for c in per_code.values():
        c["total_cost"] = round(
            c["entry_cost"] + c["exit_cost"] + c["borrow_cost"], 2)
        c["entry_cost"] = round(c["entry_cost"], 2)
        c["exit_cost"] = round(c["exit_cost"], 2)
        c["borrow_cost"] = round(c["borrow_cost"], 2)
    COSTS_LATEST.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "per_code": per_code,
        "totals": {
            "all_codes_total_cost": round(sum(c["total_cost"] for c in per_code.values()), 2),
            "entry":  round(sum(c["entry_cost"] for c in per_code.values()), 2),
            "exit":   round(sum(c["exit_cost"] for c in per_code.values()), 2),
            "borrow": round(sum(c["borrow_cost"] for c in per_code.values()), 2),
        },
    }, indent=2))


def get_costs_for_code(code: str) -> Dict:
    """Return cumulative cost rollup for one code."""
    if not COSTS_LATEST.exists():
        return {"code": code, "total_cost": 0, "entry_cost": 0,
                "exit_cost": 0, "borrow_cost": 0}
    data = json.loads(COSTS_LATEST.read_text())
    return data.get("per_code", {}).get(code, {
        "code": code, "total_cost": 0, "entry_cost": 0,
        "exit_cost": 0, "borrow_cost": 0,
    })


# ── Daily borrow accrual job ───────────────────────────────────────

def accrue_daily_borrow_for_shorts() -> Dict:
    """
    Walk every open short and record one day of borrow cost.
    Called nightly via cron.
    """
    try:
        broker = json.loads((LOG_DIR / "broker_snapshot.json").read_text())
    except Exception:
        return {"accrued": 0, "total_usd": 0}

    accrued = 0
    total = 0.0
    for p in broker.get("positions", []):
        qty = float(p.get("qty", 0))
        if qty >= 0:
            continue  # not a short
        code = p.get("code", "")
        mv = abs(float(p.get("market_val_usd", 0)))
        if mv <= 0:
            continue
        sym = code.split(".",1)[1] if "." in code else code
        borrow = short_borrow_cost(sym, mv, days_held=1)
        record_borrow_charge(code, mv, borrow["total_cost"])
        accrued += 1
        total += borrow["total_cost"]

    return {"accrued": accrued, "total_usd": round(total, 2)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = accrue_daily_borrow_for_shorts()
    print(f"Daily borrow accrual: {out['accrued']} positions, ${out['total_usd']:.2f}")
