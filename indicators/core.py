"""
Indicator orchestrator.
Runs all selected indicators on a DataFrame and returns enriched data.
"""

import pandas as pd

from indicators.moving_averages import sma, ema
from indicators.momentum import rsi, macd, roc
from indicators.volatility import atr, bollinger_bands
from indicators.volume import obv, relative_volume
from indicators.levels import rolling_support_resistance


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """
    Enrich an OHLCV DataFrame with all standard indicators.

    Expects columns: open, high, low, close, volume
    Returns the same DataFrame with indicator columns appended.
    """
    out = df.copy()
    close = out["close"]

    # Moving Averages
    out["sma_10"] = sma(close, 10)
    out["sma_20"] = sma(close, 20)
    out["sma_50"] = sma(close, 50)
    out["ema_12"] = ema(close, 12)
    out["ema_26"] = ema(close, 26)

    # Momentum
    out["rsi_14"] = rsi(close, 14)
    macd_line, signal_line, hist = macd(close)
    out["macd"] = macd_line
    out["macd_signal"] = signal_line
    out["macd_hist"] = hist
    out["roc_10"] = roc(close, 10)
    out["mom_5d"] = close.pct_change(5) * 100
    out["mom_20d"] = close.pct_change(20) * 100

    # Volatility
    out["atr_14"] = atr(out, 14)
    bb_upper, bb_mid, bb_lower = bollinger_bands(close, 20, 2.0)
    out["bb_upper"] = bb_upper
    out["bb_mid"] = bb_mid
    out["bb_lower"] = bb_lower

    # Volume
    out["obv"] = obv(out)
    out["rvol"] = relative_volume(out, 20)

    # Levels
    support, resistance = rolling_support_resistance(out, 20)
    out["support_20"] = support
    out["resistance_20"] = resistance

    return out
