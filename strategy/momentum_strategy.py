"""
Momentum Strategy v2 (ROC + MACD + trend confirmation).

v2 changes (tightened to reduce low-quality signals):
  - ROC threshold raised from >0 to >3 (require meaningful momentum)
  - MACD histogram must be positive AND increasing (not just positive)
  - Price must be above SMA50 (trend filter — avoid counter-trend entries)
  - RSI must be 40-75 (avoid chasing overbought, avoid weak momentum)
  - Strength formula reweighted toward ROC (the strongest predictor)
"""

from typing import Optional
import pandas as pd

from strategy.base import BaseStrategy, Signal, Direction


class MomentumStrategy(BaseStrategy):
    name = "momentum"

    def required_bars(self) -> int:
        return 55

    def evaluate(self, code: str, data: pd.DataFrame) -> Optional[Signal]:
        if len(data) < self.required_bars():
            return None

        curr = data.iloc[-1]
        prev = data.iloc[-2]

        roc_val = curr.get("roc_10")
        macd_hist = curr.get("macd_hist")
        prev_macd_hist = prev.get("macd_hist")
        atr_val = curr.get("atr_14", 0)
        ema_12 = curr.get("ema_12")
        ema_26 = curr.get("ema_26")
        sma_50 = curr.get("sma_50")
        rsi_val = curr.get("rsi_14")

        if roc_val is None or macd_hist is None or pd.isna(roc_val) or pd.isna(macd_hist):
            return None

        # Tightened: require meaningful momentum, not just barely positive
        if roc_val <= 3:
            return None
        if macd_hist <= 0:
            return None

        # MACD must be increasing (momentum accelerating, not decelerating)
        if prev_macd_hist is not None and not pd.isna(prev_macd_hist):
            if macd_hist <= prev_macd_hist:
                return None

        # Trend confirmation: fast EMA above slow EMA
        if ema_12 is not None and ema_26 is not None and ema_12 <= ema_26:
            return None

        # Price above SMA50 — don't chase momentum in a downtrend
        if sma_50 is not None and not pd.isna(sma_50) and curr["close"] < sma_50:
            return None

        # RSI filter: avoid overbought chasing and weak momentum
        if rsi_val is not None and not pd.isna(rsi_val):
            if rsi_val < 40 or rsi_val > 75:
                return None

        # Strength — heavier weight on ROC (best predictor in backtests)
        roc_strength = min(roc_val / 10, 1.0)
        macd_strength = min(abs(macd_hist) / (atr_val or 1), 1.0)
        strength = 0.6 * roc_strength + 0.4 * macd_strength
        strength = max(0.3, min(strength, 1.0))

        stop_loss = curr["close"] - 2 * atr_val if atr_val > 0 else curr["close"] * 0.97
        take_profit = curr["close"] + 3 * atr_val if atr_val > 0 else curr["close"] * 1.06

        return Signal(
            code=code,
            direction=Direction.LONG,
            strength=round(strength, 3),
            strategy_name=self.name,
            entry_price=curr["close"],
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            metadata={
                "roc": round(roc_val, 2),
                "macd_hist": round(macd_hist, 4),
                "atr": round(atr_val, 2),
                "rsi": round(rsi_val, 2) if rsi_val and not pd.isna(rsi_val) else None,
            },
        )

    def invalidation(self, data: pd.DataFrame, signal: Signal) -> bool:
        """Invalidate if ROC turns negative."""
        if len(data) < 1:
            return True
        return data.iloc[-1].get("roc_10", -1) < 0
