"""
Position Classifier — distinguishes COMPOUNDERS from TACTICAL trades and
applies different exit + sizing logic per class.

Three classes:
  COMPOUNDER   — high quality (Q≥70), long horizon (≥18mo), hold through
                 volatility. Exit only on thesis break, NOT stop loss.
                 Tax-aware: prefer holding past 365 days if profit < 5%.
  CATALYST     — defined event-driven trade (3-6mo). Trailing stop after
                 catalyst hits. Tax-naive (short-term is OK).
  TACTICAL     — signal-driven swing (<90d). Strict 8% stop loss.
                 Bot-originated; high churn acceptable.

This addresses the long-term return drivers we identified:
  Section 2  Hold winners — COMPOUNDERS get no stop-loss exit
  Section 3  Hedge tactically — QQQ short flagged TACTICAL, sized to events
  Section 4  Tax efficiency — track holding period; warn before <1yr exits
  Section 5  Dividend reinvestment — flag DRIP-eligible compounders
  Section 6  Permanent capital impairment — different stop logic per class

Classification rules:
  1. Manual override via env var POSITION_CLASS_{SYMBOL}=COMPOUNDER|CATALYST|TACTICAL
  2. Q≥70 AND horizon_days ≥365 → COMPOUNDER
  3. horizon_days ∈ [60, 360]      → CATALYST
  4. horizon_days < 60             → TACTICAL
  5. SHORTS default                → TACTICAL (hedges are tactical by nature)
  6. ETFs (XLF, GLD, QQQ)          → TACTICAL
"""
from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Classification thresholds ─────────────────────────────────────

COMPOUNDER_MIN_QUALITY      = 0.70
COMPOUNDER_MIN_HORIZON_DAYS = 365
CATALYST_HORIZON_RANGE      = (60, 360)
TACTICAL_MAX_HORIZON_DAYS   = 60

# ── Stop-loss policy per class ────────────────────────────────────

EXIT_POLICY = {
    "COMPOUNDER": {
        "use_stop_loss":         False,
        "trailing_stop_pct":     None,   # no trailing stop
        "max_drawdown_tol_pct":  0.30,   # tolerate 30% drawdown before review
        "exit_on_thesis_break":  True,   # ONLY exit reason: thesis broken
        "tax_aware":             True,   # prefer >365d holds
        "min_hold_days":         180,    # don't even consider exit < 6mo
    },
    "CATALYST": {
        "use_stop_loss":         True,
        "trailing_stop_pct":     0.12,   # 12% trailing stop after catalyst
        "max_drawdown_tol_pct":  0.15,   # 15% drawdown = review
        "exit_on_thesis_break":  True,
        "tax_aware":             False,
        "min_hold_days":         21,
    },
    "TACTICAL": {
        "use_stop_loss":         True,
        "trailing_stop_pct":     0.08,   # 8% trailing stop
        "max_drawdown_tol_pct":  0.08,
        "exit_on_thesis_break":  False,  # purely technical exits
        "tax_aware":             False,
        "min_hold_days":         0,
    },
}

# ── Holding-period awareness for taxes ────────────────────────────

TAX_LONG_TERM_THRESHOLD_DAYS = 365
TAX_HOLD_THROUGH_PROFIT_PCT  = 0.05  # if profit < 5% and < 365d, hold


# ── DRIP-eligible compounders (paid quarterly dividends, hi quality) ──

DRIP_ELIGIBLE = {
    "US.LLY", "US.JNJ", "US.ABBV", "US.JPM", "US.AAPL", "US.MSFT",
    "US.AVGO", "US.GOOGL", "US.META", "US.V", "US.MA", "US.HD",
    "US.COST", "US.WMT", "US.UNH",
}


@dataclass
class PositionClass:
    code:                  str
    classification:        str       # COMPOUNDER | CATALYST | TACTICAL
    quality_score:         Optional[float]
    horizon_days:          int
    days_held:             int
    opened_at:             Optional[datetime]
    policy:                Dict
    drip_eligible:         bool
    tax_status:            str       # SHORT_TERM | LONG_TERM | LONG_TERM_SOON
    days_to_long_term:     int
    reasoning:             str


def classify_position(code: str, thesis: Dict,
                      side: str = "LONG") -> PositionClass:
    """
    Classify a position based on its thesis + fundamentals.

    Args:
      code:    e.g. "US.PLTR"
      thesis:  the thesis dict from theses_active.json (has quantitative + target)
      side:    LONG or SHORT
    """
    # Manual override first
    sym = code.split(".",1)[1] if "." in code else code
    override = os.environ.get(f"POSITION_CLASS_{sym}", "").upper()
    if override in EXIT_POLICY:
        return _build(code, thesis, side, override, "manual override via env")

    # SHORTS / ETFs default TACTICAL
    if side == "SHORT":
        return _build(code, thesis, side, "TACTICAL", "SHORT positions are tactical hedges")
    if sym in ("QQQ","SPY","IWM","XLK","XLF","XLE","GLD","TLT","UVIX","VIXY","SVXY"):
        return _build(code, thesis, side, "TACTICAL", "ETF positions are tactical")

    # Quality + horizon based
    q = (thesis.get("quantitative") or {}).get("quality_score")
    target = thesis.get("target") or {}
    horizon = target.get("horizon_days") or 90

    if (q is not None and q >= COMPOUNDER_MIN_QUALITY
        and horizon >= COMPOUNDER_MIN_HORIZON_DAYS):
        return _build(code, thesis, side, "COMPOUNDER",
                      f"Q={q*100:.0f} ≥ {COMPOUNDER_MIN_QUALITY*100:.0f} and horizon {horizon}d ≥ {COMPOUNDER_MIN_HORIZON_DAYS}d")

    if CATALYST_HORIZON_RANGE[0] <= horizon <= CATALYST_HORIZON_RANGE[1]:
        return _build(code, thesis, side, "CATALYST",
                      f"horizon {horizon}d in catalyst range")

    return _build(code, thesis, side, "TACTICAL",
                  f"horizon {horizon}d < {TACTICAL_MAX_HORIZON_DAYS}d OR Q<{COMPOUNDER_MIN_QUALITY}")


