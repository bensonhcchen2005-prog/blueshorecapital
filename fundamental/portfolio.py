"""
Portfolio Construction Rules — sleeve-level allocation, overlap prevention,
and concentration limits.

Architecture:
  Portfolio ($1M total)
  ├── Core Sleeve (60-70% of capital)
  │   ├── Stock positions (buy_stock): long-term, fundamental-driven
  │   ├── Cash-secured puts (sell_csp): income on names we'd own
  │   └── Covered calls (hold_cc): income on existing core longs
  │
  ├── Tactical Sleeve (20-30% of capital)
  │   ├── Signal-driven equity trades (existing engine)
  │   └── Defined-risk options (spreads, debit trades)
  │
  └── Cash Reserve (12-15% minimum)
      └── Always available for opportunities / margin

RULES (prevent confusion):
  R1: Same ticker CANNOT be in both sleeves simultaneously.
      If conflict: core takes priority (longer-duration, bigger size).
  R2: Core positions hold for WEEKS/MONTHS. Tactical hold for DAYS.
      Different rebalance cadences prevent thrashing.
  R3: Core sleeve max 8 positions. Tactical sleeve max 5 positions.
  R4: Max 12% of total portfolio in any single name (combined sleeves).
  R5: Max 30% in any single sector.
  R6: Core sleeve only opens in bull/flat regimes (not bear).
      Tactical sleeve runs in all regimes (with regime gating).
  R7: Cash reserve floor: always keep 12% liquid.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("fundamental.portfolio")

# ── Allocation parameters ─────────────────────────────────────
TOTAL_CAPITAL = 1_000_000

# Target allocation ranges (% of total capital)
CORE_ALLOC_MIN = 0.50
CORE_ALLOC_MAX = 0.70
TACTICAL_ALLOC_MIN = 0.15
TACTICAL_ALLOC_MAX = 0.30
CASH_FLOOR = 0.12  # always keep at least 12% liquid

# Position limits
CORE_MAX_POSITIONS = 8
TACTICAL_MAX_POSITIONS = 5
MAX_SINGLE_NAME_PCT = 0.12     # 12% max in one ticker (combined)
MAX_SECTOR_PCT = 0.30          # 30% max in one sector

# Core position sizing
CORE_POSITION_PCT = 0.07       # 7% base per core position
CORE_POSITION_MAX_USD = 100_000

# Tactical position sizing (inherits from existing engine)
TACTICAL_POSITION_PCT = 0.05   # 5% per tactical position
TACTICAL_POSITION_MAX_USD = 60_000


# ── Sector mapping ────────────────────────────────────────────
TICKER_SECTOR = {
    "AAPL": "tech", "MSFT": "tech", "GOOGL": "tech", "AMZN": "tech",
    "META": "tech", "CRM": "tech",
    "NVDA": "semis", "AMD": "semis", "AVGO": "semis", "ARM": "semis",
    "TSLA": "consumer", "BA": "industrial",
    "JPM": "financial", "GS": "financial",
    "UNH": "healthcare", "XOM": "energy",
    "PLTR": "tech", "COIN": "crypto", "SMCI": "tech", "MSTR": "crypto",
    "SPY": "index", "QQQ": "index", "IWM": "index",
    "TLT": "bonds", "GLD": "commodities",
    "XLE": "energy", "XLF": "financial", "XLK": "tech",
}


@dataclass
class PortfolioState:
    """Snapshot of current portfolio allocation."""
    total_equity: float = TOTAL_CAPITAL
    core_positions: Dict[str, dict] = field(default_factory=dict)
    tactical_positions: Dict[str, dict] = field(default_factory=dict)
    cash: float = TOTAL_CAPITAL
    core_allocated_pct: float = 0.0
    tactical_allocated_pct: float = 0.0
    cash_pct: float = 1.0


def compute_sector_exposure(positions: Dict[str, dict]) -> Dict[str, float]:
    """Sum notional exposure per sector."""
    sectors = {}
    for tk, pos in positions.items():
        sector = TICKER_SECTOR.get(tk, "other")
        notional = abs(pos.get("notional", 0))
        sectors[sector] = sectors.get(sector, 0) + notional
    return sectors


def check_concentration(ticker: str, proposed_notional: float,
                        state: PortfolioState) -> Tuple[bool, str]:
    """Check if adding this position would violate concentration rules.

    Returns (allowed, reason).
    """
    # R1: no overlap between sleeves
    if ticker in state.core_positions and ticker in state.tactical_positions:
        return False, f"R1: {ticker} already in both sleeves"

    # R4: single-name concentration
    existing_notional = 0
    for positions in [state.core_positions, state.tactical_positions]:
        if ticker in positions:
            existing_notional += abs(positions[ticker].get("notional", 0))
    total_exposure = existing_notional + proposed_notional
    if total_exposure / state.total_equity > MAX_SINGLE_NAME_PCT:
        return False, (
            f"R4: {ticker} would be {total_exposure/state.total_equity:.1%} "
            f"of portfolio (max {MAX_SINGLE_NAME_PCT:.0%})"
        )

    # R5: sector concentration
    sector = TICKER_SECTOR.get(ticker, "other")
    all_positions = {**state.core_positions, **state.tactical_positions}
    sector_exposure = compute_sector_exposure(all_positions)
    current_sector = sector_exposure.get(sector, 0)
    if (current_sector + proposed_notional) / state.total_equity > MAX_SECTOR_PCT:
        return False, (
            f"R5: {sector} sector would be "
            f"{(current_sector + proposed_notional)/state.total_equity:.1%} "
            f"(max {MAX_SECTOR_PCT:.0%})"
        )

    # R7: cash floor
    available_cash = state.cash - proposed_notional
    if available_cash / state.total_equity < CASH_FLOOR:
        return False, (
            f"R7: cash would drop to {available_cash/state.total_equity:.1%} "
            f"(min {CASH_FLOOR:.0%})"
        )

    return True, "OK"


def resolve_sleeve_conflict(ticker: str, core_assignment, tactical_signal) -> str:
    """R1: When a ticker has both core and tactical interest, decide which wins.

    Core takes priority UNLESS:
      - Core strategy is "wait" (no action) and tactical has a live signal
    """
    if core_assignment and core_assignment.sleeve == "core":
        if core_assignment.strategy != "wait":
            return "core"
        # Core says wait, tactical has a signal → tactical can trade it
        if tactical_signal is not None:
            return "tactical"
        return "core"  # assigned to core even if waiting
    return "tactical"


def size_core_position(equity: float, regime: str) -> float:
    """Compute core position size in dollars."""
    base = min(equity * CORE_POSITION_PCT, CORE_POSITION_MAX_USD)
    if regime == "bear":
        base *= 0.5   # half-size in bear market
    elif regime == "flat":
        base *= 0.75  # reduce in flat market
    return base


def size_tactical_position(equity: float, regime: str, signal_score: float) -> float:
    """Compute tactical position size in dollars.

    Tactical sizing is smaller and score-proportional.
    """
    base = min(equity * TACTICAL_POSITION_PCT, TACTICAL_POSITION_MAX_USD)
    # Scale by signal strength (0.5–1.0 → 0.6–1.0x)
    score_mult = 0.6 + 0.4 * min(signal_score, 1.0)
    base *= score_mult
    if regime == "bear":
        base *= 0.5
    elif regime == "flat":
        base *= 0.75
    return base


def get_portfolio_summary(state: PortfolioState) -> dict:
    """Generate summary for dashboard display."""
    core_notional = sum(
        abs(p.get("notional", 0)) for p in state.core_positions.values()
    )
    tactical_notional = sum(
        abs(p.get("notional", 0)) for p in state.tactical_positions.values()
    )
    total = state.total_equity

    return {
        "total_equity": total,
        "core": {
            "positions": len(state.core_positions),
            "notional": round(core_notional, 2),
            "pct": round(core_notional / total * 100, 1) if total else 0,
            "tickers": list(state.core_positions.keys()),
        },
        "tactical": {
            "positions": len(state.tactical_positions),
            "notional": round(tactical_notional, 2),
            "pct": round(tactical_notional / total * 100, 1) if total else 0,
            "tickers": list(state.tactical_positions.keys()),
        },
        "cash": {
            "amount": round(state.cash, 2),
            "pct": round(state.cash / total * 100, 1) if total else 0,
        },
        "sectors": compute_sector_exposure(
            {**state.core_positions, **state.tactical_positions}
        ),
    }
