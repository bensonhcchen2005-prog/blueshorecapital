"""
Profit-protection and early-exit rules for strong rally conditions.

Three layers:
  A. Rally-extension check (macro, per-cycle) — uses SPY N-day returns
  B. Per-position trailing-stop ratchet — never lets a winner turn into a loser
  C. Momentum-fade signals — RSI divergence, volume fade, upper-wick rejection

Called by auto_trader.check_exits() on every open position each cycle.
Modifies stop-loss in the journal but never calls the broker directly.
Actual order placement is handled by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ── Rally extension levels (Layer A) ─────────────────────────────────────
def rally_extension_level(
    spy_1d: float,
    spy_3d: float,
    spy_5d: float,
    vol_fade: bool = False,
) -> str:
    """
    Returns one of: 'normal' | 'extended' | 'multi_day' | 'euphoria'

    Parameters are decimal returns (0.03 = 3%). vol_fade means SPY volume
    today was < 85% of 3-day average (rally losing breadth).
    """
    if spy_5d >= 0.10:
        return "euphoria"
    if spy_3d >= 0.06:
        return "multi_day"
    if spy_1d >= 0.03 or spy_5d >= 0.05 or (spy_5d >= 0.04 and vol_fade):
        return "extended"
    return "normal"


# ── Trailing-stop ratchet (Layer B) ──────────────────────────────────────
def compute_trailing_stop(entry: float, unrealized_pct: float) -> Optional[float]:
    """
    Ratcheting hard floor on a winner. Returns the new stop price if it
    should be raised, or None if no change is warranted.

    Ladder:
      < 1.5% gain  →  no change (hold, normal stop applies)
      ≥ 1.5% gain  →  move stop to entry (breakeven — can no longer lose)
      ≥ 3.0% gain  →  move stop to entry + 1.5% (lock in minimum gain)
      ≥ 4.5% gain  →  move stop to entry + 2.5% (trail at ~55% of open gain)
    """
    if unrealized_pct < 0.015:
        return None                    # under threshold — no ratchet
    if unrealized_pct < 0.030:
        return entry                   # breakeven
    if unrealized_pct < 0.045:
        return entry * 1.015           # lock in +1.5%
    return entry * 1.025               # lock in +2.5%


# ── Exit decision dataclass ───────────────────────────────────────────────
@dataclass
class ExitDecision:
    should_exit: bool
    trim_pct: float               # 0 = hold, 0.5 = trim half, 1.0 = full exit
    new_stop: Optional[float]     # None = keep existing; value = raise to this
    reason: str


# ── Full per-position evaluation (Layers A + B + C) ──────────────────────
def evaluate_exit(
    entry: float,
    current_price: float,
    current_stop: float,
    current_tp: float,
    sleeve: str,                  # 'core' | 'tactical' | '' (unknown = tactical)
    strategy: str,
    spy_extension: str,           # from rally_extension_level()
    rsi_divergence: bool = False,
    vol_fade: bool = False,
    upper_wick: bool = False,
    days_to_earnings: Optional[int] = None,
) -> ExitDecision:
    """
    Evaluate whether to exit, trim, or tighten stops on a single position.
    Returns an ExitDecision — caller is responsible for acting on it.

    Core positions: only trailing stops are ratcheted. No tactical exits.
    Tactical positions: full Layer A/B/C logic applies.
    """
    unrealized_pct = (current_price - entry) / entry if entry else 0.0
    is_core = sleeve.lower() == "core"

    # ── Always: earnings blackout (tactical only, core holds)
    if not is_core and days_to_earnings is not None and 0 <= days_to_earnings <= 1:
        return ExitDecision(True, 1.0, None,
                            f"earnings blackout: {days_to_earnings}d to event")

    # ── Core positions: only ratchet the stop, never tactical-exit
    if is_core:
        new_stop = compute_trailing_stop(entry, unrealized_pct)
        if new_stop and new_stop > current_stop:
            return ExitDecision(False, 0.0, new_stop,
                                f"core ratchet: stop {current_stop:.2f} → {new_stop:.2f}")
        return ExitDecision(False, 0.0, None, "core: hold")

    # ── TACTICAL from here ────────────────────────────────────────────────

    # Layer A: Euphoria — exit everything tactical regardless of P&L
    if spy_extension == "euphoria":
        return ExitDecision(True, 1.0, None,
                            "SPY euphoria (≥10% in 5d): exit all tactical")

    # Layer A: Multi-day rally — trim if in profit, tighten stop
    if spy_extension == "multi_day" and unrealized_pct >= 0.02:
        new_stop = compute_trailing_stop(entry, unrealized_pct) or current_stop
        new_stop = max(new_stop, current_stop)
        return ExitDecision(False, 0.50, new_stop,
                            f"multi-day rally + +{unrealized_pct*100:.1f}%: trim 50%, tighten")

    # Layer A: Extended rally — tighten stops on all winners to at least BE
    if spy_extension == "extended" and unrealized_pct >= 0.015:
        be = max(entry, current_stop)            # at minimum breakeven
        new_stop = compute_trailing_stop(entry, unrealized_pct)
        new_stop = max(new_stop or be, be)
        if new_stop > current_stop:
            return ExitDecision(False, 0.0, new_stop,
                                f"extended rally: stop → {new_stop:.2f}")

    # Layer C: Momentum-fade signals (only meaningful if position in profit)
    if unrealized_pct >= 0.015:
        # Upper-wick rejection near TP — price tried to reach target, was sold back
        if upper_wick and current_tp and current_price >= current_tp * 0.99:
            return ExitDecision(True, 1.0, None,
                                "upper-wick rejection at TP proximity: exit")

        # RSI divergence — price higher but momentum lower (weakening rally)
        if rsi_divergence and unrealized_pct >= 0.030:
            new_stop = max(entry * 1.01, current_stop)
            return ExitDecision(False, 0.50, new_stop,
                                f"RSI divergence at +{unrealized_pct*100:.1f}%: trim 50%")

        # Volume fade on up bar — distribution signal
        if vol_fade and unrealized_pct >= 0.030:
            new_stop = compute_trailing_stop(entry, unrealized_pct)
            new_stop = max(new_stop or current_stop, current_stop)
            if new_stop > current_stop:
                return ExitDecision(False, 0.0, new_stop,
                                    f"volume fade at +{unrealized_pct*100:.1f}%: tighten stop")

    # Layer B: Trailing ratchet (pure profit-level based, no signals needed)
    new_stop = compute_trailing_stop(entry, unrealized_pct)
    if new_stop and new_stop > current_stop:
        label = (
            "breakeven stop" if new_stop <= entry * 1.001
            else f"ratchet to +{(new_stop/entry - 1)*100:.1f}%"
        )
        return ExitDecision(False, 0.0, new_stop,
                            f"{label} at +{unrealized_pct*100:.1f}% gain")

    return ExitDecision(False, 0.0, None,
                        f"hold — {unrealized_pct*100:.1f}% gain, {spy_extension} market")
