"""
RSI Divergence Strategy — bullish divergence where price makes lower low but RSI
makes higher low. Strong reversal signal that catches bottoms before MAs react.
"""

from typing import Optional

import numpy as np
import pandas as pd

from strategy.base import BaseStrategy, Signal, Direction


class RSIDivergenceStrategy(BaseStrategy):
    name = "rsi_divergence"

    def required_bars(self) -> int:
        return 30

    def evaluate(self, code: str, data: pd.DataFrame) -> Optional[Signal]:
        if len(data) < self.required_bars():
            return None

        curr = data.iloc[-1]

        if pd.isna(curr.get("rsi_14")) or pd.isna(curr.get("atr_14")):
            return None

        # Look for divergence in last 10 bars
        window = data.iloc[-10:]
        prices = window["close"].values
        rsis = window["rsi_14"].values

        if len(prices) < 10 or np.any(np.isnan(rsis)):
            return None

        # Find two lows: recent (last 5 bars) and prior (first 5 bars)
        recent_low_idx = len(prices) - 1 - np.argmin(prices[-5:][::-1])
        prior_low_idx = int(np.argmin(prices[:5]))

        if recent_low_idx <= prior_low_idx:
            return None  # need prior before recent

        price_lower_low = prices[recent_low_idx] < prices[prior_low_idx]
        rsi_higher_low = rsis[recent_low_idx] > rsis[prior_low_idx]

        if not (price_lower_low and rsi_higher_low):
            return None

        # Additional filters
        if curr["rsi_14"] > 50:  # only useful in oversold territory
            return None
        if curr["close"] > curr.get("sma_20", float("inf")):
            return None

        atr_val = curr.get("atr_14", curr["close"] * 0.02)
        divergence_strength = (rsis[recent_low_idx] - rsis[prior_low_idx]) / 20
        strength = min(0.4 + divergence_strength, 0.85)
        strength = max(0.2, min(strength, 0.85))

        stop_loss = round(min(prices[recent_low_idx], curr["close"]) - 1.0 * atr_val, 2)
        take_profit = round(curr.get("sma_20", curr["close"] * 1.05), 2)

        return Signal(
            code=code,
            direction=Direction.LONG,
            strength=round(strength, 3),
            strategy_name=self.name,
            entry_price=curr["close"],
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata={
                "rsi_prior": round(float(rsis[prior_low_idx]), 1),
                "rsi_recent": round(float(rsis[recent_low_idx]), 1),
                "atr": round(atr_val, 2),
            },
        )

    def invalidation(self, data: pd.DataFrame, signal: Signal) -> bool:
        """Invalidate if RSI goes above 60 (no longer in reversal zone)."""
        if len(data) < 1:
            return True
        return data.iloc[-1].get("rsi_14", 60) > 60
