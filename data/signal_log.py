"""
Unified signal lifecycle logger.

Every signal (an idea the scanner produced) gets a deterministic ID that
remains stable across log sites:

    signal_id = sha1(symbol | strategy | direction | minute_bucket)[:12]

Because the ID is derived from attributes that all downstream sites know
(symbol, strategy, direction, and the minute the signal was generated),
we don't need to thread the ID through function parameters.  Every
component can re-derive the same ID independently.

The minute bucket is `floor(unix_ts / 60)`.  Two signals for the same
(symbol, strategy, direction) fired within the same minute are treated
as the same idea — that's intentional dedup.

Stage taxonomy (in funnel order)
--------------------------------
    originated    scanner generated a raw signal
    scored        signal passed the minimum-score threshold (pre-gate)
    universe_ok   symbol in Stage 1 whitelist
    strategy_ok   strategy in Stage 1 whitelist
    score_ok      signal score ≥ threshold (stage-1 enforced)
    regime_ok     regime matches strategy requirements
    capacity_ok   open-count, sector, notional, daily-exposure checks pass
    timing_ok     fresh quotes, not in earnings blackout
    allowed       all gates cleared
    queued        written to shadow/pending queue
    filled        actual paper (or live) fill

Log file: logs/signal_events.jsonl   (one event per line, append-only)
Schema:   {ts, signal_id, symbol, strategy, direction, stage, verdict,
           detail, score, market, minute_bucket, extra?}
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
EVENTS_PATH = LOG_DIR / "signal_events.jsonl"

# Deny reason canonical taxonomy — used for breakdown charts
DENY_REASONS = {
    "universe":        "Symbol not in whitelist",
    "strategy":        "Strategy not enabled at this stage",
    "score":           "Signal score below threshold",
    "regime":          "Regime gate blocked",
    "max_open":        "Open-position cap reached",
    "position_size":   "Notional exceeds per-trade cap",
    "daily_exposure":  "Daily new-exposure cap hit",
    "sector":          "Sector concentration cap hit",
    "stale_quote":     "Quote too stale",
    "earnings":        "Within earnings blackout window",
    "halted":          "System halt active",
    "options_disabled":"Options not enabled at this stage",
    "options_type":    "Option type not allowed",
    "options_locked":  "Options still locked — need more clean stock trades",
    "cash_floor":      "Cash below floor",
    "cooldown":        "In loss-cooldown window",
    "other":           "Other / unclassified",
}


def _market_of(symbol: str) -> str:
    """Return 'US' or 'HK' from a moomoo-format symbol."""
    if "." in symbol:
        return symbol.split(".", 1)[0].upper()
    return "US"


def _minute_bucket(ts: Optional[float] = None) -> int:
    """Unix minute bucket (floor)."""
    return int((ts if ts is not None else time.time()) // 60)


def make_signal_id(symbol: str, strategy: str, direction: str,
                   ts: Optional[float] = None) -> str:
    """
    Deterministic signal ID.  Given the same (symbol, strategy, direction,
    minute_bucket), every caller produces the same 12-char hex string.
    """
    bucket = _minute_bucket(ts)
    raw = f"{symbol}|{strategy}|{direction}|{bucket}".encode()
    return hashlib.sha1(raw).hexdigest()[:12]


def classify_deny_reason(detail: str) -> str:
    """
    Normalize a free-form `detail` string from stage_controller into one of
    the canonical DENY_REASONS keys.  Returns 'other' if no match.
    """
    d = (detail or "").lower()
    # Order matters: more specific first
    if "symbol_whitelist" in d or "not in stage" in d and "universe" in d:
        return "universe"
    if "strategy_whitelist" in d or ("strategy" in d and "not allowed" in d):
        return "strategy"
    if "signal_score" in d or ("score" in d and "< min" in d):
        return "score"
    if "regime" in d:
        return "regime"
    if "max_open" in d or "open >= cap" in d:
        return "max_open"
    if "position_size" in d or ("notional" in d and "> max" in d):
        return "position_size"
    if "daily_exposure" in d or "day new" in d:
        return "daily_exposure"
    if "sector" in d:
        return "sector"
    if "stale_quote" in d or "quote" in d and "stale" in d:
        return "stale_quote"
    if "earnings" in d:
        return "earnings"
    if "halted" in d or "halt" in d:
        return "halted"
    if "options_disabled" in d:
        return "options_disabled"
    if "options_locked" in d:
        return "options_locked"
    if "option_type" in d:
        return "options_type"
    if "cash" in d and "floor" in d:
        return "cash_floor"
    if "cooldown" in d:
        return "cooldown"
    return "other"


def _write(event: dict) -> None:
    """Append one JSON-line to the events log. Fire-and-forget on failure."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception as e:
        logger.debug(f"signal_log write failed: {e}")


