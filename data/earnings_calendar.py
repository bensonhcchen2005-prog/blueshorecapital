"""
Earnings date lookup using yfinance as the data source.

Provides a cached `days_to_next_earnings(ticker)` function that returns the
number of calendar days until the next scheduled earnings report for a US
ticker.  Returns None if no date can be found (safe — caller treats None as
"no blackout applies").

Cache TTL: 6 hours per symbol.  The module keeps results in memory for the
process lifetime and writes nothing to disk.

Usage:
    from data.earnings_calendar import days_to_next_earnings
    d = days_to_next_earnings("AAPL")  # → int or None
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, date
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ETFs and broad-market instruments that never have earnings dates.
# We skip the yfinance lookup for these to avoid noisy "No fundamentals
# found" error logs and unnecessary network calls.
_ETF_PREFIXES = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VEA", "VWO",
    "EEM", "GLD", "SLV", "TLT", "IEF", "SHY", "LQD", "HYG",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLB", "XLU", "XLRE",
    "SMH", "SOXX", "ARKK", "ARKG", "ARKW", "ARKQ",
    "USO", "UNG", "SQQQ", "TQQQ", "SPXU", "UPRO",
})

# in-process cache: ticker → (days_result, expiry_timestamp)
_cache: Dict[str, Tuple[Optional[int], float]] = {}
_cache_lock = threading.Lock()
_TTL = 6 * 3600          # 6 hours in seconds
_FETCH_TIMEOUT = 10      # yfinance network timeout (seconds)


def _strip_market(symbol: str) -> str:
    """Convert 'US.AAPL' → 'AAPL', 'HK.00700' → '0700.HK'."""
    if "." not in symbol:
        return symbol
    market, ticker = symbol.split(".", 1)
    if market.upper() == "HK":
        # yfinance HK format: strip leading zeros, append .HK
        return ticker.lstrip("0") + ".HK"
    # US tickers: just return the ticker part
    return ticker


def _fetch_days(yf_ticker: str) -> Optional[int]:
    """
    Fetch next earnings date via yfinance and return calendar days away.
    Returns None on any error or if no date is available.
    """
    try:
        import yfinance as yf  # lazy import — not available in all envs

        info = yf.Ticker(yf_ticker)
        # yfinance exposes earnings dates in multiple places:
        # 1. ticker.calendar (dict with 'Earnings Date' key if available)
        # 2. ticker.earnings_dates DataFrame (historical + upcoming)
        cal = info.calendar
        if cal is not None and not (hasattr(cal, "empty") and cal.empty):
            # calendar is a dict or DataFrame depending on yfinance version
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if ed is not None:
                    if hasattr(ed, "__iter__") and not isinstance(ed, str):
                        # list of dates — take the first future one
                        today = date.today()
                        for d in ed:
                            if hasattr(d, "date"):
                                d = d.date()
                            if isinstance(d, date) and d >= today:
                                return (d - today).days
                    else:
                        if hasattr(ed, "date"):
                            ed = ed.date()
                        if isinstance(ed, date) and ed >= date.today():
                            return (ed - date.today()).days
            else:
                # DataFrame — look for future dates
                try:
                    today = date.today()
                    idx = cal.columns if hasattr(cal, "columns") else []
                    if "Earnings Date" in idx:
                        for val in cal["Earnings Date"]:
                            d = val.date() if hasattr(val, "date") else val
                            if isinstance(d, date) and d >= today:
                                return (d - today).days
                except Exception:
                    pass

        # Fallback: earnings_dates property (newer yfinance versions)
        try:
            edf = info.earnings_dates
            if edf is not None and not edf.empty:
                today = datetime.utcnow().date()
                for idx_val in edf.index:
                    d = idx_val.date() if hasattr(idx_val, "date") else idx_val
                    if isinstance(d, date) and d >= today:
                        return (d - today).days
        except Exception:
            pass

    except Exception as e:
        logger.debug(f"earnings fetch failed for {yf_ticker}: {e}")
    return None


def days_to_next_earnings(symbol: str) -> Optional[int]:
    """
    Return the number of calendar days until this symbol's next earnings.

    Parameters
    ----------
    symbol : str
        Moomoo-format ticker (e.g. 'US.AAPL', 'HK.00700') or bare ticker.

    Returns
    -------
    int  — days until earnings (0 = today, 1 = tomorrow, etc.)
    None — no date found / fetch failed / HK ticker (not supported by yf well)
    """
    yf_ticker = _strip_market(symbol)

    # Skip lookup for ETFs and broad-market instruments — they have no earnings
    if yf_ticker.split(".")[0].upper() in _ETF_PREFIXES:
        return None

    now = time.time()

    with _cache_lock:
        if yf_ticker in _cache:
            result, expiry = _cache[yf_ticker]
            if now < expiry:
                return result

    # Fetch outside the lock to avoid blocking other threads
    result = _fetch_days(yf_ticker)

    with _cache_lock:
        _cache[yf_ticker] = (result, now + _TTL)

    if result is not None:
        logger.debug(f"[EARNINGS] {symbol} → {result}d to next earnings")
    return result


def prefetch_batch(symbols: list[str]) -> None:
    """
    Warm the cache for a list of symbols in background threads.
    Fire-and-forget — errors are suppressed.  Call at cycle start.
    """
    def _worker(sym: str) -> None:
        try:
            days_to_next_earnings(sym)
        except Exception:
            pass

    threads = [threading.Thread(target=_worker, args=(s,), daemon=True)
               for s in symbols]
    for t in threads:
        t.start()
    # Don't join — background prefetch, caller continues immediately
