"""
Backtesting framework v3 — replays the live strategy code over historical OHLCV
data downloaded from yfinance. Reuses indicators.core.enrich and the strategy
classes in strategy/* exactly as the live cycle uses them, so backtest results
are directly comparable to live behavior.

v3 fixes vs v2:
  1-5. (carried from v2) Next-bar execution, signal ranking, regime-aware,
       dynamic slippage, rolling walk-forward
  6.  Regime prefetch: fetch 250 extra days of SPY/VIX before backtest start
      so SMA200 is valid from day 1 (fixes Covid crash regime misclassification)
  7.  Remove STRATEGY_SIZE_MULT (in-sample overfitting) → equal strategy weight
  8.  Remove regime weight_overrides (score manipulation) → pure signal scoring
  9.  Trailing stop: after N bars in profit, move stop to breakeven + 1 ATR
  10. Drawdown circuit breaker: if DD > 5%, halve position sizes until new high
  11. Correlation guard: skip new position if >0.70 rolling corr with existing
  12. Portfolio vol targeting: scale sizes so portfolio targets ~15% annual vol

Usage:
    ./venv/bin/python backtest.py --start 2025-01-01 --end 2026-04-09
    ./venv/bin/python backtest.py --start 2020-01-01 --end 2020-12-31 --regime
    ./venv/bin/python backtest.py --walk-forward --rolling

Outputs:
    logs/backtest_<start>_<end>_<universe>_<interval>.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# Reuse the live system's strategies and indicators verbatim
from indicators.core import enrich
from strategy.base import Direction
from strategy.ma_crossover import MACrossoverStrategy
from strategy.mean_reversion import MeanReversionStrategy
from strategy.breakout import BreakoutStrategy

LOG_DIR = Path("logs")
CACHE_DIR = Path("data_cache")
CACHE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# Mirror the live universe (US only — yfinance ticker mapping)
US_UNIVERSE = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA",
    "AMD", "AVGO", "CRM", "JPM", "GS", "UNH", "XOM",
    "BA", "PLTR", "COIN", "ARM", "SMCI", "MSTR",
    "SPY", "QQQ", "IWM", "TLT", "GLD", "XLE", "XLF", "XLK",
]

# Extended universe — survivorship-bias-resistant.
EXTENDED_DELISTED = [
    "SIVB", "FRC", "SBNY", "CS", "ATVI", "TWTR", "VMW", "SPLK",
    "WORK", "FB", "ZNGA", "CERN", "PXD", "RX", "PBCT", "XLNX",
    "DISCA", "VIAC", "MGM",
    # Fallen-from-grace names still trading but materially impaired
    "WBA", "INTC", "PARA", "BABA", "PYPL", "ROKU", "PTON", "ZM", "DOCU", "BYND",
]


def get_universe(name: str) -> List[str]:
    if name == "ext":
        seen = set()
        out = []
        for t in US_UNIVERSE + EXTENDED_DELISTED:
            if t not in seen:
                out.append(t)
                seen.add(t)
        return out
    return US_UNIVERSE


# ── Cost / sizing model (mirrors live tier T0) ─────────────────
START_CAPITAL = 1_000_000.0
MAX_POSITION_PCT = 0.08
MAX_POSITION_USD = 80_000
MAX_RISK_PCT = 0.01
BASE_SLIPPAGE_BPS = 3         # base slippage; dynamic adds ATR-based component
COMMISSION_PER_SHARE = 0.005
MAX_OPEN_POSITIONS = 8
MIN_SIGNAL_SCORE = 0.50
HOLD_MAX_BARS = 20
HOLD_MIN_BARS = 2             # prevent same-day churn (min 2 bars before exit)
ATR_STOP_MULT = 2.0
SHORT_BORROW_ANNUAL_BPS = 50

# v4: Risk-free rate on idle cash — honest accounting, not a strategy improvement.
# Uses approximate T-bill rate by year (backward-looking, no lookahead).
RISK_FREE_RATES = {
    2018: 0.019, 2019: 0.022, 2020: 0.005, 2021: 0.001,
    2022: 0.015, 2023: 0.045, 2024: 0.050, 2025: 0.043, 2026: 0.040,
}

# FIX #7: REMOVED STRATEGY_SIZE_MULT — weighting strategies by in-sample
# performance is textbook overfitting. All strategies get equal capital.
# If a strategy can't justify its existence at 1.0x, it should be disabled.

# FIX #10: Drawdown circuit breaker — if portfolio drawdown > DD_BRAKE_PCT,
# halve all new position sizes until equity makes a new high.
DD_BRAKE_PCT = 0.07           # 7% drawdown triggers reduced sizing
DD_BRAKE_MULT = 0.50          # reduce to 50% of normal size

# FIX #12: Portfolio vol targeting — DISABLED for now.
# In backtesting, reactive vol targeting is pro-cyclical: scales up in
# low-vol (late-cycle) and scales down in high-vol (recovery), which is
# the opposite of what you want.  Structural risk control comes from
# regime gating + drawdown brake instead.
VOL_TARGETING_ENABLED = False
TARGET_ANNUAL_VOL = 0.15
VOL_LOOKBACK = 20
VOL_SCALE_MIN = 0.50
VOL_SCALE_MAX = 1.30

# FIX #9: Trailing stop parameters.  After TRAIL_ACTIVATE_BARS profitable
# bars, ratchet stop to max(original_stop, high_watermark - N × ATR).
# Wider than typical to avoid cutting winners on normal noise.
TRAIL_ACTIVATE_BARS = 8       # bars in profit before trailing activates
TRAIL_LOCK_ATR = 2.0          # 2 ATR below high watermark (was 1.0 — too tight)

# FIX #11: Correlation guard — skip a new position if its 60-day rolling
# correlation with any existing position exceeds this threshold.
CORR_THRESHOLD = 0.80         # 80% — only block near-duplicate exposure
CORR_LOOKBACK = 60

# 台阶战术 — capital scaling tiers (mirrors auto_trader.SCALING_TIERS)
SCALING_TIERS = [
    (1_000_000, 1.00, 0.08, 80_000,  0.010),
    (1_100_000, 0.50, 0.08, 84_000,  0.010),
    (1_250_000, 0.50, 0.07, 79_000,  0.009),
    (1_500_000, 0.40, 0.06, 72_000,  0.008),
    (2_000_000, 0.40, 0.05, 70_000,  0.007),
]

# ── Regime configuration ─────────────────────────────────────────
# FIX #8: REMOVED weight_overrides — artificially boosting signal scores
# for specific strategies in specific regimes is score manipulation / snooping.
# Strategy filtering by regime is structural (reasonable), but score boosting is not.
#
# Regime logic:
#   - Bull (SPY > 200SMA, VIX < 25): all strategies run, full sizing
#   - Flat (mixed signals): restrict to lower-vol strategies, reduce sizing
#   - Bear (SPY < 200SMA, VIX > 25): only counter-trend strategies, min sizing
REGIME_CONFIG = {
    "bull":  {"max_exposure": 1.20, "size_mult": 1.0,
              "allowed_strategies": ["ma_crossover", "mean_reversion", "breakout"]},
    "flat":  {"max_exposure": 0.80, "size_mult": 0.75,
              "allowed_strategies": ["mean_reversion", "ma_crossover"]},
    "bear":  {"max_exposure": 0.50, "size_mult": 0.50,
              "allowed_strategies": ["mean_reversion"]},
}


def get_tier(equity: float) -> tuple:
    active = SCALING_TIERS[0]
    for t in SCALING_TIERS:
        if equity >= t[0]:
            active = t
    return active


def working_capital_bt(equity: float) -> tuple:
    tier = get_tier(equity)
    trigger, keep_ratio, _, _, _ = tier
    if equity <= trigger:
        return equity, tier
    gains = equity - trigger
    working = trigger + gains * keep_ratio
    return working, tier


# Global mode configs (set by main())
SCALED_MODE = False
REGIME_MODE = False


# ── Data loader (cached locally to avoid re-downloading) ───────

def fetch_history(ticker: str, start: str, end: str, interval: str = "1d") -> Optional[pd.DataFrame]:
    cache = CACHE_DIR / f"{ticker}_{start}_{end}_{interval}.parquet"
    if cache.exists():
        try:
            return pd.read_parquet(cache)
        except Exception:
            pass
    try:
        import yfinance as yf
        df = yf.download(ticker, start=start, end=end, interval=interval,
                         progress=False, auto_adjust=True, threads=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        df = df.rename(columns={"adj close": "close"})
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        df.index.name = "date"
        try:
            df.to_parquet(cache)
        except Exception:
            pass
        return df
    except Exception as e:
        print(f"  ! fetch failed for {ticker}: {e}", file=sys.stderr)
        return None


# ── Backtest portfolio ─────────────────────────────────────────

@dataclass
class OpenPosition:
    code: str
    direction: str            # "LONG" / "SHORT"
    qty: int
    entry_price: float
    entry_date: pd.Timestamp
    stop_loss: float
    take_profit: float
    strategy: str
    bars_held: int = 0


@dataclass
class ClosedTrade:
    code: str
    direction: str
    qty: int
    entry_price: float
    exit_price: float
    entry_date: str
    exit_date: str
    pnl: float
    pnl_pct: float
    bars_held: int
    strategy: str
    exit_reason: str


@dataclass
class Portfolio:
    cash: float
    equity: float
    positions: Dict[str, OpenPosition] = field(default_factory=dict)
    trades: List[ClosedTrade] = field(default_factory=list)
    equity_curve: List[tuple] = field(default_factory=list)
    peak_equity: float = 0.0
    max_drawdown: float = 0.0


# ── FIX #4: Dynamic slippage + short borrow cost ──────────────


# Tickers with elevated overnight gap risk — wider slippage on open fills.
# These names routinely gap 2-5% on earnings/news, so next-bar-open fills
# are worse than ATR alone would suggest.
GAP_RISK_TICKERS = {
    "MSTR": 8, "TSLA": 5, "COIN": 6, "META": 4, "NVDA": 4,
    "SMCI": 7, "PLTR": 5, "RIVN": 5, "SHOP": 4, "SQ": 4,
}  # extra bps of slippage per name


def dynamic_slip(price: float, side: str, atr: float = 0.0,
                 ticker: str = "") -> float:
    """Dynamic slippage: base bps + ATR-proportional + gap risk buffer.

    For a stock trading at $100 with ATR=$3 the additional ATR
    component is 3/100 * 2bps = 6 bps on top of the 3 bps base = 9 bps.
    More volatile names pay more slippage. Illiquid / wide-spread names
    naturally have higher ATR relative to price.

    Gap risk buffer adds extra bps for names with frequent overnight gaps
    (e.g. MSTR, TSLA, COIN) since we fill at next-bar open.
    """
    base_bps = BASE_SLIPPAGE_BPS / 10_000
    # ATR component: (atr / price) scaled to ~2 bps equivalent
    atr_bps = (atr / price * 0.02) if price > 0 and atr > 0 else 0
    # Gap risk: extra bps for gap-prone names
    gap_bps = GAP_RISK_TICKERS.get(ticker, 0) / 10_000
    total = base_bps + atr_bps + gap_bps
    return price * (1 + total) if side == "BUY" else price * (1 - total)


def short_borrow_cost(entry_price: float, qty: int, bars_held: int,
                      bars_per_year: float = 252) -> float:
    """Annualized borrow cost charged per bar held."""
    notional = entry_price * abs(qty)
    daily_rate = SHORT_BORROW_ANNUAL_BPS / 10_000 / bars_per_year
    return notional * daily_rate * bars_held


def commission(qty: int) -> float:
    return abs(qty) * COMMISSION_PER_SHARE


# ── Regime engine ─────────────────────────────────────────────

def classify_regime(spy_close: float, spy_sma200: float, vix: float) -> str:
    """Classify market regime from SPY vs 200 SMA + VIX level."""
    above_200 = spy_close > spy_sma200
    if above_200 and vix < 25:
        return "bull"
    elif not above_200 and vix > 25:
        return "bear"
    return "flat"


def build_regime_series(history: Dict[str, pd.DataFrame],
                        all_dates: list,
                        spy_full: Optional[pd.DataFrame] = None,
                        vix_full: Optional[pd.DataFrame] = None) -> Dict:
    """Pre-compute regime classification for every bar date.

    FIX #6: Uses spy_full / vix_full (pre-fetched with 250 extra days before
    backtest start) so SMA200 is valid from day 1 of the backtest.  Falls back
    to history["SPY"] / history["^VIX"] if full series not provided.

    Returns dict: {date -> {"regime": str, "vix": float, "spy_sma200": float}}
    """
    spy_df = spy_full if spy_full is not None else history.get("SPY")
    vix_df = vix_full if vix_full is not None else history.get("^VIX")

    regimes = {}
    for dt in all_dates:
        regime_info = {"regime": "bull", "vix": 20.0, "spy_sma200": 0.0}

        # SPY SMA200
        if spy_df is not None and dt in spy_df.index:
            spy_idx = spy_df.index.get_loc(dt)
            if spy_idx >= 200:
                spy_sma200 = float(spy_df.iloc[spy_idx - 199:spy_idx + 1]["close"].mean())
                spy_close = float(spy_df.loc[dt, "close"])
                regime_info["spy_sma200"] = round(spy_sma200, 2)
                regime_info["spy_close"] = round(spy_close, 2)
            else:
                spy_close = float(spy_df.loc[dt, "close"])
                spy_sma200 = spy_close  # not enough data, assume neutral
                regime_info["spy_sma200"] = round(spy_sma200, 2)
                regime_info["spy_close"] = round(spy_close, 2)
        else:
            spy_close = 0
            spy_sma200 = 0

        # VIX
        vix_level = 20.0  # default
        if vix_df is not None and dt in vix_df.index:
            vix_level = float(vix_df.loc[dt, "close"])
        regime_info["vix"] = round(vix_level, 1)

        if spy_close > 0:
            regime_info["regime"] = classify_regime(spy_close, spy_sma200, vix_level)

        regimes[dt] = regime_info
    return regimes


# ── Bar-by-bar replay loop ─────────────────────────────────────

STRATEGIES = [
    MACrossoverStrategy(),
    MeanReversionStrategy(),
    BreakoutStrategy(),
]


def evaluate_signals(code: str, window: pd.DataFrame) -> List[dict]:
    """Run every strategy against the window of bars seen so far."""
    out = []
    atr = float(window.iloc[-1].get("atr_14", 0)) if len(window) > 0 else 0
    for strat in STRATEGIES:
        try:
            sig = strat.evaluate(code, window)
        except Exception:
            sig = None
        if sig is None or not sig.is_valid:
            continue
        if sig.strength < MIN_SIGNAL_SCORE:
            continue
        out.append({
            "code": code,
            "strategy": strat.name,
            "direction": sig.direction.value,
            "entry": sig.entry_price,
            "stop": sig.stop_loss,
            "take_profit": sig.take_profit,
            "score": sig.strength,
            "atr": atr,
        })
    return out


def check_exit(pos: OpenPosition, bar: pd.Series) -> Optional[str]:
    """Return exit reason string, or None to keep holding.

    FIX #9: Trailing stop — after TRAIL_ACTIVATE_BARS bars in profit,
    ratchet the stop loss upward to lock in gains.  This is structural
    (widely used in trend-following) and not data-snooped.
    """
    pos.bars_held += 1
    # Minimum hold period — prevent churn from same-day noise
    if pos.bars_held < HOLD_MIN_BARS:
        return None
    high, low, close = bar["high"], bar["low"], bar["close"]
    atr = float(bar.get("atr_14", 0)) if "atr_14" in bar.index else 0

    # FIX #9: Trailing stop ratchet — move stop to breakeven + 1 ATR
    # once position has been profitable for N bars.  Only moves UP (longs).
    if pos.bars_held >= TRAIL_ACTIVATE_BARS and atr > 0:
        if pos.direction == "LONG":
            unrealised = close - pos.entry_price
            if unrealised > 0:
                # Trailing stop = highest close seen minus TRAIL_LOCK_ATR * ATR
                # but never lower than the original stop
                trail_stop = close - TRAIL_LOCK_ATR * atr
                if trail_stop > pos.stop_loss:
                    pos.stop_loss = trail_stop
        else:  # SHORT
            unrealised = pos.entry_price - close
            if unrealised > 0:
                trail_stop = close + TRAIL_LOCK_ATR * atr
                if pos.stop_loss == 0 or trail_stop < pos.stop_loss:
                    pos.stop_loss = trail_stop

    if pos.direction == "LONG":
        if pos.stop_loss > 0 and low <= pos.stop_loss:
            return "stop_loss"
        if pos.take_profit > 0 and high >= pos.take_profit:
            return "take_profit"
    else:
        if pos.stop_loss > 0 and high >= pos.stop_loss:
            return "stop_loss"
        if pos.take_profit > 0 and low <= pos.take_profit:
            return "take_profit"
    if pos.bars_held >= HOLD_MAX_BARS:
        return "time_stop"
    return None


def exit_price_for(pos: OpenPosition, bar: pd.Series, reason: str) -> float:
    """Pick a realistic fill price given the exit trigger."""
    if reason == "stop_loss":
        return pos.stop_loss
    if reason == "take_profit":
        return pos.take_profit
    return bar["close"]


def close_position(pf: Portfolio, code: str, bar_date, bar: pd.Series,
                   reason: str, bars_per_year: float = 252):
    pos = pf.positions.pop(code)
    raw_exit = exit_price_for(pos, bar, reason)
    side = "SELL" if pos.direction == "LONG" else "BUY"
    atr = float(bar.get("atr_14", 0)) if "atr_14" in bar.index else 0
    fill = dynamic_slip(raw_exit, side, atr, ticker=code)
    if pos.direction == "LONG":
        pnl = (fill - pos.entry_price) * pos.qty
    else:
        pnl = (pos.entry_price - fill) * pos.qty
    pnl -= commission(pos.qty) * 2  # entry + exit
    # FIX #4: short borrow cost
    if pos.direction == "SHORT":
        pnl -= short_borrow_cost(pos.entry_price, pos.qty, pos.bars_held, bars_per_year)
    pf.cash += pos.qty * fill if pos.direction == "LONG" else pos.qty * (2 * pos.entry_price - fill)
    pnl_pct = pnl / (pos.entry_price * pos.qty) * 100 if pos.entry_price * pos.qty else 0
    pf.trades.append(ClosedTrade(
        code=pos.code, direction=pos.direction, qty=pos.qty,
        entry_price=round(pos.entry_price, 4), exit_price=round(fill, 4),
        entry_date=str(pos.entry_date.date()), exit_date=str(pd.Timestamp(bar_date).date()),
        pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2),
        bars_held=pos.bars_held, strategy=pos.strategy, exit_reason=reason,
    ))


# FIX #1: next-bar fill — open_position now takes the NEXT bar's open as fill
def open_position(pf: Portfolio, code: str, sig: dict, bar_date,
                  next_bar_open: float, regime_cfg: Optional[dict] = None,
                  vix_level: float = 20.0, vol_scalar: float = 1.0):
    """Open a position using next-bar open as the fill price.

    sig["entry"] is the signal-bar close (used for stop/TP reference).
    next_bar_open is the actual fill price — no same-bar bias.
    vol_scalar: FIX #12 portfolio vol targeting multiplier (0.3 – 1.5)
    """
    if len(pf.positions) >= MAX_OPEN_POSITIONS:
        return
    if code in pf.positions:
        return
    if next_bar_open <= 0:
        return
    side = "BUY" if sig["direction"] == "LONG" else "SELL"
    atr = sig.get("atr", 0)
    fill = dynamic_slip(next_bar_open, side, atr, ticker=code)
    # Sizing: scaled mode applies tier ladder, baseline uses fixed T0.
    if SCALED_MODE:
        wc, tier = working_capital_bt(pf.equity)
        _, _, pos_pct, pos_usd, risk_pct = tier
        size_budget = min(wc * pos_pct, pos_usd)
        risk_basis = wc
        risk_pct_use = risk_pct
    else:
        size_budget = min(pf.equity * MAX_POSITION_PCT, MAX_POSITION_USD)
        risk_basis = pf.equity
        risk_pct_use = MAX_RISK_PCT

    # FIX #3: regime-based size scaling
    if regime_cfg:
        size_budget *= regime_cfg.get("size_mult", 1.0)
        # VIX-based volatility scaling
        vix_scale = max(0.4, min(1.0, 20.0 / vix_level)) if vix_level > 0 else 1.0
        size_budget *= vix_scale

    # FIX #7: REMOVED STRATEGY_SIZE_MULT — all strategies get equal capital.
    # (was: strat_mult = STRATEGY_SIZE_MULT.get(sig["strategy"], 1.0))

    # FIX #10: Drawdown circuit breaker
    if pf.peak_equity > 0:
        current_dd = (pf.peak_equity - pf.equity) / pf.peak_equity
        if current_dd >= DD_BRAKE_PCT:
            size_budget *= DD_BRAKE_MULT

    # FIX #12: Portfolio vol targeting
    size_budget *= vol_scalar

    qty = int(size_budget / fill)
    if sig["stop"] and abs(fill - sig["stop"]) > 0:
        risk_qty = int(risk_basis * risk_pct_use / abs(fill - sig["stop"]))
        qty = min(qty, risk_qty)
    if qty <= 0:
        return
    cost = qty * fill + commission(qty)
    if cost > pf.cash:
        return
    pf.cash -= cost
    pf.positions[code] = OpenPosition(
        code=code, direction=sig["direction"], qty=qty,
        entry_price=fill, entry_date=pd.Timestamp(bar_date),
        stop_loss=sig["stop"] or 0, take_profit=sig["take_profit"] or 0,
        strategy=sig["strategy"],
    )


def mark_to_market(pf: Portfolio, latest: Dict[str, float]):
    mv = 0.0
    for code, pos in pf.positions.items():
        px = latest.get(code, pos.entry_price)
        if pos.direction == "LONG":
            mv += pos.qty * px
        else:
            mv += pos.qty * (2 * pos.entry_price - px)
    pf.equity = pf.cash + mv
    if pf.equity > pf.peak_equity:
        pf.peak_equity = pf.equity
    dd = (pf.peak_equity - pf.equity) / pf.peak_equity if pf.peak_equity else 0
    if dd > pf.max_drawdown:
        pf.max_drawdown = dd


# ── Metrics ────────────────────────────────────────────────────

def compute_metrics(pf: Portfolio, start_capital: float,
                    bars_per_year: float = 252,
                    benchmark_curve: Optional[List[float]] = None) -> dict:
    trades = pf.trades
    n = len(trades)
    if n == 0:
        return {"trades": 0, "final_equity": pf.equity}
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    pf_ratio = gross_win / gross_loss if gross_loss > 0 else float("inf")

    # Sharpe from equity curve daily returns
    eq = pd.Series([e for _, e in pf.equity_curve])
    rets = eq.pct_change().dropna()
    if len(rets) > 1 and rets.std() > 0:
        sharpe = (rets.mean() / rets.std()) * math.sqrt(bars_per_year)
    else:
        sharpe = 0.0

    total_pnl = sum(t.pnl for t in trades)
    metrics = {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / n * 100, 2),
        "total_pnl": round(total_pnl, 2),
        "return_pct": round((pf.equity - start_capital) / start_capital * 100, 2),
        "profit_factor": round(pf_ratio, 2) if pf_ratio != float("inf") else "inf",
        "avg_win": round(sum(t.pnl for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t.pnl for t in losses) / len(losses), 2) if losses else 0,
        "max_drawdown_pct": round(pf.max_drawdown * 100, 2),
        "sharpe": round(sharpe, 2),
        "final_equity": round(pf.equity, 2),
        "peak_equity": round(pf.peak_equity, 2),
        "best_trade": round(max(t.pnl for t in trades), 2),
        "worst_trade": round(min(t.pnl for t in trades), 2),
        "avg_hold_bars": round(sum(t.bars_held for t in trades) / n, 1),
    }

    # FIX #5: Benchmark-relative metrics
    if benchmark_curve and len(benchmark_curve) == len(pf.equity_curve):
        bench_series = pd.Series(benchmark_curve)
        bench_rets = bench_series.pct_change().dropna()
        strat_rets = rets
        # Align lengths
        min_len = min(len(strat_rets), len(bench_rets))
        if min_len > 10:
            sr = strat_rets.iloc[:min_len].values
            br = bench_rets.iloc[:min_len].values
            # Beta
            cov = np.cov(sr, br)
            beta = cov[0, 1] / cov[1, 1] if cov[1, 1] > 0 else 1.0
            # Alpha (annualized Jensen's alpha)
            alpha = (sr.mean() - beta * br.mean()) * bars_per_year
            # Excess return
            total_strat = (eq.iloc[-1] / eq.iloc[0]) - 1 if eq.iloc[0] > 0 else 0
            total_bench = (bench_series.iloc[-1] / bench_series.iloc[0]) - 1 if bench_series.iloc[0] > 0 else 0
            excess = total_strat - total_bench
            # Up/down capture
            up_mask = br > 0
            dn_mask = br < 0
            up_capture = (sr[up_mask].mean() / br[up_mask].mean() * 100) if up_mask.sum() > 5 else None
            dn_capture = (sr[dn_mask].mean() / br[dn_mask].mean() * 100) if dn_mask.sum() > 5 else None

            metrics["beta"] = round(beta, 3)
            metrics["alpha_annual"] = round(alpha * 100, 2)  # as percent
            metrics["excess_return_pct"] = round(excess * 100, 2)
            metrics["up_capture_pct"] = round(up_capture, 1) if up_capture else None
            metrics["down_capture_pct"] = round(dn_capture, 1) if dn_capture else None

    return metrics


def per_strategy_metrics(pf: Portfolio) -> dict:
    out = {}
    for strat in [s.name for s in STRATEGIES]:
        ts = [t for t in pf.trades if t.strategy == strat]
        if not ts:
            out[strat] = {"trades": 0}
            continue
        wins = [t for t in ts if t.pnl > 0]
        gw = sum(t.pnl for t in wins)
        gl = abs(sum(t.pnl for t in ts if t.pnl <= 0))
        out[strat] = {
            "trades": len(ts),
            "wins": len(wins),
            "win_rate_pct": round(len(wins) / len(ts) * 100, 1),
            "total_pnl": round(sum(t.pnl for t in ts), 2),
            "profit_factor": round(gw / gl, 2) if gl > 0 else "inf",
            "avg_pnl": round(sum(t.pnl for t in ts) / len(ts), 2),
        }
    return out


# ── Main runner ────────────────────────────────────────────────

def run_backtest(start: str, end: str, universe: List[str],
                 interval: str = "1d", quiet: bool = False) -> dict:
    bars_per_year = {"1d": 252, "1h": 252 * 6.5, "60m": 252 * 6.5}.get(interval, 252)
    log = (lambda *a, **k: None) if quiet else print
    log(f"Backtest {start} → {end}  ({len(universe)} tickers, interval={interval}, "
        f"scaled={SCALED_MODE}, regime={REGIME_MODE})")
    log("Fetching history…")
    history: Dict[str, pd.DataFrame] = {}
    for tk in universe:
        df = fetch_history(tk, start, end, interval=interval)
        if df is None or len(df) < 60:
            log(f"  - skip {tk} (insufficient data)")
            continue
        history[tk] = enrich(df)
        log(f"  + {tk}: {len(df)} bars")

    if not history:
        return {"error": "no data"}

    # FIX #6: Pre-fetch SPY and VIX with 250 extra days BEFORE the backtest
    # start date so the 200-day SMA is valid from day 1.  Without this, any
    # backtest starting < 200 trading days after the earliest available data
    # (e.g. 2020-01-01) mis-classifies regime for months (the Covid crash bug).
    prefetch_start = (pd.Timestamp(start) - pd.Timedelta(days=370)).strftime("%Y-%m-%d")
    spy_full = None   # full SPY series (prefetched) for regime engine
    vix_full = None   # full VIX series (prefetched) for regime engine

    # Fetch VIX for regime classification (if regime mode)
    vix_history = None
    if REGIME_MODE:
        vix_raw = fetch_history("^VIX", prefetch_start, end, interval=interval)
        if vix_raw is not None and len(vix_raw) > 0:
            vix_full = vix_raw
            # Also store the backtest-period slice in history for downstream use
            vix_bt = vix_raw.loc[vix_raw.index >= pd.Timestamp(start)]
            if len(vix_bt) > 0:
                history["^VIX"] = vix_bt
            else:
                history["^VIX"] = vix_raw
            vix_history = vix_raw
            log(f"  + ^VIX: {len(vix_raw)} bars (regime indicator, prefetched from {prefetch_start})")
        else:
            log("  ! VIX data unavailable — regime will use default VIX=20")

    # Ensure SPY is in the dataset for regime + benchmark
    if "SPY" not in history:
        spy_df = fetch_history("SPY", start, end, interval=interval)
        if spy_df is not None and len(spy_df) >= 60:
            history["SPY"] = enrich(spy_df)
            log(f"  + SPY: {len(spy_df)} bars (benchmark)")

    # Prefetch full SPY for regime SMA200 computation
    if REGIME_MODE:
        spy_pre = fetch_history("SPY", prefetch_start, end, interval=interval)
        if spy_pre is not None and len(spy_pre) >= 200:
            spy_full = enrich(spy_pre)
            log(f"  + SPY regime series: {len(spy_pre)} bars (prefetched from {prefetch_start})")

    # Build a unified date index across all tickers (union)
    tradable_tickers = [t for t in history if t != "^VIX"]
    all_dates = sorted(set().union(*[history[t].index for t in tradable_tickers]))
    pf = Portfolio(cash=START_CAPITAL, equity=START_CAPITAL, peak_equity=START_CAPITAL)

    # Pre-compute regime series for every bar
    regime_series = {}
    if REGIME_MODE:
        regime_series = build_regime_series(history, all_dates,
                                            spy_full=spy_full, vix_full=vix_full)

    # Build SPY benchmark curve for relative metrics
    spy_df = history.get("SPY")
    benchmark_curve = []

    # FIX #1: pending signals from prior bar → execute on this bar's open
    pending_signals: List[dict] = []

    # FIX #11: Pre-compute rolling correlation matrix for correlation guard
    # Build a close-price DataFrame for all tradable tickers
    close_matrix = pd.DataFrame({
        t: history[t]["close"] for t in tradable_tickers if t in history
    })

    log(f"Replaying {len(all_dates)} bars across {len(tradable_tickers)} tickers…")
    last_progress = 0
    regime_stats = {"bull": 0, "flat": 0, "bear": 0}

    for i, dt in enumerate(all_dates):
        # Get current regime
        regime_info = regime_series.get(dt, {"regime": "bull", "vix": 20.0})
        regime = regime_info["regime"]
        regime_cfg = REGIME_CONFIG.get(regime, REGIME_CONFIG["bull"]) if REGIME_MODE else None
        vix_level = regime_info.get("vix", 20.0)
        regime_stats[regime] = regime_stats.get(regime, 0) + 1

        # Step 1: process exits on existing positions using TODAY's bar
        latest_prices: Dict[str, float] = {}
        for code in list(pf.positions.keys()):
            if code not in history or dt not in history[code].index:
                continue
            bar = history[code].loc[dt]
            latest_prices[code] = float(bar["close"])
            reason = check_exit(pf.positions[code], bar)
            if reason:
                close_position(pf, code, dt, bar, reason, bars_per_year)

        # Step 2: collect latest_prices for non-position tickers (for MTM)
        for code in tradable_tickers:
            if code in latest_prices or dt not in history[code].index:
                continue
            latest_prices[code] = float(history[code].loc[dt, "close"])

        # FIX #12: Compute portfolio vol scalar for this bar (if enabled)
        vol_scalar = 1.0
        if VOL_TARGETING_ENABLED:
            eq_series = pd.Series([e for _, e in pf.equity_curve])
            if len(eq_series) >= VOL_LOOKBACK + 1:
                recent_rets = eq_series.pct_change().dropna().tail(VOL_LOOKBACK)
                realised_vol = float(recent_rets.std()) * math.sqrt(252) if len(recent_rets) > 5 else TARGET_ANNUAL_VOL
                if realised_vol > 0:
                    vol_scalar = TARGET_ANNUAL_VOL / realised_vol
                    vol_scalar = max(VOL_SCALE_MIN, min(VOL_SCALE_MAX, vol_scalar))

        # FIX #1: Execute pending signals from PREVIOUS bar at THIS bar's open
        if pending_signals and len(pf.positions) < MAX_OPEN_POSITIONS:
            # FIX #3: filter by regime allowed strategies
            allowed_strats = None
            if regime_cfg:
                allowed_strats = regime_cfg.get("allowed_strategies")

            # FIX #11: build current correlation snapshot for guard
            existing_codes = list(pf.positions.keys())

            # FIX #2: signals are ALREADY ranked by score (sorted below)
            for sig in pending_signals:
                if len(pf.positions) >= MAX_OPEN_POSITIONS:
                    break
                code = sig["code"]
                if code in pf.positions:
                    continue
                if code not in history or dt not in history[code].index:
                    continue
                # Regime gating
                if allowed_strats and sig["strategy"] not in allowed_strats:
                    continue
                # Check regime exposure cap
                if regime_cfg:
                    mv_total = sum(
                        abs(p.qty * latest_prices.get(c, p.entry_price))
                        for c, p in pf.positions.items()
                    )
                    exposure = mv_total / pf.equity if pf.equity > 0 else 0
                    if exposure >= regime_cfg.get("max_exposure", 1.5):
                        break

                # FIX #11: Correlation guard — skip if >0.70 corr with existing
                if existing_codes and code in close_matrix.columns and dt in close_matrix.index:
                    dt_idx = close_matrix.index.get_loc(dt)
                    if dt_idx >= CORR_LOOKBACK:
                        skip = False
                        for ec in existing_codes:
                            if ec in close_matrix.columns:
                                corr_window = close_matrix.iloc[dt_idx - CORR_LOOKBACK:dt_idx + 1]
                                if code in corr_window.columns and ec in corr_window.columns:
                                    pair = corr_window[[code, ec]].dropna()
                                    if len(pair) > 20:
                                        c_val = pair[code].corr(pair[ec])
                                        if not pd.isna(c_val) and abs(c_val) > CORR_THRESHOLD:
                                            skip = True
                                            break
                        if skip:
                            continue

                # Fill at this bar's OPEN price (not close)
                next_open = float(history[code].loc[dt, "open"])
                open_position(pf, code, sig, dt, next_open, regime_cfg, vix_level,
                              vol_scalar=vol_scalar)
                # Update existing_codes for subsequent correlation checks
                if code in pf.positions:
                    existing_codes.append(code)

        # Step 3: Generate signals on TODAY's data → queue for NEXT bar fill
        # FIX #2: Collect ALL signals across ALL tickers, then rank
        all_signals: List[dict] = []
        if len(pf.positions) < MAX_OPEN_POSITIONS:
            for code in tradable_tickers:
                if code == "SPY" and REGIME_MODE:
                    continue  # don't trade the regime indicator
                df = history[code]
                if code in pf.positions:
                    continue
                if dt not in df.index:
                    continue
                idx = df.index.get_loc(dt)
                if idx < 55:
                    continue
                window = df.iloc[:idx + 1]
                sigs = evaluate_signals(code, window)
                if sigs:
                    # Take best signal per ticker
                    best = max(sigs, key=lambda s: s["score"])
                    # FIX #8: REMOVED weight_overrides — signal scores must
                    # reflect true strategy conviction, not regime-snooped boosts
                    all_signals.append(best)

        # FIX #2: Rank all signals by score (best first) — no ticker-order bias
        pending_signals = sorted(all_signals, key=lambda s: s["score"], reverse=True)

        # Step 4: mark to market and snapshot equity curve
        mark_to_market(pf, latest_prices)

        # v4: Credit risk-free rate on idle cash (T-bill yield, daily accrual)
        try:
            yr = pd.Timestamp(dt).year
            rf_annual = RISK_FREE_RATES.get(yr, 0.03)
            rf_daily = rf_annual / 252
            idle_cash = max(pf.cash, 0)
            rf_income = idle_cash * rf_daily
            pf.cash += rf_income
            pf.equity += rf_income
        except Exception:
            pass

        pf.equity_curve.append((str(pd.Timestamp(dt).date()), round(pf.equity, 2)))

        # Benchmark (SPY buy-and-hold, rebased to start capital)
        if spy_df is not None and dt in spy_df.index:
            if len(benchmark_curve) == 0:
                _spy_start = float(spy_df.loc[dt, "close"])
                benchmark_curve.append(START_CAPITAL)
            else:
                benchmark_curve.append(
                    START_CAPITAL * float(spy_df.loc[dt, "close"]) / _spy_start
                )
        elif benchmark_curve:
            benchmark_curve.append(benchmark_curve[-1])

        pct = int(i / len(all_dates) * 100)
        if pct >= last_progress + 10:
            last_progress = pct
            r_tag = f"  regime={regime}" if REGIME_MODE else ""
            print(f"  …{pct}%  equity ${pf.equity:,.0f}  pos {len(pf.positions)}  "
                  f"trades {len(pf.trades)}{r_tag}")

    # Final close-out at last bar
    last_dt = all_dates[-1]
    for code in list(pf.positions.keys()):
        if code not in history or last_dt not in history[code].index:
            df = history.get(code)
            if df is not None and len(df):
                close_position(pf, code, df.index[-1], df.iloc[-1], "end_of_backtest", bars_per_year)
        else:
            close_position(pf, code, last_dt, history[code].loc[last_dt], "end_of_backtest", bars_per_year)

    metrics = compute_metrics(pf, START_CAPITAL, bars_per_year=bars_per_year,
                              benchmark_curve=benchmark_curve if benchmark_curve else None)
    by_strategy = per_strategy_metrics(pf)

    result = {
        "start": start,
        "end": end,
        "interval": interval,
        "scaled": SCALED_MODE,
        "regime_mode": REGIME_MODE,
        "tickers": [t for t in history.keys() if t != "^VIX"],
        "n_bars": len(all_dates),
        "config": {
            "start_capital": START_CAPITAL,
            "max_position_pct": MAX_POSITION_PCT,
            "max_position_usd": MAX_POSITION_USD,
            "min_signal_score": MIN_SIGNAL_SCORE,
            "max_open_positions": MAX_OPEN_POSITIONS,
            "base_slippage_bps": BASE_SLIPPAGE_BPS,
            "commission_per_share": COMMISSION_PER_SHARE,
            "hold_max_bars": HOLD_MAX_BARS,
            "short_borrow_bps": SHORT_BORROW_ANNUAL_BPS,
            "regime_mode": REGIME_MODE,
        },
        "metrics": metrics,
        "by_strategy": by_strategy,
        "equity_curve": pf.equity_curve,
        "trades": [asdict(t) for t in pf.trades[-500:]],
    }

    # Regime breakdown
    if REGIME_MODE:
        result["regime_bars"] = regime_stats
        # Per-regime trade P&L
        regime_trades = {"bull": [], "flat": [], "bear": []}
        for t in pf.trades:
            # Classify the trade's entry date regime
            try:
                edt = pd.Timestamp(t.entry_date)
                # Find closest regime date
                closest = min(regime_series.keys(), key=lambda d: abs((d - edt).total_seconds()))
                r = regime_series[closest]["regime"]
                regime_trades[r].append(t.pnl)
            except Exception:
                regime_trades["bull"].append(t.pnl)
        result["regime_pnl"] = {
            r: {"trades": len(pnls), "total_pnl": round(sum(pnls), 2),
                "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0}
            for r, pnls in regime_trades.items()
        }

    if benchmark_curve:
        result["benchmark_curve"] = [
            (pf.equity_curve[i][0], round(benchmark_curve[i], 2))
            for i in range(min(len(pf.equity_curve), len(benchmark_curve)))
        ]

    return result


# ── Walk-forward harness ──────────────────────────────────────

def _select_strategies(by_strategy: dict, mode: str) -> List[str]:
    """
    Selection rules:
      - "pf_wr": train PF >= 1.10 AND win-rate >= 35%
      - "pf":    train PF >= 1.10
    Min-trades floor of 5.
    """
    keep = []
    for name, m in by_strategy.items():
        if m.get("trades", 0) < 5:
            continue
        pf = m.get("profit_factor", 0)
        if pf == "inf":
            pf = 99
        wr = m.get("win_rate_pct", 0)
        if mode == "pf_wr" and pf >= 1.10 and wr >= 35:
            keep.append(name)
        elif mode == "pf" and pf >= 1.10:
            keep.append(name)
    return keep


def _run_with_strategies(start, end, universe, interval, names):
    global STRATEGIES
    full = STRATEGIES
    STRATEGIES = [s for s in full if s.name in names] if names else []
    try:
        if not STRATEGIES:
            return None
        return run_backtest(start, end, universe, interval=interval, quiet=True)
    finally:
        STRATEGIES = full


def run_walk_forward(train: tuple, test: tuple, universe: List[str],
                     interval: str = "1d") -> dict:
    """Single-window walk-forward: train on one period, test on next."""
    print(f"\n=== WALK-FORWARD ===")
    print(f"  TRAIN: {train[0]} → {train[1]}")
    print(f"  TEST:  {test[0]} → {test[1]}")
    train_res = run_backtest(train[0], train[1], universe, interval=interval, quiet=True)
    print(f"  Train: trades={train_res['metrics']['trades']}  "
          f"return={train_res['metrics']['return_pct']}%  PF={train_res['metrics']['profit_factor']}")

    keep_pf_wr = _select_strategies(train_res["by_strategy"], "pf_wr")
    keep_pf = _select_strategies(train_res["by_strategy"], "pf")
    print(f"  Selected (PF+WR): {keep_pf_wr or '— none —'}")
    print(f"  Selected (PF-only): {keep_pf or '— none —'}")

    test_full = run_backtest(test[0], test[1], universe, interval=interval, quiet=True)
    m = test_full["metrics"]
    print(f"  Test (all):     trades={m['trades']:>5}  return={m['return_pct']:>+7.2f}%  "
          f"Sharpe={m['sharpe']:>5}  DD={m['max_drawdown_pct']}%")

    test_pf_wr = _run_with_strategies(test[0], test[1], universe, interval, keep_pf_wr)
    if test_pf_wr:
        m = test_pf_wr["metrics"]
        print(f"  Test (PF+WR):   trades={m['trades']:>5}  return={m['return_pct']:>+7.2f}%  "
              f"Sharpe={m['sharpe']:>5}  DD={m['max_drawdown_pct']}%")
    else:
        print(f"  Test (PF+WR): — no strategies passed —")

    test_pf = _run_with_strategies(test[0], test[1], universe, interval, keep_pf)
    if test_pf:
        m = test_pf["metrics"]
        print(f"  Test (PF-only): trades={m['trades']:>5}  return={m['return_pct']:>+7.2f}%  "
              f"Sharpe={m['sharpe']:>5}  DD={m['max_drawdown_pct']}%")
    else:
        print(f"  Test (PF-only): — no strategies passed —")

    decision = "pf_wr"
    if test_pf and test_pf_wr:
        a = test_pf["metrics"]; b = test_pf_wr["metrics"]
        if a["return_pct"] > b["return_pct"] and a["sharpe"] >= b["sharpe"]:
            decision = "pf"
    elif test_pf and not test_pf_wr:
        decision = "pf"
    print(f"  Decision: '{decision}'")

    train_r = train_res["metrics"].get("return_pct", 0)
    test_r = test_full["metrics"].get("return_pct", 0)
    overfit = train_r > 5 and (test_r / max(train_r, 1)) < 0.30
    print(f"  Overfit flag: {'YES' if overfit else 'no'}")

    return {
        "train": train_res["metrics"],
        "train_by_strategy": train_res["by_strategy"],
        "test_full": test_full["metrics"],
        "test_full_by_strategy": test_full["by_strategy"],
        "test_pf_wr": test_pf_wr["metrics"] if test_pf_wr else None,
        "test_pf_only": test_pf["metrics"] if test_pf else None,
        "selected_pf_wr": keep_pf_wr,
        "selected_pf_only": keep_pf,
        "decision": decision,
        "overfit_flag": overfit,
    }


# FIX #5: Rolling walk-forward with multiple windows
def run_rolling_walk_forward(universe: List[str], interval: str = "1d",
                             train_years: int = 3, test_years: int = 1,
                             start_year: int = 2018) -> dict:
    """Rolling walk-forward: slide a train/test window across the data.

    Example with defaults (train=3yr, test=1yr, start=2018):
      Window 1: train 2018-2020, test 2021
      Window 2: train 2019-2021, test 2022
      Window 3: train 2020-2022, test 2023
      Window 4: train 2021-2023, test 2024
      Window 5: train 2022-2024, test 2025
    """
    current_year = datetime.now().year
    windows = []
    y = start_year
    while y + train_years + test_years <= current_year + 1:
        train_start = f"{y}-01-01"
        train_end = f"{y + train_years - 1}-12-31"
        test_start = f"{y + train_years}-01-01"
        test_end = f"{y + train_years + test_years - 1}-12-31"
        # Clamp test_end to today
        today = datetime.now().strftime("%Y-%m-%d")
        if test_end > today:
            test_end = today
        windows.append({
            "train": (train_start, train_end),
            "test": (test_start, test_end),
        })
        y += 1

    print(f"\n=== ROLLING WALK-FORWARD ({len(windows)} windows) ===")
    results = []
    for i, w in enumerate(windows):
        print(f"\n--- Window {i + 1}/{len(windows)} ---")
        wf = run_walk_forward(w["train"], w["test"], universe, interval)
        wf["window"] = w
        results.append(wf)

    # Aggregate metrics across all test windows
    test_returns = [r["test_full"]["return_pct"] for r in results if r.get("test_full")]
    test_sharpes = [r["test_full"]["sharpe"] for r in results if r.get("test_full")]
    overfit_count = sum(1 for r in results if r.get("overfit_flag"))

    summary = {
        "n_windows": len(windows),
        "windows": [r["window"] for r in results],
        "test_returns": test_returns,
        "test_sharpes": test_sharpes,
        "avg_test_return": round(np.mean(test_returns), 2) if test_returns else 0,
        "avg_test_sharpe": round(np.mean(test_sharpes), 2) if test_sharpes else 0,
        "min_test_return": round(min(test_returns), 2) if test_returns else 0,
        "max_test_return": round(max(test_returns), 2) if test_returns else 0,
        "overfit_flags": overfit_count,
        "window_results": results,
    }

    print(f"\n=== ROLLING SUMMARY ===")
    print(f"  Windows: {len(windows)}")
    print(f"  Avg test return: {summary['avg_test_return']:+.2f}%")
    print(f"  Avg test Sharpe: {summary['avg_test_sharpe']:.2f}")
    print(f"  Return range: {summary['min_test_return']:+.2f}% to {summary['max_test_return']:+.2f}%")
    print(f"  Overfit flags: {overfit_count}/{len(windows)}")

    return summary


# ── Hybrid backtest (core + tactical simulation) ─────────────

# Core simulation approach: Since we lack point-in-time fundamentals,
# we use a fixed list of "blue chip" quality names as a proxy for what
# the core sleeve would have held.  This is approximate but honest —
# it measures the capital deployment effect, not stock-picking alpha.
#
# The hybrid allocates CORE_PCT to an equal-weight buy-and-hold basket
# (rebalanced quarterly), and TACTICAL_PCT to the existing signal engine.
# Idle cash earns T-bill yield.  This lets us compare:
#   1. Tactical-only (current system, 100% to signals + idle cash)
#   2. Hybrid (split between core + tactical + cash floor)

# NOTE: Core proxy basket has survivorship bias — these are today's mega-cap
# winners, not a basket one would have selected in 2019. This overstates core
# sleeve returns. A more honest proxy would be a broad equal-weight ETF (RSP),
# but we keep individual names to model rebalance friction realistically.
# The hybrid backtest results should be interpreted with this caveat.
CORE_PROXY_NAMES = [
    "MSFT", "AAPL", "GOOGL", "JPM", "UNH", "JNJ", "PG", "XOM",
]
# Changed: replaced META, AVGO, CRM (recent winners) with JNJ, PG, XOM
# (stable blue-chips that were investable throughout the full backtest period)
HYBRID_CORE_PCT = 0.55       # 55% to core basket
HYBRID_TACTICAL_PCT = 0.33   # 33% to tactical engine
HYBRID_CASH_FLOOR = 0.12     # 12% cash floor


def run_hybrid_backtest(start: str, end: str, universe: List[str],
                        interval: str = "1d") -> dict:
    """Run a hybrid backtest: core buy-and-hold basket + tactical signals.

    Core basket: equal-weight top quality names, rebalanced quarterly.
    Tactical: same signal engine as run_backtest(), but sized to TACTICAL_PCT.
    Cash: earns T-bill yield.

    Returns combined metrics + sleeve-level attribution.
    """
    bars_per_year = 252
    print(f"HYBRID Backtest {start} → {end}")
    print(f"  Core: {HYBRID_CORE_PCT:.0%} to {len(CORE_PROXY_NAMES)} names")
    print(f"  Tactical: {HYBRID_TACTICAL_PCT:.0%} to signal engine")
    print(f"  Cash floor: {HYBRID_CASH_FLOOR:.0%}")

    # Fetch all data
    history: Dict[str, pd.DataFrame] = {}
    for tk in set(universe + CORE_PROXY_NAMES):
        df = fetch_history(tk, start, end, interval=interval)
        if df is not None and len(df) >= 60:
            history[tk] = enrich(df)

    if not history:
        return {"error": "no data"}

    # Prefetch for regime
    prefetch_start = (pd.Timestamp(start) - pd.Timedelta(days=370)).strftime("%Y-%m-%d")
    spy_full = vix_full = None
    vix_raw = fetch_history("^VIX", prefetch_start, end, interval=interval)
    if vix_raw is not None and len(vix_raw) > 0:
        vix_full = vix_raw
    if "SPY" not in history:
        spy_df = fetch_history("SPY", start, end, interval=interval)
        if spy_df is not None and len(spy_df) >= 60:
            history["SPY"] = enrich(spy_df)
    spy_pre = fetch_history("SPY", prefetch_start, end, interval=interval)
    if spy_pre is not None and len(spy_pre) >= 200:
        spy_full = enrich(spy_pre)

    tradable_tickers = [t for t in history if t != "^VIX"]
    all_dates = sorted(set().union(*[history[t].index for t in tradable_tickers]))

    regime_series = build_regime_series(history, all_dates,
                                        spy_full=spy_full, vix_full=vix_full)

    # Initialize capital split
    total_capital = START_CAPITAL
    core_capital = total_capital * HYBRID_CORE_PCT
    tactical_capital = total_capital * HYBRID_TACTICAL_PCT
    cash_reserve = total_capital * HYBRID_CASH_FLOOR

    # Core basket: equal-weight, buy on first available bar
    core_names_available = [n for n in CORE_PROXY_NAMES if n in history]
    core_holdings: Dict[str, dict] = {}  # {ticker: {qty, entry_price}}
    core_realized_pnl = 0.0
    core_initialized = False
    last_rebalance_quarter = None

    # Tactical portfolio (uses same engine, but with reduced capital)
    tac_pf = Portfolio(cash=tactical_capital, equity=tactical_capital,
                       peak_equity=tactical_capital)
    pending_signals: List[dict] = []

    # Combined tracking
    combined_curve = []
    combined_peak = total_capital
    combined_max_dd = 0.0

    spy_df = history.get("SPY")
    benchmark_curve = []
    _spy_start = None

    close_matrix = pd.DataFrame({
        t: history[t]["close"] for t in tradable_tickers if t in history
    })

    core_tickers_set = set(core_names_available)

    for i, dt in enumerate(all_dates):
        regime_info = regime_series.get(dt, {"regime": "bull", "vix": 20.0})
        regime = regime_info["regime"]
        regime_cfg = REGIME_CONFIG.get(regime, REGIME_CONFIG["bull"])
        vix_level = regime_info.get("vix", 20.0)

        # ── Core sleeve: initialize or rebalance quarterly ──────
        quarter = f"{pd.Timestamp(dt).year}Q{(pd.Timestamp(dt).month - 1) // 3 + 1}"

        if not core_initialized:
            # Buy equal-weight basket (with slippage)
            per_name = core_capital / max(len(core_names_available), 1)
            for name in core_names_available:
                if name in history and dt in history[name].index:
                    px = float(history[name].loc[dt, "open"])
                    if px > 0:
                        atr = float(history[name].loc[dt, "atr_14"]) if "atr_14" in history[name].columns else 0
                        fill_px = dynamic_slip(px, "BUY", atr, ticker=name)
                        qty = int(per_name / fill_px)
                        if qty > 0:
                            core_holdings[name] = {"qty": qty, "entry_price": fill_px}
                            core_capital -= qty * fill_px
            core_initialized = True
            last_rebalance_quarter = quarter

        elif quarter != last_rebalance_quarter and regime != "bear":
            # Quarterly rebalance: sell all (with slippage), re-buy equal weight
            for name, h in list(core_holdings.items()):
                if name in history and dt in history[name].index:
                    px = float(history[name].loc[dt, "close"])
                    atr = float(history[name].loc[dt, "atr_14"]) if "atr_14" in history[name].columns else 0
                    sell_fill = dynamic_slip(px, "SELL", atr, ticker=name)
                    pnl = (sell_fill - h["entry_price"]) * h["qty"]
                    core_realized_pnl += pnl
                    core_capital += h["qty"] * sell_fill
            core_holdings.clear()

            # Re-buy with current core capital + any gains (with slippage)
            total_core_value = core_capital
            per_name = total_core_value / max(len(core_names_available), 1)
            for name in core_names_available:
                if name in history and dt in history[name].index:
                    px = float(history[name].loc[dt, "open"])
                    if px > 0:
                        atr = float(history[name].loc[dt, "atr_14"]) if "atr_14" in history[name].columns else 0
                        fill_px = dynamic_slip(px, "BUY", atr, ticker=name)
                        qty = int(per_name / fill_px)
                        if qty > 0:
                            core_holdings[name] = {"qty": qty, "entry_price": fill_px}
                            core_capital -= qty * fill_px
            last_rebalance_quarter = quarter

        # ── Tactical sleeve: same logic as run_backtest ─────────
        # Process exits
        for code in list(tac_pf.positions.keys()):
            if code not in history or dt not in history[code].index:
                continue
            bar = history[code].loc[dt]
            reason = check_exit(tac_pf.positions[code], bar)
            if reason:
                close_position(tac_pf, code, dt, bar, reason, bars_per_year)

        # Execute pending signals
        if pending_signals and len(tac_pf.positions) < MAX_OPEN_POSITIONS:
            allowed_strats = regime_cfg.get("allowed_strategies")
            for sig in pending_signals:
                if len(tac_pf.positions) >= MAX_OPEN_POSITIONS:
                    break
                code = sig["code"]
                if code in tac_pf.positions or code not in history or dt not in history[code].index:
                    continue
                if code in core_tickers_set:
                    continue  # R1: no overlap with core
                if allowed_strats and sig["strategy"] not in allowed_strats:
                    continue
                next_open = float(history[code].loc[dt, "open"])
                open_position(tac_pf, code, sig, dt, next_open, regime_cfg, vix_level)

        # Generate new signals
        all_signals: List[dict] = []
        if len(tac_pf.positions) < MAX_OPEN_POSITIONS:
            for code in tradable_tickers:
                if code == "SPY" or code in tac_pf.positions or code in core_tickers_set:
                    continue
                df = history[code]
                if dt not in df.index:
                    continue
                idx = df.index.get_loc(dt)
                if idx < 55:
                    continue
                window = df.iloc[:idx + 1]
                sigs = evaluate_signals(code, window)
                if sigs:
                    best = max(sigs, key=lambda s: s["score"])
                    all_signals.append(best)
        pending_signals = sorted(all_signals, key=lambda s: s["score"], reverse=True)

        # ── Mark to market (both sleeves) ───────────────────────
        # Core MTM
        core_mtm = core_capital  # uninvested core cash
        for name, h in core_holdings.items():
            if name in history and dt in history[name].index:
                px = float(history[name].loc[dt, "close"])
                core_mtm += h["qty"] * px
            else:
                core_mtm += h["qty"] * h["entry_price"]

        # Tactical MTM
        tac_latest = {}
        for code in tradable_tickers:
            if dt in history[code].index:
                tac_latest[code] = float(history[code].loc[dt, "close"])
        mark_to_market(tac_pf, tac_latest)

        # T-bill on idle cash (core idle + tactical idle + cash reserve)
        try:
            yr = pd.Timestamp(dt).year
            rf_daily = RISK_FREE_RATES.get(yr, 0.03) / 252
            idle = max(core_capital, 0) + max(tac_pf.cash, 0) + cash_reserve
            rf_income = idle * rf_daily
            cash_reserve += rf_income
        except Exception:
            pass

        combined_equity = core_mtm + tac_pf.equity + cash_reserve
        combined_curve.append((str(pd.Timestamp(dt).date()), round(combined_equity, 2)))
        if combined_equity > combined_peak:
            combined_peak = combined_equity
        dd = (combined_peak - combined_equity) / combined_peak if combined_peak else 0
        if dd > combined_max_dd:
            combined_max_dd = dd

        # Benchmark
        if spy_df is not None and dt in spy_df.index:
            if _spy_start is None:
                _spy_start = float(spy_df.loc[dt, "close"])
                benchmark_curve.append(START_CAPITAL)
            else:
                benchmark_curve.append(
                    START_CAPITAL * float(spy_df.loc[dt, "close"]) / _spy_start
                )
        elif benchmark_curve:
            benchmark_curve.append(benchmark_curve[-1])

    # Final close-out
    last_dt = all_dates[-1]
    # Close tactical
    for code in list(tac_pf.positions.keys()):
        if code in history and last_dt in history[code].index:
            close_position(tac_pf, code, last_dt, history[code].loc[last_dt],
                          "end_of_backtest", bars_per_year)
    # Close core
    for name, h in core_holdings.items():
        if name in history and last_dt in history[name].index:
            px = float(history[name].loc[last_dt, "close"])
            core_realized_pnl += (px - h["entry_price"]) * h["qty"]
            core_capital += h["qty"] * px

    final_core = core_capital
    final_tactical = tac_pf.equity
    final_cash = cash_reserve
    final_combined = final_core + final_tactical + final_cash

    # Compute combined Sharpe from equity curve
    eq = pd.Series([e for _, e in combined_curve])
    rets = eq.pct_change().dropna()
    combined_sharpe = 0.0
    if len(rets) > 1 and rets.std() > 0:
        combined_sharpe = (rets.mean() / rets.std()) * math.sqrt(bars_per_year)

    tactical_metrics = compute_metrics(tac_pf, tactical_capital,
                                        bars_per_year=bars_per_year)

    combined_return = (final_combined - START_CAPITAL) / START_CAPITAL * 100
    core_return = (final_core - START_CAPITAL * HYBRID_CORE_PCT) / (START_CAPITAL * HYBRID_CORE_PCT) * 100

    result = {
        "mode": "hybrid",
        "start": start, "end": end,
        "core_names": core_names_available,
        "allocation": {
            "core_pct": HYBRID_CORE_PCT,
            "tactical_pct": HYBRID_TACTICAL_PCT,
            "cash_pct": HYBRID_CASH_FLOOR,
        },
        "combined_metrics": {
            "return_pct": round(combined_return, 2),
            "sharpe": round(combined_sharpe, 2),
            "max_drawdown_pct": round(combined_max_dd * 100, 2),
            "final_equity": round(final_combined, 2),
        },
        "sleeve_attribution": {
            "core": {
                "return_pct": round(core_return, 2),
                "final_value": round(final_core, 2),
                "allocated": round(START_CAPITAL * HYBRID_CORE_PCT, 2),
                "realized_pnl": round(core_realized_pnl, 2),
            },
            "tactical": tactical_metrics,
            "cash_reserve": {
                "final_value": round(final_cash, 2),
                "allocated": round(START_CAPITAL * HYBRID_CASH_FLOOR, 2),
            },
        },
        "tactical_by_strategy": per_strategy_metrics(tac_pf),
        "equity_curve": combined_curve,
        "n_bars": len(all_dates),
    }

    if benchmark_curve and len(benchmark_curve) == len(combined_curve):
        bench = pd.Series(benchmark_curve)
        bench_rets = bench.pct_change().dropna()
        min_len = min(len(rets), len(bench_rets))
        if min_len > 10:
            sr = rets.iloc[:min_len].values
            br = bench_rets.iloc[:min_len].values
            cov = np.cov(sr, br)
            beta = cov[0, 1] / cov[1, 1] if cov[1, 1] > 0 else 1.0
            result["combined_metrics"]["beta"] = round(beta, 3)
            total_bench = (bench.iloc[-1] / bench.iloc[0]) - 1 if bench.iloc[0] > 0 else 0
            result["combined_metrics"]["excess_return_pct"] = round(combined_return / 100 - total_bench, 4) * 100

        result["benchmark_curve"] = [
            (combined_curve[i][0], round(benchmark_curve[i], 2))
            for i in range(min(len(combined_curve), len(benchmark_curve)))
        ]

    return result


# ── Point-in-Time Hybrid Backtest ────────────────────────────
#
# Instead of a fixed 8-name core basket, this version scores the entire
# universe at each quarterly rebalance using only data available at that date.
# The top 8 are selected, with a turnover constraint of 3 changes per quarter.

def run_pit_hybrid_backtest(start: str, end: str, universe: List[str],
                             interval: str = "1d") -> dict:
    """Run a point-in-time hybrid backtest with scored quarterly rebalancing.

    Core basket is selected at each quarter using fundamental + momentum +
    valuation + stability scores computed from data available at that date.
    Tactical sleeve runs the same signal engine as run_backtest().

    Returns combined metrics + sleeve-level attribution + basket history.
    """
    from fundamental.pit_scoring import PITScorer, CORE_UNIVERSE

    bars_per_year = 252
    print(f"PIT HYBRID Backtest {start} → {end}")
    print(f"  Core universe: {len(CORE_UNIVERSE)} scorable names")
    print(f"  Core: {HYBRID_CORE_PCT:.0%}, Tactical: {HYBRID_TACTICAL_PCT:.0%}, "
          f"Cash: {HYBRID_CASH_FLOOR:.0%}")

    # Initialize PIT scorer and load all data
    scorer = PITScorer(universe=CORE_UNIVERSE, start=start, end=end)
    scorer.load_data()

    # Auto-detect earliest usable date (when enough tickers have financials)
    earliest = scorer.earliest_usable_date(min_scored=8)
    if earliest > start:
        print(f"  ⚠ yfinance financials only go back to ~{earliest}")
        print(f"    PIT scoring will use price-only factors until fundamental data is available")
        print(f"    Full 5-factor scoring available from {earliest}")

    # Fetch price history for tactical universe + core universe
    all_tickers = list(set(universe + CORE_UNIVERSE))
    history: Dict[str, pd.DataFrame] = {}
    for tk in all_tickers:
        df = fetch_history(tk, start, end, interval=interval)
        if df is not None and len(df) >= 60:
            history[tk] = enrich(df)

    if not history:
        return {"error": "no data"}

    # Prefetch for regime
    prefetch_start = (pd.Timestamp(start) - pd.Timedelta(days=370)).strftime("%Y-%m-%d")
    spy_full = vix_full = None
    vix_raw = fetch_history("^VIX", prefetch_start, end, interval=interval)
    if vix_raw is not None and len(vix_raw) > 0:
        vix_full = vix_raw
    if "SPY" not in history:
        spy_df = fetch_history("SPY", start, end, interval=interval)
        if spy_df is not None and len(spy_df) >= 60:
            history["SPY"] = enrich(spy_df)
    spy_pre = fetch_history("SPY", prefetch_start, end, interval=interval)
    if spy_pre is not None and len(spy_pre) >= 200:
        spy_full = enrich(spy_pre)

    tradable_tickers = [t for t in history if t != "^VIX"]
    all_dates = sorted(set().union(*[history[t].index for t in tradable_tickers]))

    regime_series = build_regime_series(history, all_dates,
                                         spy_full=spy_full, vix_full=vix_full)

    # Get rebalance dates
    rebal_dates = scorer.get_quarterly_rebalance_dates()
    rebal_set = set(rebal_dates)

    # Initialize capital split
    total_capital = START_CAPITAL
    core_capital = total_capital * HYBRID_CORE_PCT
    tactical_capital = total_capital * HYBRID_TACTICAL_PCT
    cash_reserve = total_capital * HYBRID_CASH_FLOOR

    # Core basket tracking
    core_holdings: Dict[str, dict] = {}  # {ticker: {qty, entry_price, entry_date}}
    core_realized_pnl = 0.0
    current_basket: List[str] = []
    basket_history = []  # [{date, basket, scores, regime, added, removed}]
    core_trades = []  # [{ticker, entry_date, entry_price, exit_date, exit_price, pnl, pnl_pct, reason}]
    next_rebal_idx = 0

    # Tactical portfolio
    tac_pf = Portfolio(cash=tactical_capital, equity=tactical_capital,
                       peak_equity=tactical_capital)
    pending_signals: List[dict] = []

    # Portfolio snapshots (monthly)
    portfolio_snapshots = []
    last_snapshot_month = ""

    # Combined tracking
    combined_curve = []
    combined_peak = total_capital
    combined_max_dd = 0.0

    spy_df = history.get("SPY")
    benchmark_curve = []
    _spy_start = None

    close_matrix = pd.DataFrame({
        t: history[t]["close"] for t in tradable_tickers if t in history
    })

    for i, dt in enumerate(all_dates):
        dt_str = str(pd.Timestamp(dt).date())
        regime_info = regime_series.get(dt, {"regime": "bull", "vix": 20.0})
        regime = regime_info["regime"]
        regime_cfg = REGIME_CONFIG.get(regime, REGIME_CONFIG["bull"])
        vix_level = regime_info.get("vix", 20.0)

        # ── Core sleeve: scored quarterly rebalance ────────────────
        should_rebal = False
        if next_rebal_idx < len(rebal_dates):
            if dt_str >= rebal_dates[next_rebal_idx]:
                should_rebal = True
                next_rebal_idx += 1

        if should_rebal:
            # Score universe and select new basket
            new_basket = scorer.select_core_basket(dt_str, n=8,
                                                    prev_basket=current_basket if current_basket else None)
            scores = scorer.score_universe(dt_str)

            prev_set = set(current_basket)
            new_set = set(new_basket)
            removed = prev_set - new_set
            added = new_set - prev_set

            if new_basket:
                basket_history.append({
                    "date": dt_str,
                    "basket": new_basket,
                    "scores": {tk: {
                        "composite": round(scores[tk].composite, 3),
                        "valuation": round(scores[tk].valuation, 3),
                        "quality": round(scores[tk].quality, 3),
                        "revision": round(scores[tk].revision, 3),
                        "trend": round(scores[tk].trend, 3),
                        "stability": round(scores[tk].stability, 3),
                    } for tk in new_basket if tk in scores},
                    "regime": regime,
                    "added": list(added),
                    "removed": list(removed),
                })
            elif not current_basket:
                # No scoreable tickers yet — skip silently until next rebalance
                pass

            # Sell removed names
            for name in removed:
                if name in core_holdings and name in history and dt in history[name].index:
                    px = float(history[name].loc[dt, "close"])
                    atr = float(history[name].loc[dt, "atr_14"]) if "atr_14" in history[name].columns else 0
                    sell_fill = dynamic_slip(px, "SELL", atr, ticker=name)
                    h = core_holdings.pop(name)
                    pnl = (sell_fill - h["entry_price"]) * h["qty"]
                    core_realized_pnl += pnl
                    core_capital += h["qty"] * sell_fill
                    core_trades.append({
                        "ticker": name,
                        "entry_date": h.get("entry_date", "?"),
                        "entry_price": round(h["entry_price"], 2),
                        "exit_date": dt_str,
                        "exit_price": round(sell_fill, 2),
                        "qty": h["qty"],
                        "pnl": round(pnl, 2),
                        "pnl_pct": round((sell_fill / h["entry_price"] - 1) * 100, 2),
                        "reason": "rebalance_out",
                    })

            # Buy new names (equal-weight)
            names_to_buy = [n for n in new_basket if n not in core_holdings]
            if names_to_buy:
                # Total core value = cash + existing holdings MTM
                core_mtm_for_sizing = core_capital
                for name, h in core_holdings.items():
                    if name in history and dt in history[name].index:
                        core_mtm_for_sizing += h["qty"] * float(history[name].loc[dt, "close"])
                    else:
                        core_mtm_for_sizing += h["qty"] * h["entry_price"]

                per_name = core_mtm_for_sizing / max(len(new_basket), 1)
                for name in names_to_buy:
                    if name in history and dt in history[name].index:
                        px = float(history[name].loc[dt, "open"])
                        if px > 0:
                            atr = float(history[name].loc[dt, "atr_14"]) if "atr_14" in history[name].columns else 0
                            fill_px = dynamic_slip(px, "BUY", atr, ticker=name)
                            qty = int(per_name / fill_px)
                            if qty > 0:
                                core_holdings[name] = {"qty": qty, "entry_price": fill_px, "entry_date": dt_str}
                                core_capital -= qty * fill_px

            if new_basket:
                current_basket = new_basket
                print(f"  {dt_str} rebal: {new_basket} "
                      f"(+{len(added)} -{len(removed)})")

        # ── Tactical sleeve: same logic as run_backtest ─────────
        core_tickers_set = set(current_basket)

        # Process exits
        for code in list(tac_pf.positions.keys()):
            if code not in history or dt not in history[code].index:
                continue
            bar = history[code].loc[dt]
            reason = check_exit(tac_pf.positions[code], bar)
            if reason:
                close_position(tac_pf, code, dt, bar, reason, bars_per_year)

        # Execute pending signals
        if pending_signals and len(tac_pf.positions) < MAX_OPEN_POSITIONS:
            allowed_strats = regime_cfg.get("allowed_strategies")
            for sig in pending_signals:
                if len(tac_pf.positions) >= MAX_OPEN_POSITIONS:
                    break
                code = sig["code"]
                if code in tac_pf.positions or code not in history or dt not in history[code].index:
                    continue
                if code in core_tickers_set:
                    continue  # R1: no overlap with core
                if allowed_strats and sig["strategy"] not in allowed_strats:
                    continue
                next_open = float(history[code].loc[dt, "open"])
                open_position(tac_pf, code, sig, dt, next_open, regime_cfg, vix_level)

        # Generate new signals
        all_signals: List[dict] = []
        if len(tac_pf.positions) < MAX_OPEN_POSITIONS:
            for code in tradable_tickers:
                if code == "SPY" or code in tac_pf.positions or code in core_tickers_set:
                    continue
                df = history[code]
                if dt not in df.index:
                    continue
                idx = df.index.get_loc(dt)
                if idx < 55:
                    continue
                window = df.iloc[:idx + 1]
                sigs = evaluate_signals(code, window)
                if sigs:
                    best = max(sigs, key=lambda s: s["score"])
                    all_signals.append(best)
        pending_signals = sorted(all_signals, key=lambda s: s["score"], reverse=True)

        # ── Mark to market (both sleeves) ───────────────────────
        # Core MTM
        core_mtm = core_capital
        for name, h in core_holdings.items():
            if name in history and dt in history[name].index:
                px = float(history[name].loc[dt, "close"])
                core_mtm += h["qty"] * px
            else:
                core_mtm += h["qty"] * h["entry_price"]

        # Tactical MTM
        tac_latest = {}
        for code in tradable_tickers:
            if dt in history[code].index:
                tac_latest[code] = float(history[code].loc[dt, "close"])
        mark_to_market(tac_pf, tac_latest)

        # T-bill on idle cash
        try:
            yr = pd.Timestamp(dt).year
            rf_daily = RISK_FREE_RATES.get(yr, 0.03) / 252
            idle = max(core_capital, 0) + max(tac_pf.cash, 0) + cash_reserve
            rf_income = idle * rf_daily
            cash_reserve += rf_income
        except Exception:
            pass

        combined_equity = core_mtm + tac_pf.equity + cash_reserve
        combined_curve.append((dt_str, round(combined_equity, 2)))
        if combined_equity > combined_peak:
            combined_peak = combined_equity
        dd = (combined_peak - combined_equity) / combined_peak if combined_peak else 0
        if dd > combined_max_dd:
            combined_max_dd = dd

        # Monthly portfolio snapshot
        current_month = dt_str[:7]  # "YYYY-MM"
        if current_month != last_snapshot_month and current_basket:
            last_snapshot_month = current_month
            snap = {
                "date": dt_str,
                "core_holdings": {name: {
                    "qty": h["qty"],
                    "entry_price": round(h["entry_price"], 2),
                    "current_price": round(float(history[name].loc[dt, "close"]), 2) if name in history and dt in history[name].index else round(h["entry_price"], 2),
                    "unrealized_pnl": round((float(history[name].loc[dt, "close"]) - h["entry_price"]) * h["qty"], 2) if name in history and dt in history[name].index else 0,
                } for name, h in core_holdings.items()},
                "core_cash": round(core_capital, 2),
                "core_total": round(core_mtm, 2),
                "tactical_equity": round(tac_pf.equity, 2),
                "tactical_open_positions": len(tac_pf.positions),
                "cash_reserve": round(cash_reserve, 2),
                "combined_equity": round(combined_equity, 2),
                "regime": regime,
            }
            portfolio_snapshots.append(snap)

        # Benchmark
        if spy_df is not None and dt in spy_df.index:
            if _spy_start is None:
                _spy_start = float(spy_df.loc[dt, "close"])
                benchmark_curve.append(START_CAPITAL)
            else:
                benchmark_curve.append(
                    START_CAPITAL * float(spy_df.loc[dt, "close"]) / _spy_start
                )
        elif benchmark_curve:
            benchmark_curve.append(benchmark_curve[-1])

    # Final close-out
    last_dt = all_dates[-1]
    last_dt_str = str(pd.Timestamp(last_dt).date())
    for code in list(tac_pf.positions.keys()):
        if code in history and last_dt in history[code].index:
            close_position(tac_pf, code, last_dt, history[code].loc[last_dt],
                          "end_of_backtest", bars_per_year)
    for name, h in list(core_holdings.items()):
        if name in history and last_dt in history[name].index:
            px = float(history[name].loc[last_dt, "close"])
            pnl = (px - h["entry_price"]) * h["qty"]
            core_realized_pnl += pnl
            core_capital += h["qty"] * px
            core_trades.append({
                "ticker": name,
                "entry_date": h.get("entry_date", "?"),
                "entry_price": round(h["entry_price"], 2),
                "exit_date": last_dt_str,
                "exit_price": round(px, 2),
                "qty": h["qty"],
                "pnl": round(pnl, 2),
                "pnl_pct": round((px / h["entry_price"] - 1) * 100, 2),
                "reason": "end_of_backtest",
            })

    final_core = core_capital
    final_tactical = tac_pf.equity
    final_cash = cash_reserve
    final_combined = final_core + final_tactical + final_cash

    # Combined Sharpe
    eq = pd.Series([e for _, e in combined_curve])
    rets = eq.pct_change().dropna()
    combined_sharpe = 0.0
    if len(rets) > 1 and rets.std() > 0:
        combined_sharpe = (rets.mean() / rets.std()) * math.sqrt(bars_per_year)

    tactical_metrics = compute_metrics(tac_pf, tactical_capital,
                                        bars_per_year=bars_per_year)

    combined_return = (final_combined - START_CAPITAL) / START_CAPITAL * 100
    core_return = (final_core - START_CAPITAL * HYBRID_CORE_PCT) / (START_CAPITAL * HYBRID_CORE_PCT) * 100

    # Serialize tactical trades
    tac_trades_out = []
    for t in tac_pf.trades:
        tac_trades_out.append({
            "ticker": t.code,
            "direction": t.direction,
            "strategy": t.strategy,
            "entry_date": str(t.entry_date)[:10] if t.entry_date else "?",
            "exit_date": str(t.exit_date)[:10] if t.exit_date else "?",
            "entry_price": round(t.entry_price, 2),
            "exit_price": round(t.exit_price, 2),
            "pnl": round(t.pnl, 2),
            "pnl_pct": round(t.pnl_pct, 2),
            "bars_held": t.bars_held,
            "exit_reason": t.exit_reason,
        })

    result = {
        "mode": "pit_hybrid",
        "start": start, "end": end,
        "core_universe_size": len(CORE_UNIVERSE),
        "allocation": {
            "core_pct": HYBRID_CORE_PCT,
            "tactical_pct": HYBRID_TACTICAL_PCT,
            "cash_pct": HYBRID_CASH_FLOOR,
        },
        "combined_metrics": {
            "return_pct": round(combined_return, 2),
            "sharpe": round(combined_sharpe, 2),
            "max_drawdown_pct": round(combined_max_dd * 100, 2),
            "final_equity": round(final_combined, 2),
        },
        "sleeve_attribution": {
            "core": {
                "return_pct": round(core_return, 2),
                "final_value": round(final_core, 2),
                "allocated": round(START_CAPITAL * HYBRID_CORE_PCT, 2),
                "realized_pnl": round(core_realized_pnl, 2),
            },
            "tactical": tactical_metrics,
            "cash_reserve": {
                "final_value": round(final_cash, 2),
                "allocated": round(START_CAPITAL * HYBRID_CASH_FLOOR, 2),
            },
        },
        "tactical_by_strategy": per_strategy_metrics(tac_pf),
        "basket_history": basket_history,
        "equity_curve": combined_curve,
        "n_bars": len(all_dates),
        "core_trades": core_trades,
        "portfolio_snapshots": portfolio_snapshots,
        "tactical_trades": tac_trades_out[-200:],
        "tactical_trades_total": len(tac_trades_out),
    }

    if benchmark_curve and len(benchmark_curve) == len(combined_curve):
        bench = pd.Series(benchmark_curve)
        bench_rets = bench.pct_change().dropna()
        min_len = min(len(rets), len(bench_rets))
        if min_len > 10:
            sr = rets.iloc[:min_len].values
            br = bench_rets.iloc[:min_len].values
            cov = np.cov(sr, br)
            beta = cov[0, 1] / cov[1, 1] if cov[1, 1] > 0 else 1.0
            result["combined_metrics"]["beta"] = round(beta, 3)
            total_bench = (bench.iloc[-1] / bench.iloc[0]) - 1 if bench.iloc[0] > 0 else 0
            result["combined_metrics"]["excess_return_pct"] = round(combined_return / 100 - total_bench, 4) * 100

        result["benchmark_curve"] = [
            (combined_curve[i][0], round(benchmark_curve[i], 2))
            for i in range(min(len(combined_curve), len(benchmark_curve)))
        ]

    return result


# ── CLI ───────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--universe", choices=["us", "ext"], default="us")
    ap.add_argument("--interval", choices=["1d", "1h", "60m"], default="1d")
    ap.add_argument("--scaled", action="store_true", help="Use tier sizing")
    ap.add_argument("--regime", action="store_true", help="Enable regime-aware rules")
    ap.add_argument("--hybrid", action="store_true",
                    help="Run hybrid backtest (core + tactical sleeves)")
    ap.add_argument("--pit-hybrid", action="store_true",
                    help="Run point-in-time hybrid (scored quarterly rebalance)")
    ap.add_argument("--walk-forward", action="store_true",
                    help="Run walk-forward (train 2020-2023, test 2024-now)")
    ap.add_argument("--rolling", action="store_true",
                    help="Use rolling walk-forward windows (requires --walk-forward)")
    args = ap.parse_args()

    global SCALED_MODE, REGIME_MODE
    SCALED_MODE = args.scaled
    REGIME_MODE = args.regime
    universe = get_universe(args.universe)

    if args.pit_hybrid:
        REGIME_MODE = True
        result = run_pit_hybrid_backtest(args.start, args.end, universe,
                                          interval=args.interval)
        suffix = f"_{args.universe}_{args.interval}_pit_hybrid"
        out_path = LOG_DIR / f"backtest_{args.start}_{args.end}{suffix}.json"
        out_path.write_text(json.dumps(result, indent=2, default=str))
        print(f"\nWrote {out_path}")
        print("\n=== COMBINED METRICS ===")
        print(json.dumps(result.get("combined_metrics", {}), indent=2))
        print("\n=== SLEEVE ATTRIBUTION ===")
        print(json.dumps(result.get("sleeve_attribution", {}), indent=2))
        print("\n=== BASKET HISTORY ===")
        for bh in result.get("basket_history", []):
            print(f"  {bh['date']}: {bh['basket']} (regime={bh.get('regime','?')})")
        return

    if args.hybrid:
        REGIME_MODE = True  # hybrid always uses regime
        result = run_hybrid_backtest(args.start, args.end, universe,
                                     interval=args.interval)
        suffix = f"_{args.universe}_{args.interval}_hybrid"
        out_path = LOG_DIR / f"backtest_{args.start}_{args.end}{suffix}.json"
        out_path.write_text(json.dumps(result, indent=2, default=str))
        print(f"\nWrote {out_path}")
        print("\n=== COMBINED METRICS ===")
        print(json.dumps(result.get("combined_metrics", {}), indent=2))
        print("\n=== SLEEVE ATTRIBUTION ===")
        print(json.dumps(result.get("sleeve_attribution", {}), indent=2))
        print("\n=== TACTICAL BY STRATEGY ===")
        print(json.dumps(result.get("tactical_by_strategy", {}), indent=2))
        return

    if args.walk_forward:
        if args.rolling:
            result = run_rolling_walk_forward(
                universe=universe, interval=args.interval,
                train_years=3, test_years=1, start_year=2018,
            )
            suffix = f"_{args.universe}_{args.interval}"
            if args.regime:
                suffix += "_regime"
            out_path = LOG_DIR / f"rolling_wf{suffix}.json"
        else:
            result = run_walk_forward(
                train=("2020-01-01", "2023-12-31"),
                test=("2024-01-01", args.end),
                universe=universe,
                interval=args.interval,
            )
            suffix = f"_{args.universe}_{args.interval}"
            if args.regime:
                suffix += "_regime"
            out_path = LOG_DIR / f"walkforward{suffix}.json"
        out_path.write_text(json.dumps(result, indent=2, default=str))
        print(f"\nWrote {out_path}")
        return

    result = run_backtest(args.start, args.end, universe, interval=args.interval)
    suffix = f"_{args.universe}_{args.interval}"
    if args.scaled:
        suffix += "_scaled"
    if args.regime:
        suffix += "_regime"
    out_path = LOG_DIR / f"backtest_{args.start}_{args.end}{suffix}.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"\nWrote {out_path}")
    print("\n=== METRICS ===")
    print(json.dumps(result.get("metrics", {}), indent=2))
    print("\n=== BY STRATEGY ===")
    print(json.dumps(result.get("by_strategy", {}), indent=2))


if __name__ == "__main__":
    main()
