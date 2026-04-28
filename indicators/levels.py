"""Support/resistance and pivot point indicators."""

import pandas as pd


def pivot_points(df: pd.DataFrame) -> pd.DataFrame:
    """
    Standard floor pivot points from prior bar's high/low/close.
    Returns DataFrame with columns: pivot, r1, r2, s1, s2.
    """
    h = df["high"].shift(1)
    l = df["low"].shift(1)
    c = df["close"].shift(1)

    pivot = (h + l + c) / 3
    r1 = 2 * pivot - l
    s1 = 2 * pivot - h
    r2 = pivot + (h - l)
    s2 = pivot - (h - l)

    return pd.DataFrame({"pivot": pivot, "r1": r1, "r2": r2, "s1": s1, "s2": s2})


def rolling_support_resistance(
    df: pd.DataFrame, lookback: int = 20
) -> tuple[pd.Series, pd.Series]:
    """
    Simple rolling support (lowest low) and resistance (highest high).
    """
    support = df["low"].rolling(window=lookback).min()
    resistance = df["high"].rolling(window=lookback).max()
    return support, resistance
