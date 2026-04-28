"""
Short Momentum Strategy — shorts weak US names with negative momentum below SMA20.
US-only (HK paper does not support shorting).
"""

from typing import Optional
import pandas as pd

from strategy.base import BaseStrategy, Signal, Direction


class ShortMomentumStrategy(BaseStrategy):
    name = "short_momentum"

    def required_bars(self) -> int:
        return 30

    def evaluate(self, code: str, data: pd.DataFrame) -> Optional[Signal]:
        # US-only: accept both moomoo-prefixed ("US.NVDA") and plain ("NVDA") codes
        if "." in code and not code.startswith("US."):
            return None  # HK or other non-US market
        if len(data) < self.required_bars():
            return None

        curr = data.iloc[-1]

        mom_20 = curr.get("mom_20d")
        rsi_val = curr.get("rsi_14")
        if pd.isna(mom_20) or pd.isna(rsi_val):
            return None

        # Conditions: weak 20d momentum, below SMA20, RSI trending down but not oversold
        below_sma20 = curr["close"] < curr.get("sma_20", float("inf"))
        if not below_sma20 or mom_20 > -5 or rsi_val < 25 or rsi_val > 55:
            return None

        atr_val = curr.get("atr_14", curr["close"] * 0.02)
        weakness = min(abs(mom_20) / 20, 1.0)
        rvol = curr.get("rvol", 0) if not pd.isna(curr.get("rvol", 0)) else 0
        strength = 0.4 * weakness + 0.3 * (1 - rsi_val / 55) + 0.3 * (1 if rvol > 1.2 else 0.3)
        strength = max(0.1, min(strength, 1.0))

        stop_loss = round(curr.get("sma_20", curr["close"] * 1.05) + atr_val, 2)
        take_profit = round(curr["close"] - 2.5 * atr_val, 2)

        return Signal(
            code=code,
            direction=Direction.SHORT,
            strength=round(strength, 3),
            strategy_name=self.name,
            entry_price=curr["close"],
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata={
                "mom_20d": round(mom_20, 1),
                "rsi": round(rsi_val, 1),
                "atr": round(atr_val, 2),
            },
        )

    def invalidation(self, data: pd.DataFrame, signal: Signal) -> bool:
        """Invalidate if price recovers above SMA20."""
        if len(data) < 1:
            return True
        curr = data.iloc[-1]
        sma20 = curr.get("sma_20")
        if sma20 is None or pd.isna(sma20):
            return False
        return curr["close"] > sma20
