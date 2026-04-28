"""
Gap Reversal Strategy — catches panic gap-downs that reverse intraday.
Entry when stock gaps down >= 2% and recovers >= 50% of the gap.
"""

from typing import Optional
import pandas as pd

from strategy.base import BaseStrategy, Signal, Direction


class GapReversalStrategy(BaseStrategy):
    name = "gap_reversal"

    def required_bars(self) -> int:
        return 10

    def evaluate(self, code: str, data: pd.DataFrame) -> Optional[Signal]:
        if len(data) < self.required_bars():
            return None

        curr = data.iloc[-1]
        prev = data.iloc[-2]

        if pd.isna(curr.get("atr_14")):
            return None

        atr_val = curr.get("atr_14", curr["close"] * 0.02)

        # Require: opened below prior close (gap down) but closed above open (recovered)
        gap_down = curr["open"] < prev["close"] * 0.98  # at least 2% gap
        recovered = curr["close"] > curr["open"]  # green candle

        if not (gap_down and recovered):
            return None

        gap_size = (prev["close"] - curr["open"]) / prev["close"]
        denom = prev["close"] - curr["open"]
        recovery = (curr["close"] - curr["open"]) / denom if denom > 0 else 0

        if recovery < 0.5:  # must recover at least 50% of gap
            return None

        strength = min(0.3 + gap_size * 5 + recovery * 0.2, 0.85)
        strength = max(0.2, min(strength, 0.85))

        stop_loss = round(curr["low"] - 0.5 * atr_val, 2)
        take_profit = round(prev["close"] + 0.5 * atr_val, 2)

        return Signal(
            code=code,
            direction=Direction.LONG,
            strength=round(strength, 3),
            strategy_name=self.name,
            entry_price=curr["close"],
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata={
                "gap_size_pct": round(gap_size * 100, 1),
                "recovery_pct": round(recovery * 100, 0),
                "atr": round(atr_val, 2),
            },
        )

    def invalidation(self, data: pd.DataFrame, signal: Signal) -> bool:
        """Invalidate if price drops below the gap low."""
        if len(data) < 1:
            return True
        return data.iloc[-1]["close"] < signal.stop_loss
