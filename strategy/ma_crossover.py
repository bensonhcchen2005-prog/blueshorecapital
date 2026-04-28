"""
Moving Average Crossover Strategy.

Entry: Fast SMA crosses above slow SMA, RSI > 50, volume above average.
Exit: Fast SMA crosses below slow SMA or trailing stop hit.
"""

from typing import Optional
import pandas as pd

from strategy.base import BaseStrategy, Signal, Direction


class MACrossoverStrategy(BaseStrategy):
    name = "ma_crossover"

    def __init__(self, fast_period: int = 20, slow_period: int = 50,
                 cross_window: int = 5):
        # v2: Backtest showed v1 fired only 16 times in 6 years because it
        # required an exact same-bar cross. Relaxed:
        #   - "First up-cross within the last N bars" (was 1)
        #   - Drop RVOL gate (duplicates the trend filter)
        #   - Keep RSI > 50 confirmation
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.cross_window = cross_window
        self.fast_col = f"sma_{fast_period}"
        self.slow_col = f"sma_{slow_period}"

    def required_bars(self) -> int:
        return self.slow_period + self.cross_window + 2

    def evaluate(self, code: str, data: pd.DataFrame) -> Optional[Signal]:
        if len(data) < self.required_bars():
            return None

        if self.fast_col not in data.columns or self.slow_col not in data.columns:
            return None

        curr = data.iloc[-1]
        fast_now = curr[self.fast_col]
        slow_now = curr[self.slow_col]
        if pd.isna(fast_now) or pd.isna(slow_now):
            return None

        # Current state must be fast > slow (uptrend confirmed)
        if fast_now <= slow_now:
            return None

        # Look for an up-cross within the last cross_window bars
        recent = data.iloc[-(self.cross_window + 1):]
        diff = recent[self.fast_col] - recent[self.slow_col]
        if diff.isna().any():
            return None
        # Was there a sign flip from <=0 to >0 anywhere in the window?
        signs = (diff > 0).astype(int).values
        crossover = any(signs[i] == 1 and signs[i - 1] == 0 for i in range(1, len(signs)))
        if not crossover:
            return None

        # Confirm with RSI > 50 (bullish momentum)
        rsi_val = curr.get("rsi_14", 50)
        if rsi_val < 50:
            return None

        rvol = curr.get("rvol", 1.0)
        # Use rvol only as a strength contributor, not a gate

        rsi_strength = min((rsi_val - 50) / 30, 1.0)
        vol_strength = min(rvol / 2.0, 1.0)
        ma_spread = (fast_now - slow_now) / slow_now if slow_now > 0 else 0
        ma_strength = min(ma_spread * 100, 1.0)

        strength = 0.4 * rsi_strength + 0.3 * vol_strength + 0.3 * ma_strength
        strength = max(0.15, min(strength, 1.0))

        # Stop loss at slow MA
        stop_loss = slow_now
        # Take profit at 2x risk
        risk = curr["close"] - stop_loss
        take_profit = curr["close"] + 2 * risk if risk > 0 else curr["close"] * 1.04

        return Signal(
            code=code,
            direction=Direction.LONG,
            strength=round(strength, 3),
            strategy_name=self.name,
            entry_price=curr["close"],
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            metadata={
                "fast_ma": round(fast_now, 2),
                "slow_ma": round(slow_now, 2),
                "rsi": round(rsi_val, 2),
                "rvol": round(rvol, 2),
            },
        )

    def invalidation(self, data: pd.DataFrame, signal: Signal) -> bool:
        """Invalidate if price drops below slow MA within 2 bars."""
        if len(data) < 2:
            return True
        curr = data.iloc[-1]
        slow_ma = curr.get(self.slow_col, 0)
        return curr["close"] < slow_ma