def log_event(symbol: str, strategy: str, direction: str, stage: str,
              verdict: str, detail: str = "", score: Optional[float] = None,
              extra: Optional[dict] = None,
              ts: Optional[datetime] = None) -> str:
    """
    Write a lifecycle event and return the signal_id.

    Parameters
    ----------
    symbol, strategy, direction : identity of the signal
    stage       : one of {originated, scored, universe_ok, strategy_ok,
                  score_ok, regime_ok, capacity_ok, timing_ok, allowed,
                  queued, filled, denied}
    verdict     : PASS | FAIL | ALLOW | DENY | QUEUED | FILLED | ORIGINATED
    detail      : free-form human-readable detail (for DENYs this is normalized
                  via classify_deny_reason for the reason field)
    score       : signal score if known
    extra       : extra context (price, qty, thesis, etc.)
    ts          : event timestamp (default: now, UTC)
    """
    now = ts or datetime.now(timezone.utc)
    sid = make_signal_id(symbol, strategy, direction, now.timestamp())
    event = {
        "ts": now.isoformat(),
        "signal_id": sid,
        "symbol": symbol,
        "strategy": strategy,
        "direction": direction,
        "stage": stage,
        "verdict": verdict,
        "detail": detail,
        "score": score,
        "market": _market_of(symbol),
        "minute_bucket": _minute_bucket(now.timestamp()),
    }
    if verdict in ("DENY", "FAIL"):
        event["deny_reason"] = classify_deny_reason(detail)
    if extra:
        event["extra"] = extra
    _write(event)
    return sid


# ── Convenience helpers for the three main call sites ────────────────────

def log_originated(symbol: str, strategy: str, direction: str,
                   score: float, thesis: str = "") -> str:
    return log_event(symbol, strategy, direction, "originated", "ORIGINATED",
                     detail=thesis, score=score,
                     extra={"thesis": thesis})


def log_gate_decision(symbol: str, strategy: str, direction: str,
                      verdict: str, detail: str,
                      score: Optional[float] = None) -> str:
    """
    Stage 1 gate result.  verdict is 'ALLOW' or 'DENY'.  If ALLOW the
    stage is 'allowed'; if DENY the stage is 'denied' and detail is
    classified into a canonical reason.
    """
    stage = "allowed" if verdict == "ALLOW" else "denied"
    return log_event(symbol, strategy, direction, stage, verdict,
                     detail=detail, score=score)


def log_queued(symbol: str, strategy: str, direction: str,
               qty: float, price: float, thesis: str = "",
               score: Optional[float] = None) -> str:
    return log_event(symbol, strategy, direction, "queued", "QUEUED",
                     detail=f"qty={qty} @ ${price}", score=score,
                     extra={"qty": qty, "price": price, "thesis": thesis,
                            "notional": qty * price})


def log_filled(symbol: str, strategy: str, direction: str,
               qty: float, price: float, stop: float, target: float,
               score: Optional[float] = None) -> str:
    return log_event(symbol, strategy, direction, "filled", "FILLED",
                     detail=(f"{direction} {qty} @ ${price} "
                             f"stop={stop} tp={target}"),
                     score=score,
                     extra={"qty": qty, "price": price,
                            "stop": stop, "target": target,
                            "notional": qty * price})
