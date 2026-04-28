"""
Core Sleeve Rebalancer — weekly cadence rebalance logic.

Decides which core positions to add, hold, trim, or overlay with options.
Runs ONCE PER WEEK (not per cycle) to prevent thrashing.

Rules:
  - Max 2 position changes per rebalance (add or trim)
  - Only opens new positions in bull/flat regime
  - Trims positions where valuation has become unattractive (< 0.25)
  - Attaches covered call overlay after 2+ weeks holding
  - Generates CSP ideas for core entries at discount
  - Respects portfolio concentration limits (R1-R7)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fundamental.portfolio import (
    PortfolioState, check_concentration, size_core_position,
    CORE_MAX_POSITIONS, TICKER_SECTOR, MAX_SINGLE_NAME_PCT, MAX_SECTOR_PCT,
)
from fundamental.sleeve import SleeveAssignment

logger = logging.getLogger("fundamental.rebalancer")

LOG_DIR = Path("logs")
REBALANCE_STATE_FILE = LOG_DIR / "core_rebalance_state.json"

# Rebalance constraints
MAX_CHANGES_PER_REBALANCE = 2     # don't churn — max 2 adds or trims per week
MIN_HOLD_WEEKS = 2                 # don't trim positions held < 2 weeks
TRIM_VALUATION_THRESHOLD = 0.25    # trim if valuation drops below this
CC_ELIGIBLE_HOLD_WEEKS = 2         # must hold 2+ weeks before CC overlay
CC_VALUATION_RANGE = (0.30, 0.60)  # only CC when not deeply cheap or expensive
EARNINGS_GUARD_DAYS = 14           # no CC/CSP within 14 days of earnings


@dataclass
class RebalanceAction:
    """One rebalance decision for a single ticker."""
    ticker: str
    action: str          # "add" | "trim" | "hold" | "attach_cc" | "sell_csp" | "exit"
    sleeve: str = "core"
    target_notional: float = 0.0
    reasoning: str = ""
    assignment: Optional[SleeveAssignment] = None


@dataclass
class RebalanceState:
    """Persisted state across rebalance cycles."""
    last_rebalance_date: Optional[str] = None
    core_holdings: Dict[str, dict] = field(default_factory=dict)
    # Each holding: {ticker, entry_date, entry_price, notional, weeks_held}


def load_rebalance_state() -> RebalanceState:
    """Load persisted rebalance state from disk."""
    if REBALANCE_STATE_FILE.exists():
        try:
            data = json.loads(REBALANCE_STATE_FILE.read_text())
            return RebalanceState(
                last_rebalance_date=data.get("last_rebalance_date"),
                core_holdings=data.get("core_holdings", {}),
            )
        except Exception:
            pass
    return RebalanceState()


def save_rebalance_state(state: RebalanceState):
    """Persist rebalance state to disk."""
    data = {
        "last_rebalance_date": state.last_rebalance_date,
        "core_holdings": state.core_holdings,
    }
    REBALANCE_STATE_FILE.write_text(json.dumps(data, indent=2, default=str))


def should_rebalance(state: RebalanceState) -> bool:
    """Check if enough time has passed since last rebalance (7 days)."""
    if state.last_rebalance_date is None:
        return True
    try:
        last = datetime.strptime(state.last_rebalance_date, "%Y-%m-%d")
        return (datetime.now() - last).days >= 7
    except Exception:
        return True


def generate_rebalance_actions(
    assignments: Dict[str, SleeveAssignment],
    portfolio_state: PortfolioState,
    regime: str,
    rebalance_state: RebalanceState,
) -> List[RebalanceAction]:
    """Generate weekly rebalance actions for the core sleeve.

    Args:
        assignments: Current sleeve classifications {ticker: SleeveAssignment}
        portfolio_state: Current portfolio allocation snapshot
        regime: Current market regime (bull/flat/bear)
        rebalance_state: Persisted state with holding history

    Returns:
        List of RebalanceAction objects (max MAX_CHANGES_PER_REBALANCE adds/trims)
    """
    actions = []
    changes_count = 0
    now = datetime.now()

    # ── Step 1: Check existing holdings for trims/exits ──────────
    for ticker, holding in rebalance_state.core_holdings.items():
        assignment = assignments.get(ticker)
        entry_date = holding.get("entry_date", "")
        try:
            weeks_held = (now - datetime.strptime(entry_date, "%Y-%m-%d")).days / 7
        except Exception:
            weeks_held = 0

        # Don't trim positions held less than MIN_HOLD_WEEKS
        if weeks_held < MIN_HOLD_WEEKS:
            actions.append(RebalanceAction(
                ticker=ticker, action="hold",
                reasoning=f"Held {weeks_held:.1f} weeks (min {MIN_HOLD_WEEKS})",
            ))
            continue

        # If no longer classified as core, exit
        if assignment is None or assignment.sleeve != "core":
            if changes_count < MAX_CHANGES_PER_REBALANCE:
                actions.append(RebalanceAction(
                    ticker=ticker, action="exit",
                    reasoning=f"No longer core-classified: {assignment.reasoning if assignment else 'no data'}",
                ))
                changes_count += 1
            continue

        # If valuation has become very unattractive, trim
        val_score = assignment.valuation.composite if assignment.valuation else 0.5
        if val_score < TRIM_VALUATION_THRESHOLD:
            if changes_count < MAX_CHANGES_PER_REBALANCE:
                actions.append(RebalanceAction(
                    ticker=ticker, action="trim",
                    reasoning=f"Valuation dropped to {val_score:.2f} (threshold {TRIM_VALUATION_THRESHOLD})",
                    assignment=assignment,
                ))
                changes_count += 1
            continue

        # Check CC overlay eligibility
        if (weeks_held >= CC_ELIGIBLE_HOLD_WEEKS
                and CC_VALUATION_RANGE[0] <= val_score <= CC_VALUATION_RANGE[1]):
            actions.append(RebalanceAction(
                ticker=ticker, action="attach_cc",
                reasoning=(f"Held {weeks_held:.0f}w, valuation {val_score:.2f} "
                          f"(CC range {CC_VALUATION_RANGE[0]}-{CC_VALUATION_RANGE[1]})"),
                assignment=assignment,
            ))
        else:
            actions.append(RebalanceAction(
                ticker=ticker, action="hold",
                reasoning=f"Core hold: quality={assignment.quality.grade}, val={val_score:.2f}",
                assignment=assignment,
            ))

    # ── Step 2: Check for new additions (only in bull/flat) ──────
    if regime == "bear":
        # In bear, only CSP entries (no new stock buys)
        core_candidates = [
            (tk, a) for tk, a in assignments.items()
            if a.sleeve == "core" and a.strategy == "sell_csp"
            and tk not in rebalance_state.core_holdings
        ]
    else:
        core_candidates = [
            (tk, a) for tk, a in assignments.items()
            if a.sleeve == "core" and a.strategy in ("buy_stock", "sell_csp")
            and tk not in rebalance_state.core_holdings
        ]

    # Sort by confidence (highest first)
    core_candidates.sort(key=lambda x: x[1].confidence, reverse=True)

    current_core_count = len(rebalance_state.core_holdings)

    for ticker, assignment in core_candidates:
        if changes_count >= MAX_CHANGES_PER_REBALANCE:
            break
        if current_core_count >= CORE_MAX_POSITIONS:
            break

        # Check concentration rules
        target_size = size_core_position(portfolio_state.total_equity, regime)
        allowed, reason = check_concentration(ticker, target_size, portfolio_state)
        if not allowed:
            logger.info(f"Core add blocked for {ticker}: {reason}")
            continue

        action_type = "add" if assignment.strategy == "buy_stock" else "sell_csp"
        actions.append(RebalanceAction(
            ticker=ticker,
            action=action_type,
            target_notional=target_size,
            reasoning=(f"{assignment.strategy}: quality={assignment.quality.grade} "
                      f"({assignment.quality.composite:.2f}), "
                      f"valuation={assignment.valuation.composite:.2f}, "
                      f"confidence={assignment.confidence:.2f}"),
            assignment=assignment,
        ))
        changes_count += 1
        current_core_count += 1

    return actions


def execute_rebalance(actions: List[RebalanceAction],
                      rebalance_state: RebalanceState) -> RebalanceState:
    """Apply rebalance actions to the state.

    NOTE: This updates the persisted state only. Actual order execution
    is handled by the main auto_trader loop which reads these actions.
    """
    now_str = datetime.now().strftime("%Y-%m-%d")

    for action in actions:
        if action.action == "add":
            rebalance_state.core_holdings[action.ticker] = {
                "entry_date": now_str,
                "notional": action.target_notional,
                "strategy": "buy_stock",
            }
            logger.info(f"CORE ADD: {action.ticker} ${action.target_notional:,.0f} — {action.reasoning}")

        elif action.action == "sell_csp":
            rebalance_state.core_holdings[action.ticker] = {
                "entry_date": now_str,
                "notional": action.target_notional,
                "strategy": "sell_csp",
            }
            logger.info(f"CORE CSP: {action.ticker} ${action.target_notional:,.0f} — {action.reasoning}")

        elif action.action == "trim":
            if action.ticker in rebalance_state.core_holdings:
                # Reduce notional by 50%
                holding = rebalance_state.core_holdings[action.ticker]
                holding["notional"] = holding.get("notional", 0) * 0.5
                logger.info(f"CORE TRIM: {action.ticker} — {action.reasoning}")

        elif action.action == "exit":
            if action.ticker in rebalance_state.core_holdings:
                del rebalance_state.core_holdings[action.ticker]
                logger.info(f"CORE EXIT: {action.ticker} — {action.reasoning}")

        elif action.action == "attach_cc":
            logger.info(f"CORE CC: {action.ticker} — {action.reasoning}")
            # CC execution happens in options_strategies.py — log the intent

        elif action.action == "hold":
            pass  # no change

    rebalance_state.last_rebalance_date = now_str
    save_rebalance_state(rebalance_state)

    return rebalance_state


def get_core_tickers(rebalance_state: RebalanceState) -> set:
    """Return set of tickers currently in the core sleeve.

    Used by tactical engine to enforce R1 (no overlap).
    """
    return set(rebalance_state.core_holdings.keys())


def log_rebalance_actions(actions: List[RebalanceAction]):
    """Save rebalance actions to disk for dashboard display."""
    out = []
    for a in actions:
        out.append({
            "ticker": a.ticker,
            "action": a.action,
            "sleeve": a.sleeve,
            "target_notional": round(a.target_notional, 2),
            "reasoning": a.reasoning,
            "timestamp": datetime.now().isoformat(),
        })
    path = LOG_DIR / "core_rebalance_log.json"
    path.write_text(json.dumps(out, indent=2, default=str))
    logger.info(f"Logged {len(out)} rebalance actions to {path}")
