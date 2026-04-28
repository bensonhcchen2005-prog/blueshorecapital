"""
Technical indicator computation from moomoo kline data.

Computes RSI, volume-fade, and upper-wick signals using only the kline
DataFrames already being fetched by auto_trader.  No external API calls.

All functions are pure / stateless — they take a pandas DataFrame and return
a scalar signal.  Callers should cache the results themselves if needed.

Typical usage
-------------
    from data.indicators import rsi, volume_fade, upper_wick_rejection
    from moomoo import KLType

    ret, df = ctx.request_history_kline("US.AAPL", start=..., end=...,
                                        ktype=KLType.K_DAY, max_count=20)
    if ret == RET_OK and not df.empty:
        rsi_val       = rsi(df)                          # float 0–100
        vfade         = volume_fade(df)                  # bool
        upper_wick    = upper_wick_rejection(df)         # bool
        rsi_div       = rsi_divergence(df, price_col="close")  # bool
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ── RSI ──────────────────────────────────────────────────────────────────────

def rsi(df: pd.DataFrame, period: int = 14,
        price_col: str = "close") -> Optional[float]:
    """
    Wilder RSI for the most-recent bar.

    Parameters
    ----------
    df         : kline DataFrame (must have `price_col` column)
    period     : lookback period (default 14)
    price_col  : column name to use

    Returns
    -------
    float in [0, 100] — most-recent RSI value, or None if insufficient data.
    """
    if price_col not in df.columns or len(df) < period + 1:
        return None
    closes = df[price_col].astype(float)
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    # Wilder smoothing (EWM with alpha=1/period)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    last_loss = avg_loss.iloc[-1]
    if last_loss == 0:
        return 100.0
    rs = avg_gain.iloc[-1] / last_loss
    return float(100 - (100 / (1 + rs)))


# ── Volume-fade ───────────────────────────────────────────────────────────────

def volume_fade(df: pd.DataFrame,
                vol_col: str = "volume",
                price_col: str = "close",
                lookback: int = 5,
                fade_threshold: float = 0.85) -> bool:
    """
    True when the last bar closed higher than the prior bar **and** volume
    was less than `fade_threshold` × the average of the prior `lookback` bars.

    This detects "up-bar on thin volume" — a distribution signal.

    Parameters
    ----------
    df              : kline DataFrame
    vol_col         : column name for volume
    price_col       : column name for close price
    lookback        : bars to average for volume baseline
    fade_threshold  : fraction below which volume counts as faded (default 0.85)

    Returns
    -------
    bool — True if up-bar + volume fade detected on last bar.
    """
    if vol_col not in df.columns or price_col not in df.columns:
        return False
    if len(df) < lookback + 2:
        return False

    closes = df[price_col].astype(float)
    vols   = df[vol_col].astype(float)

    last_close  = closes.iloc[-1]
    prior_close = closes.iloc[-2]
    last_vol    = vols.iloc[-1]
    avg_vol     = vols.iloc[-(lookback + 1):-1].mean()

    if avg_vol <= 0:
        return False

    is_up_bar   = last_close > prior_close
    is_vol_fade = last_vol < fade_threshold * avg_vol
    return bool(is_up_bar and is_vol_fade)


# ── Upper-wick rejection ──────────────────────────────────────────────────────

def upper_wick_rejection(df: pd.DataFrame,
                         wick_ratio: float = 0.40) -> bool:
    """
    True when the last bar has a large upper wick (≥ `wick_ratio` of the
    full range), indicating intraday sellers absorbed the rally.

    Useful when price probed near the take-profit target but closed back
    in the lower portion of the candle body.

    Parameters
    ----------
    df          : kline DataFrame (needs open, high, low, close)
    wick_ratio  : upper-wick / total-range threshold (default 0.40 = 40%)

    Returns
    -------
    bool
    """
    required = {"open", "high", "low", "close"}
    if not required.issubset(df.columns):
        return False
    if df.empty:
        return False

    row = df.iloc[-1]
    o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
    total_range = h - l
    if total_range <= 0:
        return False

    body_top    = max(o, c)
    upper_wick  = h - body_top
    return bool(upper_wick / total_range >= wick_ratio)


# ── RSI divergence ────────────────────────────────────────────────────────────

def rsi_divergence(df: pd.DataFrame,
                   price_col: str = "close",
                   rsi_period: int = 14,
                   lookback: int = 5) -> bool:
    """
    Bearish RSI divergence: price made a higher high over the last `lookback`
    bars, but the RSI at those same points was lower (momentum weakening).

    Simple two-point comparison: compares (price[-1], RSI[-1]) vs
    (price[-lookback], RSI[-lookback]).

    Parameters
    ----------
    df          : kline DataFrame with at least rsi_period + lookback bars
    price_col   : price column to compare
    rsi_period  : RSI period
    lookback    : how many bars back to look for the prior high

    Returns
    -------
    bool — True if bearish divergence detected.
    """
    if price_col not in df.columns or len(df) < rsi_period + lookback + 1:
        return False

    closes  = df[price_col].astype(float)
    delta   = closes.diff()
    gain    = delta.clip(lower=0)
    loss    = (-delta).clip(lower=0)
    avg_g   = gain.ewm(alpha=1 / rsi_period, adjust=False).mean()
    avg_l   = loss.ewm(alpha=1 / rsi_period, adjust=False).mean()

    def _rsi_at(i: int) -> float:
        al = avg_l.iloc[i]
        if al == 0:
            return 100.0
        return float(100 - (100 / (1 + avg_g.iloc[i] / al)))

    rsi_now   = _rsi_at(-1)
    rsi_prior = _rsi_at(-(lookback + 1))
    price_now   = float(closes.iloc[-1])
    price_prior = float(closes.iloc[-(lookback + 1)])

    # Bearish divergence: price higher, RSI lower
    return bool(price_now > price_prior and rsi_now < rsi_prior)


# ── Convenience bundle ────────────────────────────────────────────────────────

def compute_signals(df: pd.DataFrame) -> dict:
    """
    Compute all three exit-signal booleans from a single kline DataFrame.

    Returns dict with keys: rsi_value, rsi_divergence, vol_fade, upper_wick
    Safe to call even if df is empty or missing columns.
    """
    if df is None or df.empty:
        return {"rsi_value": None, "rsi_divergence": False,
                "vol_fade": False, "upper_wick": False}
    try:
        return {
            "rsi_value":     rsi(df),
            "rsi_divergence": rsi_divergence(df),
            "vol_fade":       volume_fade(df),
            "upper_wick":     upper_wick_rejection(df),
        }
    except Exception as e:
        logger.debug(f"compute_signals error: {e}")
        return {"rsi_value": None, "rsi_divergence": False,
                "vol_fade": False, "upper_wick": False}
