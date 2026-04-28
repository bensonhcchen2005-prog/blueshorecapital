"""
Mean Reversion Strategy v2 (RSI + Bollinger Bands).

v2 changes:
  - Relaxed RSI from <30 to <35 (more signals — this is our best strategy)
  - Relaxed BB proximity from 0.5% to 1.5% (catches more setups)
  - Relaxed volume filter from rvol>=1.2 to rvol>=0.8 (volume spikes are nice
    but not required — the RSI+BB setup is strong enough)
  - Added ATR-based stop (2.5x ATR) instead of fixed 3% (adapts to volatility)
  - Wider take profit: BB upper band instead of mid (let winners run)
"""

from typing import Optional
import pandas as pd

from strategy.base import BaseStrategy, Signal, Direction


class MeanReversionStrategy(BaseStrategy):
    name = "mean_reversion"

    def __init__(self, rsi_oversold: float = 32, rsi_exit: float = 50):
        self.rsi_oversold = rsi_oversold
        self.rsi_exit = rsi_exit

    def required_bars(self) -> int:
        return 25

    def evaluate(self, code: str, data: pd.DataFrame) -> Optional[Signal]:
        if len(data) < self.required_bars():
            return None

        curr = data.iloc[-1]

        rsi_val = curr.get("rsi_14")
        bb_lower = curr.get("bb_lower")
        bb_mid = curr.get("bb_mid")
        bb_upper = curr.get("bb_upper")
        rvol = curr.get("rvol", 1.0)
        atr_val = curr.get("atr_14", 0)

        if rsi_val is None or bb_lower is None or pd.isna(rsi_val):
            return None

        # Oversold + at/near lower band
        if rsi_val >= self.rsi_oversold:
            return None
        if curr["close"] > bb_lower * 1.015:  # within 1.5% of lower band
            return None

        # Mild volume filter: not dead volume, but don't require a spike
        if rvol is not None and not pd.isna(rvol) and rvol < 0.8:
            return None

        # Signal strength — deeper oversold = stronger signal
        rsi_depth = (self.rsi_oversold - rsi_val) / self.rsi_oversold
        # Bonus if price actually crossed below BB (stronger reversal setup)
        below_bb = 1.0 if curr["close"] <= bb_lower else 0.0
        strength = min(0.50 + 0.35 * rsi_depth + 0.15 * below_bb, 1.0)

        # ATR-based stop adapts to volatility instead of fixed 3%
        if atr_val > 0:
            stop_loss = curr["close"] - 2.5 * atr_val
        else:
            stop_loss = curr["close"] * 0.97

        # Take profit at BB upper (let winners run further than BB mid)
        take_profit = bb_upper if bb_upper and not pd.isna(bb_upper) else bb_mid

        return Signal(
            code=code,
            direction=Direction.LONG,
            strength=round(strength, 3),
            strategy_name=self.name,
            entry_price=curr["close"],
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2) if take_profit else 0,
            metadata={
                "rsi": round(rsi_val, 2),
                "bb_lower": round(bb_lower, 2),
                "bb_mid": round(bb_mid, 2) if bb_mid else 0,
                "rvol": round(rvol, 2) if rvol and not pd.isna(rvol) else 0,
                "atr": round(atr_val, 2) if atr_val else 0,
            },
        )

    def invalidation(self, data: pd.DataFrame, signal: Signal) -> bool:
        """Invalidate if price closes below lower BB for 3 consecutive bars."""
        if len(data) < 3:
            return True
        recent = data.tail(3)
        bb_lower = recent.get("bb_lower")
        if bb_lower is None:
            return False
        below_count = (recent["close"] < bb_lower).sum()
        return below_count >= 3