def _build(code: str, thesis: Dict, side: str, cls: str, reason: str) -> PositionClass:
    target = thesis.get("target") or {}
    q = (thesis.get("quantitative") or {}).get("quality_score")
    horizon_days = target.get("horizon_days") or 90

    opened_at = None
    days_held = 0
    try:
        opened_at = datetime.fromisoformat(thesis["opened_at"].replace("Z","+00:00"))
        days_held = (datetime.now(timezone.utc) - opened_at).days
    except Exception:
        pass

    days_to_lt = max(0, TAX_LONG_TERM_THRESHOLD_DAYS - days_held)
    if days_held >= TAX_LONG_TERM_THRESHOLD_DAYS:
        tax_status = "LONG_TERM"
    elif days_to_lt <= 45:
        tax_status = "LONG_TERM_SOON"
    else:
        tax_status = "SHORT_TERM"

    return PositionClass(
        code=code, classification=cls,
        quality_score=q, horizon_days=horizon_days, days_held=days_held,
        opened_at=opened_at, policy=EXIT_POLICY[cls],
        drip_eligible=(code in DRIP_ELIGIBLE and cls == "COMPOUNDER"),
        tax_status=tax_status, days_to_long_term=days_to_lt,
        reasoning=reason,
    )


# ── Exit decision engine ─────────────────────────────────────────

def should_exit(code: str, thesis: Dict, side: str,
                current_price: float,
                technical_signal_exit: bool = False,
                thesis_break_detected: bool = False,
                stop_price: Optional[float] = None) -> Tuple[bool, str]:
    """
    Final word on whether to exit a position. Applies class-specific
    policy on top of technical signals.

    Returns (should_exit, reason).

    Logic per class:

    COMPOUNDER:
      - NEVER exit on technical signal alone
      - NEVER exit on stop-loss touch
      - ONLY exit if thesis_break_detected
      - Tax-aware: if winning < 5% and held < 365d, hold to clear LT threshold

    CATALYST:
      - Exit on technical OR stop OR thesis break, after min_hold_days
      - Before min_hold_days, only thesis_break_detected triggers exit

    TACTICAL:
      - Standard technical / stop-loss exits apply
      - No thesis-break logic (no fundamental thesis)
    """
    pc = classify_position(code, thesis, side)
    policy = pc.policy

    # Compounder: ignore technical signals entirely
    if pc.classification == "COMPOUNDER":
        if thesis_break_detected:
            return True, f"COMPOUNDER thesis break — exit allowed"
        # Tax check: held > 0 days, profit < 5%, days_to_long_term ≤ 45
        try:
            entry = float(thesis.get("entry_price") or 0)
            if entry > 0:
                profit_pct = (current_price - entry) / entry
                if side == "SHORT":
                    profit_pct = -profit_pct
                if (profit_pct > 0 and profit_pct < TAX_HOLD_THROUGH_PROFIT_PCT
                    and pc.tax_status == "LONG_TERM_SOON"):
                    return False, (f"COMPOUNDER tax hold: +{profit_pct*100:.1f}% "
                                   f"profit, {pc.days_to_long_term}d to long-term")
        except Exception:
            pass
        return False, (f"COMPOUNDER: ignoring technical signal "
                       f"(only thesis break triggers exit)")

    # Catalyst: respect min_hold_days
    if pc.classification == "CATALYST":
        if pc.days_held < policy["min_hold_days"]:
            if thesis_break_detected:
                return True, (f"CATALYST thesis break before "
                              f"min_hold_days ({pc.days_held}/{policy['min_hold_days']})")
            return False, (f"CATALYST: holding for min_hold_days "
                           f"({pc.days_held}/{policy['min_hold_days']})")
        if thesis_break_detected:
            return True, "CATALYST thesis break"
        if technical_signal_exit:
            return True, "CATALYST technical exit signal"
        if stop_price and current_price <= stop_price:
            return True, f"CATALYST stop-loss touched ${stop_price}"
        return False, "CATALYST: no exit trigger"

    # Tactical: standard exits
    if technical_signal_exit:
        return True, "TACTICAL technical exit"
    if stop_price and current_price <= stop_price:
        return True, f"TACTICAL stop-loss ${stop_price}"
    return False, "TACTICAL: no exit trigger"


# ── Convenience: classify a whole portfolio ──────────────────────

def classify_portfolio(theses: Dict[str, Dict]) -> Dict[str, PositionClass]:
    """Apply classifier to every active thesis."""
    out = {}
    for code, thesis in theses.items():
        try:
            side = thesis.get("side", "LONG")
            out[code] = classify_position(code, thesis, side)
        except Exception as e:
            logger.warning(f"classify_portfolio: {code} failed: {e}")
    return out
