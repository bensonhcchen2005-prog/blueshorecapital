"""Volume indicators."""

import pandas as pd
import numpy as np


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    direction = np.sign(df["close"].diff())
    return (df["volume"] * direction).cumsum()


def relative_volume(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Current volume relative to the N-period average."""
    avg_vol = df["volume"].rolling(window=period).mean()
    return df["volume"] / avg_vol
