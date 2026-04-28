"""
Breakout Strategy (Support/Resistance).

Entry: Price breaks above resistance with 2x average volume.
Exit: Next resistance level or 2x ATR trailing stop.
"""

from typing import Optional
import pandas as pd

from strategy.base import BaseStrategy, Signal, Direction


class BreakoutStrategy(BaseStrategy):
    name = "breakout"

    def __init__(self, volume_multiplier: float = 1.3, lookback: int = 20,
                 breakout_window: int = 3):
        # v2: Relaxed thresholds based on backtest finding that v1 fired 0
        # times in 6+ years on daily bars. RVOL 2.0 + same-bar cross was
        # mathematically impossible most days. New logic:
        #   - RVOL >= 1.3 (was 2.0)
        #   - Allow the breakout to have occurred in the last N bars (was 1)
        #   - Require a higher-high vs the prior 20-bar window, not just close
        #     above the running resistance
        self.volume_multiplier = volume_multiplier
        self.lookback = lookback
        self.breakout_window = breakout_window

    def required_bars(self) -> int:
        return self.lookback + self.breakout_window + 2

    def evaluate(self, code: str, data: pd.DataFrame) -> Optional[Signal]:
        if len(data) < self.required_bars():
            return None

        curr = data.iloc[-1]
        rvol = curr.get("rvol", 1.0)
        atr_val = curr.get("atr_14", 0)

        if atr_val == 0:
            return None

        # Look at the prior 20-bar window EXCLUDING the breakout window. The
        # breakout reference is the highest high of bars [t-N-lookback, t-N).
        ref_window = data.iloc[-(self.lookback + self.breakout_window):-self.breakout_window]
        if len(ref_window) < self.lookback // 2:
            return None
        prior_high = ref_window["high"].max()

        # Latest N bars
        recent = data.iloc[-self.breakout_window:]
        # Did any of the recent bars close above the prior high?
        broke = (recent["close"] > prior_high).any()
        if not broke:
            return None
        # Current bar must still be holding above the breakout level
        if curr["close"] < prior_high:
            return None

        # Volume confirmation — average RVOL across the breakout window
        recent_rvol = recent.get("rvol")
        avg_rvol = float(recent_rvol.mean()) if recent_rvol is not None else rvol
        if avg_rvol < self.volume_multiplier:
            return None

        # Trend filter: don't long a breakout below the 50 SMA (avoid bear-rally fakes)
        sma_50 = curr.get("sma_50")
        if sma_50 is not None and curr["close"] < sma_50:
            return None

        # Strength based on volume surge, breakout magnitude, and trend slope
        breakout_pct = (curr["close"] - prior_high) / prior_high if prior_high > 0 else 0
        vol_score = min(avg_rvol / 2.5, 1.0)
        breakout_score = min(max(breakout_pct, 0) * 30, 1.0)
        strength = 0.5 * vol_score + 0.5 * breakout_score
        strength = max(0.25, min(strength, 1.0))

        # Stop just below the breakout level (or 1 ATR, whichever is closer)
        stop_loss = max(prior_high - 0.5 * atr_val, curr["close"] - 1.5 * atr_val)
        take_profit = curr["close"] + 2.5 * atr_val
        # Backwards-compatible aliases for downstream code that used "resistance"
        resistance = prior_high

        return Signal(
            code=code,
            direction=Direction.LONG,
            strength=round(strength, 3),
            strategy_name=self.name,
            entry_price=curr["close"],
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            metadata={
                "resistance": round(resistance, 2),
                "rvol": round(rvol, 2),
                "atr": round(atr_val, 2),
                "breakout_pct": round(breakout_pct * 100, 3),
            },
        )

    def invalidation(self, data: pd.DataFrame, signal: Signal) -> bool:
        """Invalidate if price falls back below breakout level within 2 bars."""
        if len(data) < 2:
            return True
        resistance = signal.metadata.get("resistance", 0)
        return data.iloc[-1]["close"] < resistance
