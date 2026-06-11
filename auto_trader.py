"""
Fully Automated Trading Loop — Multi-Strategy Fund.

Scans US + HK markets, evaluates strategies, executes paper trades,
tracks P&L, and learns from trade outcomes to improve over time.

Usage: ./venv/bin/python auto_trader.py
"""

import csv
import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from moomoo import (
    RET_OK, TrdSide, OrderType, TrdEnv, TrdMarket,
    SubType, KLType, AuType, SysConfig,
    OpenQuoteContext, OpenSecTradeContext,
)
# SDK 10.7+ removed OpenUSTradeContext; use OpenSecTradeContext with
# filter_trdmarket=TrdMarket.US instead. Provide a compatibility alias
# so legacy call sites keep working without a mass refactor.
class OpenUSTradeContext(OpenSecTradeContext):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("filter_trdmarket", TrdMarket.US)
        super().__init__(*args, **kwargs)

from logging_config.logger import setup_logging
from dashboard.web_server import start_dashboard, write_system_state
from options_strategies import scan_option_ideas, get_option_quotes
from fundamental.rebalancer import (
    load_rebalance_state, should_rebalance, generate_rebalance_actions,
    execute_rebalance, get_core_tickers, log_rebalance_actions,
)

# Stage 1/2/3 live deployment framework (shadow-mode-safe — never places
# real orders unless live_mode is flipped via scripts/enable_live.py)
try:
    from risk.stage_controller import get_controller as _get_stage_controller
    from execution.live_gate import queue_order as _stage_queue_order
    _STAGE_ENABLED = True
except Exception as _stage_err:  # pragma: no cover
    _STAGE_ENABLED = False
    _stage_queue_order = None
    _get_stage_controller = None

# ── Configuration ───────────────────────────────────────────────

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

OPEND_HOST = "127.0.0.1"
OPEND_PORT = 11111

# Risk limits
MAX_POSITION_PCT = 0.08        # 8% of account per position
MAX_POSITION_USD = 80_000      # hard dollar cap
MAX_OPEN_TRADES = 20           # across all pods
MAX_DAILY_LOSS_PCT = 0.025     # 2.5% of account — halt
MAX_GROSS_EXPOSURE = 1.50      # 150% gross (allows long+short book); net exposure capped by position limits
COOLDOWN_AFTER_LOSSES = 3      # consecutive
COOLDOWN_MINUTES = 30
MIN_SIGNAL_SCORE = 0.50        # raised from 0.40 — fewer but better trades

# 台阶战术 — Capital scaling tiers. As the account grows, lock in a portion of
# gains as "principal reserve" and only risk the working capital above the
# locked floor. Sizing % of working capital DECREASES as the account grows so
# absolute risk plateaus rather than compounds aggressively. Tiers are sticky
# upward (a new high promotes you) and ratcheted downward (drawdown below the
# previous trigger reverts to that lower tier).
#
# Each tier: (balance_trigger_usd, keep_ratio_of_gains, max_position_pct,
#            max_position_usd, max_risk_pct_per_trade)
#   keep_ratio_of_gains = fraction of gains above trigger that stays "working".
#       The remainder is locked into the principal reserve.
SCALING_TIERS = [
    (1_000_000, 1.00, 0.08, 80_000,  0.010),   # T0 baseline
    (1_100_000, 0.50, 0.08, 84_000,  0.010),   # T1: lock half of gains
    (1_250_000, 0.50, 0.07, 79_000,  0.009),   # T2: tighter sizing
    (1_500_000, 0.40, 0.06, 72_000,  0.008),   # T3: defensive
    (2_000_000, 0.40, 0.05, 70_000,  0.007),   # T4: compound conservatively
]


def get_scaling_tier(balance: float) -> tuple:
    """Return the active tier (trigger, keep_ratio, pos_pct, pos_usd, risk_pct)."""
    active = SCALING_TIERS[0]
    for t in SCALING_TIERS:
        if balance >= t[0]:
            active = t
    return active


_DAY_START_PATH = LOG_DIR / "day_start_balance.json"


def _load_day_start_balance(current_balance: float) -> float:
    """
    Persist start-of-day balance for 今日盈亏比例 calculation.
    Resets at the first cycle of a new calendar day.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        if _DAY_START_PATH.exists():
            data = json.loads(_DAY_START_PATH.read_text())
            if data.get("date") == today:
                return float(data.get("balance", current_balance))
    except Exception:
        pass
    # New day — snapshot current balance
    try:
        _DAY_START_PATH.write_text(json.dumps({"date": today, "balance": current_balance}))
    except Exception:
        pass
    return current_balance


def working_capital(balance: float) -> tuple:
    """
    Compute working capital and locked reserve for the active tier.
    Returns (working_cap, locked_reserve, tier).
    """
    tier = get_scaling_tier(balance)
    trigger, keep_ratio, _, _, _ = tier
    if balance <= trigger:
        return balance, 0.0, tier
    gains = balance - trigger
    working = trigger + gains * keep_ratio
    locked = balance - working
    return working, locked, tier

# FX normalization — PnL is computed in local currency (HKD for HK.* tickers,
# USD for US.*). Everything stored and displayed to the investor is USD, so we
# convert HK trade PnL using a fixed rate. HKD/USD ≈ 7.78.
HKD_PER_USD = 7.78

def to_usd(code: str, amount: float) -> float:
    """Convert a local-currency amount to USD based on the ticker's market."""
    if code.startswith("HK."):
        return amount / HKD_PER_USD
    return amount

# Universe
US_UNIVERSE = [
    # Mega-cap tech (Nasdaq 100 core)
    "US.NVDA", "US.AAPL", "US.MSFT", "US.GOOGL", "US.AMZN", "US.META", "US.TSLA",
    "US.NFLX", "US.ORCL", "US.ADBE", "US.CRM",
    # Semiconductors
    "US.AMD", "US.AVGO", "US.QCOM", "US.TSM", "US.MU", "US.TXN", "US.ARM", "US.SMCI",
    # Financials (S&P 500 large-caps)
    "US.JPM", "US.GS", "US.BAC", "US.WFC", "US.MS", "US.V", "US.MA",
    # Healthcare
    "US.UNH", "US.LLY", "US.JNJ", "US.ABBV",
    # Consumer / Retail
    "US.WMT", "US.COST", "US.HD", "US.MCD", "US.NKE",
    # Energy / Industrials / Materials
    "US.XOM", "US.CVX", "US.CAT", "US.BA", "US.LIN",
    # High-beta / thematic
    "US.PLTR", "US.COIN", "US.MSTR",
]
HK_UNIVERSE = [
    # Mega-cap tech (Hang Seng TECH)
    "HK.00700",  # Tencent
    "HK.09988",  # Alibaba
    "HK.03690",  # Meituan
    "HK.01810",  # Xiaomi
    "HK.09618",  # JD.com
    "HK.09999",  # NetEase
    "HK.09888",  # Baidu
    "HK.01024",  # Kuaishou
    # Financials (HSI heavyweights)
    "HK.00005",  # HSBC
    "HK.00388",  # HKEX
    "HK.02318",  # Ping An
    "HK.02388",  # BOC Hong Kong
    "HK.00939",  # CCB
    "HK.01398",  # ICBC
    "HK.03988",  # Bank of China
    "HK.01299",  # AIA
    "HK.02628",  # China Life
    # Energy
    "HK.00883",  # CNOOC
    "HK.00857",  # PetroChina
    "HK.00386",  # Sinopec
    # EV / autos
    "HK.01211",  # BYD
    "HK.09868",  # XPeng
    "HK.02015",  # Li Auto
    # Consumer / healthcare / conglomerate
    "HK.02020",  # Anta Sports
    "HK.02269",  # Wuxi Biologics
    "HK.00001",  # CK Hutchison
]
ETF_UNIVERSE = [
    "US.SPY", "US.QQQ", "US.IWM", "US.TLT", "US.GLD", "US.XLE", "US.XLF", "US.XLK",
]

# ── Top-100 US test universe ────────────────────────────────────
# Curated from S&P 100 + top Nasdaq 100 names by market cap / ADV.
# NOT used for live execution — exposed for scripts/eval_universe.py to
# evaluate whether our current whitelist is leaving alpha on the table.
# All names are large-cap, high ADV, and paper-tradable via moomoo.
US_TOP100_TEST = [
    # Mega-cap tech & internet
    "US.NVDA","US.AAPL","US.MSFT","US.GOOGL","US.GOOG","US.AMZN","US.META",
    "US.TSLA","US.NFLX","US.ORCL","US.ADBE","US.CRM","US.INTU","US.CSCO",
    "US.IBM","US.SAP","US.NOW","US.UBER","US.SHOP","US.ABNB",
    # Semiconductors
    "US.AMD","US.AVGO","US.QCOM","US.TSM","US.MU","US.TXN","US.ARM","US.SMCI",
    "US.AMAT","US.LRCX","US.KLAC","US.ASML","US.INTC","US.MRVL","US.ADI",
    # Financials
    "US.JPM","US.GS","US.BAC","US.WFC","US.MS","US.C","US.V","US.MA",
    "US.AXP","US.BLK","US.SCHW","US.PYPL","US.BRK.B","US.SPGI","US.CME",
    # Healthcare
    "US.UNH","US.LLY","US.JNJ","US.ABBV","US.MRK","US.PFE","US.TMO",
    "US.ABT","US.DHR","US.ISRG","US.BMY","US.AMGN","US.GILD","US.VRTX",
    # Consumer / Retail / Staples
    "US.WMT","US.COST","US.HD","US.MCD","US.NKE","US.SBUX","US.LOW",
    "US.TGT","US.PG","US.KO","US.PEP","US.PM","US.MDLZ","US.CL",
    # Energy / Industrials / Materials / Utilities
    "US.XOM","US.CVX","US.COP","US.SLB","US.OXY","US.CAT","US.BA",
    "US.GE","US.HON","US.LMT","US.RTX","US.DE","US.UPS","US.LIN",
    "US.FCX","US.NEE","US.DUK","US.SO",
    # Comm services / Media
    "US.DIS","US.T","US.VZ","US.CMCSA","US.TMUS",
    # High-beta / thematic
    "US.PLTR","US.COIN","US.MSTR","US.SNOW","US.DDOG","US.CRWD","US.NET",
]


# ── Logging ─────────────────────────────────────────────────────

setup_logging(LOG_DIR, "INFO")
logger = logging.getLogger("auto_trader")

# ── Shutdown ────────────────────────────────────────────────────

_shutdown = False

# ── SPY rally-extension cache (used by profit-protection rules) ─────────
_spy_ext_ts: float = 0.0
_spy_ext_val: str = "normal"
_spy_ext_returns: tuple = (0.0, 0.0, 0.0)   # (1d, 3d, 5d)

def _get_spy_extension(ctx) -> str:
    """Fetch SPY daily returns and return the rally-extension level.
    Results are cached for 10 minutes so we only hit the API once per cycle."""
    global _spy_ext_ts, _spy_ext_val, _spy_ext_returns
    import time as _time
    if _time.time() - _spy_ext_ts < 600:
        return _spy_ext_val
    try:
        from datetime import timedelta as _td
        from risk.profit_protection import rally_extension_level as _rl
        start_dt = (datetime.now() - _td(days=12)).strftime("%Y-%m-%d")
        end_dt   = datetime.now().strftime("%Y-%m-%d")
        from moomoo import KLType as _KLT
        result = ctx.request_history_kline(
            "US.SPY", start=start_dt, end=end_dt,
            ktype=_KLT.K_DAY, max_count=10,
        )
        df = result[1]
        closes = df["close"].tolist()
        r1 = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 else 0.0
        r3 = (closes[-1] - closes[-4]) / closes[-4] if len(closes) >= 4 else r1
        r5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else r1
        _spy_ext_returns = (r1, r3, r5)
        _spy_ext_val = _rl(r1, r3, r5)
        _spy_ext_ts = _time.time()
        logger.info(
            f"[RALLY-EXT] SPY 1d={r1*100:+.2f}% 3d={r3*100:+.2f}% 5d={r5*100:+.2f}% "
            f"→ {_spy_ext_val.upper()}"
        )
    except Exception as _e:
        logger.debug(f"SPY extension fetch failed: {_e}")
    return _spy_ext_val

def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Shutdown signal received")
    _shutdown = True

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ── Trade Journal ───────────────────────────────────────────────

JOURNAL_PATH = LOG_DIR / "auto_trades.csv"
LEARNING_PATH = LOG_DIR / "strategy_performance.json"

JOURNAL_FIELDS = [
    "timestamp", "cycle", "code", "name", "side", "qty", "entry_price",
    "stop_loss", "take_profit", "strategy", "signal_score", "order_id",
    "exit_price", "exit_time", "pnl", "pnl_pct", "hold_bars", "status",
    "pod", "thesis",
]


def init_journal():
    if not JOURNAL_PATH.exists():
        with open(JOURNAL_PATH, "w", newline="") as f:
            csv.DictWriter(f, JOURNAL_FIELDS).writeheader()


def append_trade(trade: Dict):
    with open(JOURNAL_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, JOURNAL_FIELDS)
        writer.writerow({k: trade.get(k, "") for k in JOURNAL_FIELDS})


def read_journal() -> List[Dict]:
    if not JOURNAL_PATH.exists():
        return []
    with open(JOURNAL_PATH) as f:
        return list(csv.DictReader(f))

# ── Learning System ─────────────────────────────────────────────

def load_strategy_stats() -> Dict:
    """Load accumulated strategy performance stats."""
    if LEARNING_PATH.exists():
        with open(LEARNING_PATH) as f:
            return json.load(f)
    return {}


def save_strategy_stats(stats: Dict):
    with open(LEARNING_PATH, "w") as f:
        json.dump(stats, f, indent=2)


def update_learning(trades: List[Dict]) -> Dict:
    """
    Analyze closed trades and update strategy performance stats.
    This is how the system 'learns' — it tracks win rate, avg P&L,
    and risk/reward realization per strategy, and adjusts future
    signal score thresholds and position sizing accordingly.
    """
    stats = load_strategy_stats()

    closed = [t for t in trades if t.get("status") == "CLOSED"]
    for t in closed:
        strat = t.get("strategy", "unknown")
        if strat not in stats:
            stats[strat] = {
                "trades": 0, "wins": 0, "losses": 0,
                "total_pnl": 0, "avg_pnl": 0, "win_rate": 0,
                "best": 0, "worst": 0,
                "avg_hold_bars": 0,
                "score_adjustment": 0,  # learned adjustment to signal score
                "size_multiplier": 1.0,  # learned position size adjustment
            }

        s = stats[strat]
        pnl = float(t.get("pnl", 0) or 0)
        hold = int(t.get("hold_bars", 0) or 0)

        s["trades"] += 1
        s["total_pnl"] += pnl
        if pnl > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1

        s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] > 0 else 0
        s["avg_pnl"] = round(s["total_pnl"] / s["trades"], 2)
        s["best"] = max(s["best"], pnl)
        s["worst"] = min(s["worst"], pnl)
        s["avg_hold_bars"] = round(
            (s["avg_hold_bars"] * (s["trades"] - 1) + hold) / s["trades"], 1
        )

        # Adaptive adjustments based on performance
        if s["trades"] >= 5:
            # If win rate > 60%, boost signals from this strategy
            if s["win_rate"] > 60:
                s["score_adjustment"] = 0.05
                s["size_multiplier"] = 1.2
            # If win rate < 35%, penalize
            elif s["win_rate"] < 35:
                s["score_adjustment"] = -0.1
                s["size_multiplier"] = 0.6
            # If avg P&L is negative, reduce size
            elif s["avg_pnl"] < 0:
                s["score_adjustment"] = -0.05
                s["size_multiplier"] = 0.8
            else:
                s["score_adjustment"] = 0
                s["size_multiplier"] = 1.0

    save_strategy_stats(stats)
    return stats

# ── Market Data ─────────────────────────────────────────────────

def get_enriched_data(ctx: OpenQuoteContext, code: str) -> Optional[pd.DataFrame]:
    """Fetch 90-day daily klines and compute indicators.

    IMPORTANT: Must pass explicit start/end dates — without them,
    moomoo API returns stale data from ~1 year ago instead of recent bars.
    """
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    ret, data, _ = ctx.request_history_kline(
        code, ktype=KLType.K_DAY, autype=AuType.QFQ,
        start=start_date, end=end_date, max_count=90
    )
    if ret != RET_OK or data is None or len(data) < 30:
        return None

    # Staleness guard: reject data if last bar is more than 3 days old
    last_date_str = str(data.iloc[-1]["time_key"])[:10]
    last_date = datetime.strptime(last_date_str, "%Y-%m-%d")
    if (datetime.now() - last_date).days > 5:
        logger.warning(f"Stale kline data for {code}: last bar is {last_date_str}")
        return None

    df = data.copy()
    close = df["close"]

    # Moving averages
    df["sma_10"] = close.rolling(10).mean()
    df["sma_20"] = close.rolling(20).mean()
    df["sma_50"] = close.rolling(50).mean()
    df["ema_12"] = close.ewm(span=12, adjust=False).mean()
    df["ema_26"] = close.ewm(span=26, adjust=False).mean()

    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi_14"] = 100 - (100 / (1 + rs))

    # MACD
    macd_line = df["ema_12"] - df["ema_26"]
    df["macd"] = macd_line
    df["macd_signal"] = macd_line.ewm(span=9, adjust=False).mean()
    df["macd_hist"] = macd_line - df["macd_signal"]

    # ATR
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - close.shift(1)).abs()
    low_close = (df["low"] - close.shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr_14"] = tr.ewm(alpha=1/14, min_periods=14).mean()

    # Bollinger Bands
    df["bb_mid"] = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2 * bb_std

    # Volume — use PREVIOUS completed bar for relative volume, because
    # today's (last) bar is incomplete and always shows low volume intraday.
    df["rvol_raw"] = df["volume"] / df["volume"].rolling(20).mean()
    df["rvol"] = df["rvol_raw"].shift(1)  # use yesterday's completed RVOL
    # Fill last row with raw if only 1 bar or for after-hours
    if pd.isna(df["rvol"].iloc[-1]):
        df.loc[df.index[-1], "rvol"] = df["rvol_raw"].iloc[-1]

    # ROC
    df["roc_10"] = close.pct_change(10) * 100

    # Support / Resistance — exclude today's incomplete bar for clean levels
    df["support_20"] = df["low"].shift(1).rolling(20).min()
    df["resistance_20"] = df["high"].shift(1).rolling(20).max()

    # Momentum
    df["mom_5d"] = close.pct_change(5) * 100
    df["mom_20d"] = close.pct_change(20) * 100

    return df


def get_snapshot_data(ctx: OpenQuoteContext, codes: List[str]) -> Dict:
    """Get latest snapshot for multiple tickers."""
    results = {}
    for i in range(0, len(codes), 20):
        batch = codes[i:i+20]
        time.sleep(0.5)
        ret, snap = ctx.get_market_snapshot(batch)
        if ret == RET_OK:
            for _, r in snap.iterrows():
                code = str(r["code"])
                lp = float(r.get("last_price", 0))
                pc = float(r.get("prev_close_price", 0))
                results[code] = {
                    "price": lp,
                    "change_pct": round((lp - pc) / pc * 100, 2) if pc > 0 else 0,
                    "volume": int(r.get("volume", 0)),
                    "name": str(r.get("name", "")),
                }
    return results

# ── Regime Detection ────────────────────────────────────────────

def fetch_regime_state(ctx: OpenQuoteContext) -> Dict:
    """Classify market regime using SPY vs 200 SMA and VIX level.

    Called once per day. Returns {"regime": "bull"|"flat"|"bear", ...}.
    Uses extended lookback (250 bars / 400 days) for SPY to compute 200 SMA.
    """
    # Check if already fetched today
    if REGIME_STATE_PATH.exists():
        try:
            cached = json.loads(REGIME_STATE_PATH.read_text())
            if cached.get("date") == datetime.now().strftime("%Y-%m-%d"):
                return cached
        except Exception:
            pass

    regime_data = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "regime": "bull",  # default
        "spy_price": 0.0,
        "spy_sma200": 0.0,
        "vix_level": 0.0,
        "spy_above_200": True,
        "signals": [],
    }

    try:
        # Fetch SPY with extended lookback for 200 SMA
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
        ret, data, _ = ctx.request_history_kline(
            "US.SPY", ktype=KLType.K_DAY, autype=AuType.QFQ,
            start=start_date, end=end_date, max_count=250,
        )
        if ret == RET_OK and data is not None and len(data) >= 200:
            close = data["close"]
            sma_200 = close.rolling(200).mean()
            spy_price = float(close.iloc[-1])
            spy_sma200 = float(sma_200.iloc[-1])
            spy_above_200 = spy_price > spy_sma200
            spy_pct_from_200 = (spy_price - spy_sma200) / spy_sma200 * 100

            regime_data["spy_price"] = round(spy_price, 2)
            regime_data["spy_sma200"] = round(spy_sma200, 2)
            regime_data["spy_above_200"] = spy_above_200
            regime_data["spy_pct_from_200"] = round(spy_pct_from_200, 2)

            regime_data["signals"].append(
                f"SPY ${spy_price:.0f} vs 200SMA ${spy_sma200:.0f} "
                f"({'above' if spy_above_200 else 'below'} by {abs(spy_pct_from_200):.1f}%)"
            )
        else:
            logger.warning(f"Regime: SPY data insufficient ({len(data) if data is not None else 0} bars)")
            regime_data["signals"].append("SPY data unavailable — defaulting to bull")

        # Fetch VIX — try snapshot for VIXY as proxy (moomoo may not support VIX index directly)
        time.sleep(0.5)
        vix_level = 20.0  # default mid-level
        # Try VIX directly first
        for vix_code in ["US.VIX", "US.VIXY", "US.UVXY"]:
            try:
                ret_v, snap_v = ctx.get_market_snapshot([vix_code])
                if ret_v == RET_OK and snap_v is not None and len(snap_v) > 0:
                    raw_val = float(snap_v.iloc[0].get("last_price", 0))
                    if raw_val > 0:
                        if vix_code == "US.VIX":
                            vix_level = raw_val
                        elif vix_code == "US.VIXY":
                            # VIXY is a VIX short-term futures ETF; its price doesn't
                            # scale linearly to VIX due to contango decay.
                            # Rough mapping: VIXY ~$15→VIX~15, ~$25→VIX~25, ~$50→VIX~40
                            # Use a sqrt-based approximation for elevated levels.
                            if raw_val <= 20:
                                vix_level = raw_val  # roughly 1:1 at low levels
                            else:
                                vix_level = 20 + (raw_val - 20) ** 0.7  # compress high values
                        elif vix_code == "US.UVXY":
                            # UVXY is 1.5x leveraged VIX futures
                            vix_level = raw_val / 1.5
                        regime_data["signals"].append(f"VIX proxy ({vix_code}): {raw_val:.1f} → est. VIX={vix_level:.1f}")
                        break
            except Exception:
                continue
        regime_data["vix_level"] = round(vix_level, 1)

        # Classify regime
        spy_above = regime_data.get("spy_above_200", True)
        if spy_above and vix_level < 25:
            regime = "bull"
        elif not spy_above and vix_level > 25:
            regime = "bear"
        else:
            regime = "flat"

        regime_data["regime"] = regime
        regime_data["signals"].append(f"REGIME: {regime.upper()}")

    except Exception as e:
        logger.warning(f"Regime detection failed: {e} — defaulting to bull")
        regime_data["signals"].append(f"Error: {e}")

    # Persist
    try:
        REGIME_STATE_PATH.write_text(json.dumps(regime_data, indent=2))
    except Exception:
        pass

    logger.info(
        f"Regime: {regime_data['regime'].upper()} | "
        f"SPY=${regime_data['spy_price']:.0f} vs 200SMA=${regime_data['spy_sma200']:.0f} | "
        f"VIX={regime_data['vix_level']:.0f}"
    )
    return regime_data


def get_regime_config(regime: str) -> Dict:
    """Return the allocation config for the current regime."""
    return REGIME_CONFIG.get(regime, REGIME_CONFIG["bull"])


# ── Post-Trade Feedback Loop ──────────────────────────────────────

def analyze_closed_trade(trade: Dict, regime: str) -> Dict:
    """Analyze a closed trade for entry quality, timing, and strategy fit.

    Returns a feedback dict logged to trade_feedback.json.
    """
    entry = float(trade.get("entry_price", 0))
    exit_price = float(trade.get("exit_price", 0))
    stop = float(trade.get("stop_loss", 0))
    target = float(trade.get("take_profit", 0))
    pnl = float(trade.get("pnl", 0))
    pnl_pct = float(trade.get("pnl_pct", 0))
    direction = trade.get("side", "LONG")
    strategy = trade.get("strategy", "unknown")

    # Determine exit type
    exit_type = "unknown"
    if direction == "LONG":
        if exit_price <= stop and stop > 0:
            exit_type = "stop_loss"
        elif exit_price >= target and target > 0:
            exit_type = "take_profit"
        else:
            exit_type = "other"
    else:
        if exit_price >= stop and stop > 0:
            exit_type = "stop_loss"
        elif exit_price <= target and target > 0:
            exit_type = "take_profit"
        else:
            exit_type = "other"

    # Calculate risk/reward metrics
    risk_per_share = abs(entry - stop) if stop > 0 else entry * 0.02
    reward_per_share = abs(target - entry) if target > 0 else entry * 0.03
    planned_rr = reward_per_share / risk_per_share if risk_per_share > 0 else 0
    actual_return_vs_risk = abs(pnl_pct) / (risk_per_share / entry * 100) if risk_per_share > 0 and entry > 0 else 0

    # Hold time analysis
    try:
        open_time = datetime.fromisoformat(trade.get("timestamp", ""))
        close_time = datetime.fromisoformat(trade.get("exit_time", ""))
        hold_hours = (close_time - open_time).total_seconds() / 3600
    except Exception:
        hold_hours = 0

    # Strategy-regime fit
    regime_cfg = get_regime_config(regime)
    allowed = regime_cfg.get("allowed_strategies")
    regime_fit = "good" if allowed is None or strategy in allowed else "poor"

    # Mistake detection
    mistakes = []
    if pnl < 0:
        if exit_type == "stop_loss" and hold_hours < 1:
            mistakes.append("quick_stop: stopped out within 1 hour — possible bad entry timing")
        if exit_type == "other" and abs(pnl_pct) > 2:
            mistakes.append("session_close_loss: closed at session end with >2% loss — should have stopped earlier")
    if regime_fit == "poor":
        mistakes.append(f"regime_mismatch: {strategy} in {regime} regime — strategy not suited for conditions")

    # Improvement suggestions
    improvements = []
    if exit_type == "stop_loss" and pnl < 0 and risk_per_share > 0:
        actual_risk_pct = risk_per_share / entry * 100
        if actual_risk_pct > 5:
            improvements.append("tighten_stops: stop was >5% from entry — consider tighter risk")
    if hold_hours < 0.5 and pnl < 0:
        improvements.append("entry_timing: position lost money in <30min — consider waiting for confirmation")
    if pnl > 0 and exit_type != "take_profit" and target > 0:
        capture_pct = pnl_pct / (reward_per_share / entry * 100) * 100 if entry > 0 else 0
        if capture_pct < 50:
            improvements.append(f"premature_exit: captured only {capture_pct:.0f}% of target — let winners run")

    feedback = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "code": trade.get("code", ""),
        "strategy": strategy,
        "direction": direction,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "exit_type": exit_type,
        "hold_hours": round(hold_hours, 1),
        "planned_rr": round(planned_rr, 2),
        "actual_return_vs_risk": round(actual_return_vs_risk, 2),
        "regime": regime,
        "regime_fit": regime_fit,
        "mistakes": mistakes,
        "improvements": improvements,
    }
    return feedback


def log_trade_feedback(feedback: Dict):
    """Append trade feedback to the feedback log."""
    try:
        existing = []
        if FEEDBACK_PATH.exists():
            try:
                existing = json.loads(FEEDBACK_PATH.read_text())
            except Exception:
                existing = []
        existing.append(feedback)
        # Keep last 500
        FEEDBACK_PATH.write_text(json.dumps(existing[-500:], indent=2, default=str))
    except Exception as e:
        logger.debug(f"Feedback log write failed: {e}")


# ── Strategy Signals ────────────────────────────────────────────

def evaluate_ma_crossover(code: str, df: pd.DataFrame) -> Optional[Dict]:
    """SMA 20/50 crossover with RSI and volume confirmation.

    Three modes:
    1. Fresh crossover (highest conviction)
    2. Already trending above both MAs
    3. Approaching crossover — SMA20 converging toward SMA50 with price strength
    """
    if len(df) < 52:
        return None
    curr = df.iloc[-1]
    prev = df.iloc[-2]

    if pd.isna(curr.get("sma_20")) or pd.isna(curr.get("sma_50")):
        return None

    # Mode 1: Bullish crossover
    cross_up = prev["sma_20"] <= prev["sma_50"] and curr["sma_20"] > curr["sma_50"]

    # Mode 2: Already above and trending (relaxed RVOL from 1.1 to 0.8)
    trending_up = (curr["sma_20"] > curr["sma_50"] and
                   curr["close"] > curr["sma_20"] and
                   curr["rsi_14"] > 50 and curr["rsi_14"] < 75 and
                   curr.get("rvol", 0) > 0.8)

    # Mode 3: Approaching crossover — price above both MAs, SMA20 closing gap
    sma_gap = (curr["sma_50"] - curr["sma_20"]) / curr["sma_50"]
    approaching = (sma_gap > 0 and sma_gap < 0.02 and  # within 2% of crossing
                   curr["close"] > curr["sma_50"] and    # price already reclaimed
                   curr["rsi_14"] > 50)

    if not (cross_up or trending_up or approaching):
        return None

    rsi_str = min((curr["rsi_14"] - 50) / 25, 1.0)
    vol_str = min(curr.get("rvol", 1) / 2, 1.0)
    if cross_up:
        trend_str = 1.0
    elif approaching:
        trend_str = 0.5
    else:
        trend_str = 0.6

    score = 0.4 * rsi_str + 0.3 * vol_str + 0.3 * trend_str
    atr = curr.get("atr_14", curr["close"] * 0.02)

    return {
        "strategy": "ma_crossover",
        "direction": "LONG",
        "score": round(max(0.1, min(score, 1.0)), 3),
        "entry": curr["close"],
        "stop": round(curr["sma_50"] - 0.5 * atr, 2),
        "target": round(curr["close"] + 2.5 * atr, 2),
        "thesis": f"SMA20 {'crossed above' if cross_up else 'above'} SMA50, RSI={curr['rsi_14']:.0f}, RVOL={curr.get('rvol',0):.1f}x",
    }


def evaluate_mean_reversion(code: str, df: pd.DataFrame) -> Optional[Dict]:
    """RSI oversold + Bollinger Band bounce with confirmation.

    Two modes:
    1. Classic: RSI < 35 at lower BB (catching the bounce)
    2. Bounce confirmed: RSI was < 35 in last 3 bars and is now turning up
    """
    if len(df) < 25:
        return None
    curr = df.iloc[-1]
    prev = df.iloc[-2]

    if pd.isna(curr.get("rsi_14")) or pd.isna(curr.get("bb_lower")):
        return None

    # Avoid structural breakdown (>15% below 50DMA)
    if not pd.isna(curr.get("sma_50")) and curr["close"] < curr["sma_50"] * 0.85:
        return None

    # Mode 1: Classic oversold entry
    classic = (curr["rsi_14"] < 38 and
               curr["close"] <= curr["bb_lower"] * 1.02)

    # Mode 2: Bounce confirmed — RSI was deeply oversold recently, now turning up
    recent_rsi = [df.iloc[i].get("rsi_14", 50) for i in range(-3, 0)]
    was_oversold = any(r < 35 for r in recent_rsi if not pd.isna(r))
    rsi_turning_up = curr["rsi_14"] > prev.get("rsi_14", 100)
    bounce = (was_oversold and rsi_turning_up and
              curr["rsi_14"] < 45 and
              curr["close"] > prev["close"])  # green candle

    if not (classic or bounce):
        return None

    depth = max(38 - curr["rsi_14"], 0) / 38
    bounce_bonus = 0.15 if bounce else 0
    score = min(0.35 + depth + bounce_bonus, 0.9)
    atr = curr.get("atr_14", curr["close"] * 0.02)

    return {
        "strategy": "mean_reversion",
        "direction": "LONG",
        "score": round(score, 3),
        "entry": curr["close"],
        "stop": round(curr["close"] - 1.5 * atr, 2),
        "target": round(curr["bb_mid"], 2),
        "thesis": f"RSI={curr['rsi_14']:.0f} oversold at lower BB, RVOL={curr.get('rvol',0):.1f}x",
    }


def evaluate_breakout(code: str, df: pd.DataFrame) -> Optional[Dict]:
    """Price breaks above 20-bar resistance with volume and candle quality."""
    if len(df) < 25:
        return None
    curr = df.iloc[-1]
    prev = df.iloc[-2]

    resistance = curr.get("resistance_20")
    if pd.isna(resistance):
        return None

    # Breakout: close above prior resistance, OR close within 0.5% of making new high
    breakout = prev["close"] < resistance and curr["close"] > resistance
    near_breakout = (curr["close"] > resistance * 0.995 and
                     curr["close"] > prev["close"] and
                     curr.get("rsi_14", 50) > 55)

    if not (breakout or near_breakout):
        return None

    # Volume: lowered from 1.8 to 1.2 (yesterday's completed RVOL is now used)
    if curr.get("rvol", 0) < 1.2:
        return None

    # Candle quality: close must be in upper 40% of range (no long upper wick)
    candle_range = curr["high"] - curr["low"]
    if candle_range > 0:
        close_position = (curr["close"] - curr["low"]) / candle_range
        if close_position < 0.4:
            return None

    # Don't chase: skip if already >1.5 ATR above resistance
    atr = curr.get("atr_14", curr["close"] * 0.02)
    if breakout and (curr["close"] - resistance) > 1.5 * atr:
        return None

    # Consolidation filter: require the 10-bar price range (high-low) to be
    # less than 3x ATR before breakout. This avoids false breakouts from
    # choppy/volatile periods where resistance levels are unreliable.
    if len(df) >= 10 and atr > 0:
        recent_10 = df.iloc[-10:]
        price_range = recent_10["high"].max() - recent_10["low"].min()
        if price_range > 3.0 * atr:
            return None  # too volatile / not consolidated

    breakout_pct = (curr["close"] - resistance) / resistance
    vol_str = min(curr["rvol"] / 3, 1.0)
    score = 0.5 * vol_str + 0.5 * min(breakout_pct * 40, 1.0)
    atr = curr.get("atr_14", curr["close"] * 0.02)

    return {
        "strategy": "breakout",
        "direction": "LONG",
        "score": round(max(0.2, min(score, 1.0)), 3),
        "entry": curr["close"],
        "stop": round(resistance - atr, 2),
        "target": round(curr["close"] + 2.5 * atr, 2),
        "thesis": f"Broke resistance {resistance:.2f} with {curr['rvol']:.1f}x volume",
    }


ALL_STRATEGIES = [
    evaluate_ma_crossover,
    evaluate_mean_reversion,
    evaluate_breakout,
]

# ── Trade Context Router ────────────────────────────────────────

def get_broker_position_qty(trade_ctx, code: str) -> float:
    """
    Return the broker's current signed quantity for `code` (positive = long,
    negative = short, 0 = no position). Returns float('nan') on lookup failure
    so callers can decide whether to bail out or proceed.

    This is the guard that prevents zombie shorts: when the bot tries to
    close a long it no longer has, we must not send a naked SELL order
    (Moomoo paper would treat it as a short sale of fresh shares).
    """
    try:
        ret, positions = trade_ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
        if ret != RET_OK or positions is None:
            return float('nan')
        # positions is a DataFrame; find row for this code
        row = positions[positions["code"] == code] if hasattr(positions, "loc") else None
        if row is None or len(row) == 0:
            return 0.0
        return float(row["qty"].iloc[0])
    except Exception as e:
        logger.warning(f"Broker position lookup failed for {code}: {e}")
        return float('nan')


def get_trd_ctx_for_code(code: str, us_trd: OpenUSTradeContext, hk_trd: OpenSecTradeContext):
    """Return the correct trade context based on the ticker's market prefix."""
    if code.startswith("US."):
        return us_trd
    return hk_trd


def get_lot_size(code: str) -> int:
    """Return minimum lot size for HK stocks. US stocks trade in 1-share lots."""
    HK_LOTS = {
        "HK.00700": 100, "HK.09988": 100, "HK.01810": 200, "HK.00005": 400,
        "HK.00388": 100, "HK.02318": 500, "HK.00883": 1000, "HK.02800": 500,
        "HK.03033": 200, "HK.09618": 100, "HK.03690": 100, "HK.01299": 200,
    }
    return HK_LOTS.get(code, 100 if code.startswith("HK.") else 1)


def round_to_lot(qty: int, code: str) -> int:
    """Round quantity down to nearest board lot for HK stocks."""
    lot = get_lot_size(code)
    return (qty // lot) * lot

# ── Market Session Management ──────────────────────────────────

TZ_HK = ZoneInfo("Asia/Hong_Kong")
TZ_ET = ZoneInfo("America/New_York")

# Market hours (local time)
# HK: 09:30 - 16:00 HKT (lunch break 12:00-13:00 ignored for simplicity)
# US: 09:30 - 16:00 ET
HK_OPEN_H, HK_OPEN_M = 9, 30
HK_CLOSE_H, HK_CLOSE_M = 16, 0
US_OPEN_H, US_OPEN_M = 9, 30
US_CLOSE_H, US_CLOSE_M = 16, 0

# How many minutes before close to start flattening
SESSION_FLATTEN_MINUTES = 5

# Holding mode: "day" = flatten all at close (pure day-trade),
#               "swing" = hold longs overnight, flatten only shorts and stale positions.
# Swing mode keeps winners running for multi-day compounding.
HOLD_MODE = "swing"


# ── Regime Configuration ───────────────────────────────────────────
# Regime-based allocation: adjusts exposure, strategy selection, and sizing
# dynamically based on market conditions (SPY vs 200SMA + VIX level).
# v3: REMOVED weight_overrides (score manipulation / snooping) and
# STRATEGY_SIZE_MULT (in-sample overfitting). All strategies get equal capital.
REGIME_CONFIG = {
    "bull": {
        "max_exposure": 1.20,
        "size_mult": 1.0,
        "allowed_strategies": ["ma_crossover", "mean_reversion", "breakout"],
        "description": "SPY > 200SMA, VIX < 25 — full risk budget, all strategies",
    },
    "flat": {
        "max_exposure": 0.80,
        "size_mult": 0.75,
        "allowed_strategies": ["mean_reversion", "ma_crossover"],
        "description": "SPY near 200SMA — partial exposure, mean reversion + MA only",
    },
    "bear": {
        "max_exposure": 0.50,
        "size_mult": 0.50,
        "allowed_strategies": ["mean_reversion"],
        "description": "SPY < 200SMA, VIX > 25 — minimal exposure, mean reversion only",
    },
}

REGIME_STATE_PATH = LOG_DIR / "regime_state.json"
FEEDBACK_PATH = LOG_DIR / "trade_feedback.json"


def _is_hk_market_open() -> bool:
    """Check if HK market is currently in session (Mon-Fri, 9:30-16:00 HKT)."""
    now_hk = datetime.now(TZ_HK)
    if now_hk.weekday() >= 5:  # Sat/Sun
        return False
    t = now_hk.hour * 60 + now_hk.minute
    return (HK_OPEN_H * 60 + HK_OPEN_M) <= t < (HK_CLOSE_H * 60 + HK_CLOSE_M)


def _is_us_market_open() -> bool:
    """Check if US market is currently in session (Mon-Fri, 9:30-16:00 ET)."""
    now_et = datetime.now(TZ_ET)
    if now_et.weekday() >= 5:
        return False
    t = now_et.hour * 60 + now_et.minute
    return (US_OPEN_H * 60 + US_OPEN_M) <= t < (US_CLOSE_H * 60 + US_CLOSE_M)


def _minutes_to_close(market: str) -> int:
    """Minutes until market close. Returns 9999 if market is closed."""
    if market == "HK":
        now = datetime.now(TZ_HK)
        close_min = HK_CLOSE_H * 60 + HK_CLOSE_M
    else:
        now = datetime.now(TZ_ET)
        close_min = US_CLOSE_H * 60 + US_CLOSE_M
    now_min = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return 9999
    diff = close_min - now_min
    return diff if diff > 0 else 9999


def _market_for_code(code: str) -> str:
    """Return 'HK' or 'US' for a given stock code."""
    return "HK" if code.startswith("HK.") else "US"


def get_market_session_status() -> Dict:
    """Get current session status for both markets."""
    now_hk = datetime.now(TZ_HK)
    now_et = datetime.now(TZ_ET)
    return {
        "hk_open": _is_hk_market_open(),
        "hk_time": now_hk.strftime("%H:%M HKT"),
        "hk_minutes_to_close": _minutes_to_close("HK"),
        "us_open": _is_us_market_open(),
        "us_time": now_et.strftime("%H:%M ET"),
        "us_minutes_to_close": _minutes_to_close("US"),
    }


def flatten_market_positions(
    market: str,
    ctx: OpenQuoteContext,
    us_trd: OpenUSTradeContext,
    hk_trd: OpenSecTradeContext,
    journal: List[Dict],
) -> Tuple[List[Dict], float]:
    """Close positions for a given market at end-of-session.

    In "day" mode: flatten everything (pure day-trade).
    In "swing" mode: only close shorts (overnight short risk is dangerous)
    and losing/stale longs. Keep profitable longs for multi-day compounding.

    Returns updated journal and realized P&L from flattening.
    """
    open_trades = [t for t in journal if t.get("status") == "OPEN" and _market_for_code(t["code"]) == market]
    if not open_trades:
        return journal, 0.0

    codes = list(set(t["code"] for t in open_trades))
    snapshots = get_snapshot_data(ctx, codes)
    realized_pnl = 0.0
    flattened = 0

    for trade in open_trades:
        # ── Swing mode filter: keep profitable/near-entry longs overnight ──
        if HOLD_MODE == "swing" and trade.get("side") == "LONG":
            snap = snapshots.get(trade["code"])
            if snap:
                cur = snap["price"]
                entry = float(trade["entry_price"])
                pnl_pct = (cur - entry) / entry if entry > 0 else 0
                # Keep longs that aren't deeply losing (> -2% from entry)
                if pnl_pct > -0.02:
                    logger.info(
                        f"SWING HOLD [{market}] {trade['code']} "
                        f"P&L={pnl_pct:+.1%} — keeping overnight"
                    )
                    continue
                else:
                    logger.info(
                        f"SWING CUT [{market}] {trade['code']} "
                        f"P&L={pnl_pct:+.1%} — closing losing long"
                    )
        code = trade["code"]
        snap = snapshots.get(code)
        if not snap:
            continue

        current_price = snap["price"]
        entry = float(trade["entry_price"])
        direction = trade["side"]
        qty = int(trade["qty"])

        exit_side = TrdSide.SELL if direction == "LONG" else TrdSide.BUY
        trade_ctx = get_trd_ctx_for_code(code, us_trd, hk_trd)

        # GUARD: verify broker actually holds the position before sending close.
        # Prevents the "failed-close → fresh short" zombie bug.
        broker_qty = get_broker_position_qty(trade_ctx, code)
        if direction == "LONG" and broker_qty <= 0:
            logger.warning(
                f"[zombie-guard] {code} session-close: bot says LONG {qty} "
                f"but broker has {broker_qty}. Marking CLOSED without order."
            )
            trade["status"] = "CLOSED"
            trade["exit_time"] = datetime.now().isoformat()
            trade["exit_price"] = current_price
            continue
        if direction == "SHORT" and broker_qty >= 0:
            logger.warning(
                f"[zombie-guard] {code} session-close: bot says SHORT {qty} "
                f"but broker has {broker_qty}. Marking CLOSED without order."
            )
            trade["status"] = "CLOSED"
            trade["exit_time"] = datetime.now().isoformat()
            trade["exit_price"] = current_price
            continue
        # Cap qty to what broker actually holds (never sell/cover more)
        if broker_qty == broker_qty:  # not NaN
            qty = min(qty, int(abs(broker_qty)))

        time.sleep(2.5)
        ret, data = trade_ctx.place_order(
            price=0, qty=qty, code=code,
            trd_side=exit_side,
            order_type=OrderType.MARKET,
            trd_env=TrdEnv.SIMULATE,
            remark=f"exit:session_close:{trade.get('order_id', '')}",
        )

        if ret == RET_OK:
            if direction == "LONG":
                pnl_local = (current_price - entry) * qty
            else:
                pnl_local = (entry - current_price) * qty

            # Normalize to USD (HK positions trade in HKD)
            pnl = to_usd(code, pnl_local)
            pnl_pct = round(pnl_local / (entry * qty) * 100, 2)
            trade["exit_price"] = current_price
            trade["exit_time"] = datetime.now().isoformat()
            trade["pnl"] = round(pnl, 2)
            trade["pnl_pct"] = pnl_pct
            trade["status"] = "CLOSED"
            realized_pnl += pnl
            flattened += 1

            logger.info(
                f"SESSION CLOSE [{market}] {code} {direction} "
                f"P&L=${pnl:+.2f} USD ({pnl_pct:+.1f}%)"
            )

            # Post-trade feedback
            try:
                _regime = "bull"
                if REGIME_STATE_PATH.exists():
                    _regime = json.loads(REGIME_STATE_PATH.read_text()).get("regime", "bull")
                fb = analyze_closed_trade(trade, _regime)
                fb["exit_reason"] = "session_close"
                log_trade_feedback(fb)
            except Exception:
                pass
        else:
            logger.warning(f"SESSION CLOSE failed {code}: {data}")

    # Persist changes
    if flattened > 0:
        with open(JOURNAL_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, JOURNAL_FIELDS)
            writer.writeheader()
            for t in journal:
                writer.writerow({k: t.get(k, "") for k in JOURNAL_FIELDS})

        logger.info(
            f"SESSION FLATTEN [{market}]: {flattened} positions closed, "
            f"realized P&L=${realized_pnl:+,.2f}"
        )

    return journal, realized_pnl


# ── Position Management ─────────────────────────────────────────

def check_exits(
    ctx: OpenQuoteContext,
    us_trd: OpenUSTradeContext,
    hk_trd: OpenSecTradeContext,
    journal: List[Dict],
) -> Tuple[List[Dict], float]:
    """Check open trades for stop-loss or take-profit exits. Returns updated journal and realized P&L."""
    open_trades = [t for t in journal if t.get("status") == "OPEN"]
    if not open_trades:
        return journal, 0.0

    codes = list(set(t["code"] for t in open_trades))
    snapshots = get_snapshot_data(ctx, codes)
    realized_pnl = 0.0

    # Publish latest snapshot prices + live unrealized P&L for the dashboard.
    # Keyed by code; consumed by dashboard/web_server.read_latest_prices().
    try:
        latest = {}
        now_iso = datetime.now().isoformat(timespec="seconds")
        for t in open_trades:
            code_i = t["code"]
            snap_i = snapshots.get(code_i)
            if not snap_i:
                continue
            cp = float(snap_i["price"])
            ep = float(t.get("entry_price") or 0)
            q = int(t.get("qty") or 0)
            side = t.get("side", "LONG")
            pnl_local = (cp - ep) * q if side == "LONG" else (ep - cp) * q
            pnl_usd = to_usd(code_i, pnl_local)
            pnl_pct = (pnl_local / (ep * q) * 100) if ep > 0 and q > 0 else 0.0
            latest[code_i] = {
                "price": round(cp, 4),
                "ts": now_iso,
                "unrealized_pnl": round(pnl_usd, 2),
                "unrealized_pnl_pct": round(pnl_pct, 2),
            }
        (LOG_DIR / "latest_prices.json").write_text(json.dumps(latest))
    except Exception as e:
        logger.debug(f"latest_prices write failed: {e}")

    for trade in open_trades:
        code = trade["code"]
        snap = snapshots.get(code)
        if not snap:
            continue

        current_price = snap["price"]
        entry = float(trade["entry_price"])
        stop = float(trade["stop_loss"])
        target = float(trade["take_profit"])
        direction = trade["side"]
        qty = int(trade["qty"])

        should_exit = False
        exit_reason = ""

        # ── Trailing stop logic ──────────────────────────────
        # After 1×ATR profit: move stop to breakeven
        # After 2×ATR profit: trail at current_price - 1.5×ATR
        atr_est = abs(target - entry) / 2.5 if direction == "LONG" else abs(entry - target) / 2.5
        if atr_est <= 0:
            atr_est = entry * 0.02  # fallback 2%

        if direction == "LONG":
            unrealized = current_price - entry
            if unrealized >= 2 * atr_est:
                # Trail at 1.5 ATR below current price
                trailing_stop = round(current_price - 1.5 * atr_est, 2)
                if trailing_stop > stop:
                    logger.debug(f"TRAIL: {code} stop {stop:.2f} -> {trailing_stop:.2f} (price={current_price:.2f})")
                    trade["stop_loss"] = str(trailing_stop)
                    stop = trailing_stop
            elif unrealized >= 1 * atr_est:
                # Move to breakeven
                if entry > stop:
                    logger.debug(f"BREAKEVEN: {code} stop {stop:.2f} -> {entry:.2f}")
                    trade["stop_loss"] = str(entry)
                    stop = entry
        else:  # SHORT
            unrealized = entry - current_price
            if unrealized >= 2 * atr_est:
                trailing_stop = round(current_price + 1.5 * atr_est, 2)
                if trailing_stop < stop:
                    logger.debug(f"TRAIL: {code} stop {stop:.2f} -> {trailing_stop:.2f}")
                    trade["stop_loss"] = str(trailing_stop)
                    stop = trailing_stop
            elif unrealized >= 1 * atr_est:
                if entry < stop:
                    logger.debug(f"BREAKEVEN: {code} stop {stop:.2f} -> {entry:.2f}")
                    trade["stop_loss"] = str(entry)
                    stop = entry

        # ── Profit-protection layer (rally-aware, % ratchet + fade signals)
        # Supplements the ATR-based trailing above with tighter percentage
        # thresholds and macro rally-extension exit rules. Uses the cached
        # SPY extension level computed once per cycle.
        if direction == "LONG":
            try:
                from risk.profit_protection import evaluate_exit as _eval_exit
                from data.indicators import compute_signals as _compute_signals
                from data.earnings_calendar import days_to_next_earnings as _dte

                _spy_ext = _get_spy_extension(ctx)
                _unreal_pct = (current_price - entry) / entry if entry else 0.0
                _sleeve = trade.get("pod", "")
                _is_core = any(k in _sleeve.lower() for k in ("core",))

                # Fetch recent daily klines for indicator computation.
                # US symbols use standard daily bars; HK same. Limit 25 bars
                # so each call is fast (< 0.1 s typically).
                _ind_signals = {"rsi_divergence": False, "vol_fade": False,
                                "upper_wick": False}
                try:
                    _start_kl = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
                    _end_kl   = datetime.now().strftime("%Y-%m-%d")
                    _kl_result = ctx.request_history_kline(
                        code, start=_start_kl, end=_end_kl,
                        ktype=KLType.K_DAY, max_count=25,
                    )
                    # Returns (ret, df, page_req) — unpack positionally
                    _kl_df = _kl_result[1] if len(_kl_result) >= 2 else None
                    if _kl_df is not None and not _kl_df.empty:
                        _ind_signals = _compute_signals(_kl_df)
                except Exception as _ki:
                    logger.debug(f"kline fetch for indicators {code}: {_ki}")

                # Earnings blackout — only meaningful for US equities
                _days_to_earn = None
                if code.startswith("US."):
                    try:
                        _days_to_earn = _dte(code)
                    except Exception:
                        pass

                _decision = _eval_exit(
                    entry=entry,
                    current_price=current_price,
                    current_stop=stop,
                    current_tp=target if target else entry * 1.06,
                    sleeve="core" if _is_core else "tactical",
                    strategy=trade.get("strategy", ""),
                    spy_extension=_spy_ext,
                    rsi_divergence=_ind_signals.get("rsi_divergence", False),
                    vol_fade=_ind_signals.get("vol_fade", False),
                    upper_wick=_ind_signals.get("upper_wick", False),
                    days_to_earnings=_days_to_earn,
                )
                if _decision.should_exit and not should_exit:
                    should_exit = True
                    exit_reason = f"profit_protect"
                    logger.info(
                        f"[PROFIT-PROTECT] EXIT {code} @ {current_price:.2f} "
                        f"(+{_unreal_pct*100:.1f}%): {_decision.reason}"
                    )
                elif _decision.trim_pct > 0 and not should_exit:
                    # Full exit in paper (partial fills not implemented yet)
                    logger.info(
                        f"[PROFIT-PROTECT] TRIM {int(_decision.trim_pct*100)}% {code} "
                        f"@ {current_price:.2f}: {_decision.reason}"
                    )
                    should_exit = True
                    exit_reason = f"profit_protect_trim"
                if _decision.new_stop and _decision.new_stop > stop and not should_exit:
                    logger.info(
                        f"[PROFIT-PROTECT] STOP {code}: {stop:.2f} → "
                        f"{_decision.new_stop:.2f} ({_decision.reason})"
                    )
                    trade["stop_loss"] = str(round(_decision.new_stop, 2))
                    stop = _decision.new_stop
            except Exception as _ppe:
                logger.debug(f"profit-protect {code}: {_ppe}")

        # ── Standard exit checks ─────────────────────────────
        if direction == "LONG":
            if current_price <= stop:
                should_exit = True
                exit_reason = "stop_loss"
            elif current_price >= target:
                should_exit = True
                exit_reason = "take_profit"
        else:  # SHORT
            if current_price >= stop:
                should_exit = True
                exit_reason = "stop_loss"
            elif current_price <= target:
                should_exit = True
                exit_reason = "take_profit"

        # ── Time-based exit ──────────────────────────────────
        # 5 days with <1% move = dead money, close it
        # 10 days = hard max regardless
        if not should_exit:
            try:
                open_time = datetime.fromisoformat(trade.get("timestamp", ""))
                days_held = (datetime.now() - open_time).days
                pnl_pct = abs(current_price - entry) / entry if entry > 0 else 0
                if days_held >= 5 and pnl_pct < 0.01:
                    should_exit = True
                    exit_reason = "time_exit_stale"
                    logger.info(f"TIME EXIT: {code} held {days_held}d with {pnl_pct:.1%} move")
                elif days_held >= 10:
                    should_exit = True
                    exit_reason = "time_exit_max"
                    logger.info(f"MAX TIME EXIT: {code} held {days_held}d")
            except Exception:
                pass

        # === POSITION-CLASS GATE ===
        # COMPOUNDERS exit ONLY on thesis break, not technical signals.
        # CATALYST positions respect min_hold_days.
        # TACTICAL positions exit normally.
        if should_exit:
            try:
                from risk.position_classifier import should_exit as class_should_exit
                from data.trade_thesis import get_active_theses
                _theses = get_active_theses()
                _thesis = _theses.get(code)
                if _thesis:
                    pc_exit, pc_reason = class_should_exit(
                        code=code, thesis=_thesis, side=direction,
                        current_price=current_price,
                        technical_signal_exit=True,
                        thesis_break_detected=False,  # would come from monitor
                        stop_price=trade.get("stop_loss"),
                    )
                    if not pc_exit:
                        logger.info(
                            f"[pos-class] HOLD {code}: {pc_reason} "
                            f"(technical wanted exit_reason={exit_reason})"
                        )
                        should_exit = False
                    else:
                        logger.info(f"[pos-class] EXIT OK {code}: {pc_reason}")
            except Exception as e:
                logger.debug(f"position_classifier check failed: {e}")

        if should_exit:
            # Place exit order via correct market context
            exit_side = TrdSide.SELL if direction == "LONG" else TrdSide.BUY
            trade_ctx = get_trd_ctx_for_code(code, us_trd, hk_trd)

            # GUARD: verify broker actually holds the position before sending close.
            # Prevents the "failed-close → fresh short" zombie bug.
            broker_qty = get_broker_position_qty(trade_ctx, code)
            if direction == "LONG" and broker_qty <= 0:
                logger.warning(
                    f"[zombie-guard] {code} intraday-exit ({exit_reason}): "
                    f"bot says LONG {qty} but broker has {broker_qty}. "
                    f"Marking CLOSED without order."
                )
                trade["status"] = "CLOSED"
                trade["exit_time"] = datetime.now().isoformat()
                trade["exit_price"] = current_price
                continue
            if direction == "SHORT" and broker_qty >= 0:
                logger.warning(
                    f"[zombie-guard] {code} intraday-exit ({exit_reason}): "
                    f"bot says SHORT {qty} but broker has {broker_qty}. "
                    f"Marking CLOSED without order."
                )
                trade["status"] = "CLOSED"
                trade["exit_time"] = datetime.now().isoformat()
                trade["exit_price"] = current_price
                continue
            if broker_qty == broker_qty:  # not NaN
                qty = min(qty, int(abs(broker_qty)))

            ret, data = trade_ctx.place_order(
                price=0, qty=qty, code=code,
                trd_side=exit_side,
                order_type=OrderType.MARKET,
                trd_env=TrdEnv.SIMULATE,
                remark=f"exit:{exit_reason}:{trade.get('order_id', '')}",
            )

            if ret == RET_OK:
                if direction == "LONG":
                    pnl_local = (current_price - entry) * qty
                else:
                    pnl_local = (entry - current_price) * qty

                # Normalize to USD (HK positions trade in HKD)
                pnl = to_usd(code, pnl_local)
                pnl_pct = round(pnl_local / (entry * qty) * 100, 2)

                trade["exit_price"] = current_price
                trade["exit_time"] = datetime.now().isoformat()
                trade["pnl"] = round(pnl, 2)
                trade["pnl_pct"] = pnl_pct
                trade["status"] = "CLOSED"
                realized_pnl += pnl

                logger.info(
                    f"EXIT [{exit_reason}] {code} {direction} "
                    f"P&L=${pnl:+.2f} USD ({pnl_pct:+.1f}%)"
                )

                # Post-trade feedback loop
                try:
                    _regime = "bull"
                    if REGIME_STATE_PATH.exists():
                        _regime = json.loads(REGIME_STATE_PATH.read_text()).get("regime", "bull")
                    fb = analyze_closed_trade(trade, _regime)
                    fb["exit_reason"] = exit_reason
                    log_trade_feedback(fb)
                    if fb["mistakes"]:
                        logger.info(f"FEEDBACK {code}: {'; '.join(fb['mistakes'])}")
                except Exception as _fb_err:
                    logger.debug(f"Feedback analysis failed: {_fb_err}")

    # Persist any trailing stop updates even if no exits happened
    # (so updated stops survive restarts)
    with open(JOURNAL_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, JOURNAL_FIELDS)
        writer.writeheader()
        for t in journal:
            writer.writerow({k: t.get(k, "") for k in JOURNAL_FIELDS})

    return journal, realized_pnl

# ── Sleeve Classification (daily, background) ──────────────────

_last_sleeve_date: Optional[str] = None

def _run_daily_sleeve_classification(regime: str, open_codes: set):
    """Run sleeve classification once per day in a background thread.

    Classifies the US universe into Core/Tactical/None using fundamental
    data from yfinance. Results are saved to logs/sleeve_assignments.json
    for the dashboard Sleeves tab.
    """
    global _last_sleeve_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _last_sleeve_date == today:
        return  # already ran today

    import threading

    def _classify():
        global _last_sleeve_date
        try:
            from fundamental.sleeve import classify_universe, save_sleeve_assignments
            # Strip market prefix for yfinance (US.AAPL → AAPL)
            tickers = [c.replace("US.", "") for c in US_UNIVERSE + ETF_UNIVERSE]
            existing = [c.replace("US.", "") for c in open_codes if c.startswith("US.")]
            logger.info(f"Running daily sleeve classification for {len(tickers)} tickers (regime={regime})")
            assignments = classify_universe(
                tickers=tickers,
                regime=regime,
                existing_positions=existing,
            )
            if assignments:
                save_sleeve_assignments(assignments)
                core = sum(1 for a in assignments.values() if a.sleeve == "core")
                tac = sum(1 for a in assignments.values() if a.sleeve == "tactical")
                logger.info(f"Sleeve classification complete: {core} core, {tac} tactical, "
                            f"{len(assignments) - core - tac} none")
            _last_sleeve_date = today
        except Exception as e:
            logger.error(f"Sleeve classification failed: {e}")

    t = threading.Thread(target=_classify, daemon=True, name="sleeve-classify")
    t.start()


def _run_weekly_core_rebalance(regime: str) -> set:
    """Run core sleeve rebalance if 7+ days since last rebalance.

    Returns set of tickers currently in core sleeve (for R1 enforcement).
    """
    rb_state = load_rebalance_state()
    core_tickers = get_core_tickers(rb_state)

    if not should_rebalance(rb_state):
        return core_tickers

    # Load latest sleeve assignments
    assignments_file = LOG_DIR / "sleeve_assignments.json"
    if not assignments_file.exists():
        logger.info("No sleeve assignments yet — skipping core rebalance")
        return core_tickers

    try:
        import json as _json
        raw = _json.loads(assignments_file.read_text())

        # Reconstruct minimal SleeveAssignment objects from saved JSON
        from fundamental.sleeve import SleeveAssignment
        from fundamental.quality import QualityScore
        from fundamental.valuation import ValuationScore

        assignments = {}
        for tk, data in raw.items():
            q = QualityScore(
                ticker=tk,
                composite=data.get("quality_composite") or 0,
                grade=data.get("quality_grade") or "D",
            )
            v = ValuationScore(
                ticker=tk,
                composite=data.get("valuation_composite") or 0,
                dcf_fair_value=data.get("dcf_fair_value"),
                dcf_margin_of_safety=data.get("dcf_mos") or 0,
            )
            assignments[tk] = SleeveAssignment(
                ticker=tk,
                sleeve=data.get("sleeve", "none"),
                quality=q,
                valuation=v,
                strategy=data.get("strategy", "wait"),
                confidence=data.get("confidence", 0),
                reasoning=data.get("reasoning", ""),
            )

        # Build minimal portfolio state for concentration checks
        from fundamental.portfolio import PortfolioState
        pf_state = PortfolioState(
            core_positions={
                tk: {"notional": h.get("notional", 0)}
                for tk, h in rb_state.core_holdings.items()
            },
        )

        actions = generate_rebalance_actions(assignments, pf_state, regime, rb_state)
        if actions:
            log_rebalance_actions(actions)
            rb_state = execute_rebalance(actions, rb_state)
            adds = sum(1 for a in actions if a.action in ("add", "sell_csp"))
            trims = sum(1 for a in actions if a.action in ("trim", "exit"))
            ccs = sum(1 for a in actions if a.action == "attach_cc")
            logger.info(f"Core rebalance: {adds} adds, {trims} trims, {ccs} CC overlays, "
                        f"{len(rb_state.core_holdings)} total core positions")
        else:
            rb_state.last_rebalance_date = datetime.now().strftime("%Y-%m-%d")
            from fundamental.rebalancer import save_rebalance_state
            save_rebalance_state(rb_state)
            logger.info("Core rebalance: no changes needed")

        return get_core_tickers(rb_state)

    except Exception as e:
        logger.error(f"Core rebalance failed: {e}")
        return core_tickers


# ── Main Loop ───────────────────────────────────────────────────

def run_cycle(
    ctx: OpenQuoteContext,
    us_trd: OpenUSTradeContext,
    hk_trd: OpenSecTradeContext,
    cycle_num: int,
    daily_pnl: float,
    consecutive_losses: int,
    last_loss_time: Optional[datetime],
) -> Tuple[float, int, Optional[datetime]]:
    """Run one full scan-evaluate-execute cycle."""
    logger.info(f"{'='*60}")
    logger.info(f"  CYCLE {cycle_num} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'='*60}")

    # Load journal and learning stats
    journal = read_journal()
    strat_stats = load_strategy_stats()
    open_trades = [t for t in journal if t.get("status") == "OPEN"]
    open_codes = set(t["code"] for t in open_trades)

    # Warm earnings-date cache for all open US positions (background threads)
    try:
        from data.earnings_calendar import prefetch_batch as _prefetch_earnings
        us_open = [t["code"] for t in open_trades if t["code"].startswith("US.")]
        if us_open:
            _prefetch_earnings(us_open)
    except Exception as _ec_err:
        logger.debug(f"earnings prefetch skipped: {_ec_err}")

    # 1. CHECK EXITS on open positions
    journal, exit_pnl = check_exits(ctx, us_trd, hk_trd, journal)
    daily_pnl += exit_pnl
    if exit_pnl != 0:
        # Rewrite journal with updated exits
        with open(JOURNAL_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, JOURNAL_FIELDS)
            writer.writeheader()
            for t in journal:
                writer.writerow({k: t.get(k, "") for k in JOURNAL_FIELDS})

        # Update learning
        strat_stats = update_learning(journal)

        # Update consecutive losses
        recently_closed = [t for t in journal if t.get("status") == "CLOSED"]
        if recently_closed:
            last_closed = recently_closed[-1]
            if float(last_closed.get("pnl", 0)) < 0:
                consecutive_losses += 1
                last_loss_time = datetime.now()
            else:
                consecutive_losses = 0

    # 1b. SESSION MANAGEMENT — flatten positions at market close
    session = get_market_session_status()
    logger.info(
        f"Markets: HK={'OPEN' if session['hk_open'] else 'CLOSED'} ({session['hk_time']}) | "
        f"US={'OPEN' if session['us_open'] else 'CLOSED'} ({session['us_time']})"
    )

    # 1c. REGIME DETECTION — classify market as bull/flat/bear (once per day)
    regime_state = fetch_regime_state(ctx)
    regime = regime_state.get("regime", "bull")
    regime_cfg = get_regime_config(regime)
    vix_level = regime_state.get("vix_level", 20.0)

    # 1d. SLEEVE CLASSIFICATION — run once per day in background thread
    # Classifies universe into Core / Tactical / None using fundamentals.
    # Results saved to logs/sleeve_assignments.json for dashboard display.
    _run_daily_sleeve_classification(regime, open_codes)

    # 1e. CORE SLEEVE REBALANCE — run once per week
    # Checks for core position adds/trims/exits and CC overlay opportunities.
    _core_tickers = _run_weekly_core_rebalance(regime)

    # Flatten HK positions near HK close
    if session["hk_open"] and session["hk_minutes_to_close"] <= SESSION_FLATTEN_MINUTES:
        logger.info(f"HK closing in {session['hk_minutes_to_close']}min — flattening HK positions")
        journal, flatten_pnl = flatten_market_positions("HK", ctx, us_trd, hk_trd, journal)
        daily_pnl += flatten_pnl
        if flatten_pnl != 0:
            strat_stats = update_learning(journal)

    # Flatten US positions near US close
    if session["us_open"] and session["us_minutes_to_close"] <= SESSION_FLATTEN_MINUTES:
        logger.info(f"US closing in {session['us_minutes_to_close']}min — flattening US positions")
        journal, flatten_pnl = flatten_market_positions("US", ctx, us_trd, hk_trd, journal)
        daily_pnl += flatten_pnl
        if flatten_pnl != 0:
            strat_stats = update_learning(journal)

    # Refresh open trades after exits + flattening
    journal = read_journal()
    open_trades = [t for t in journal if t.get("status") == "OPEN"]
    open_codes = set(t["code"] for t in open_trades)

    # 2. RISK CHECKS — query both US and HK accounts. Total balance is the
    # sum of both books in USD; HK figures are converted from HKD.
    ret, acct = us_trd.accinfo_query(trd_env=TrdEnv.SIMULATE)
    if ret != RET_OK:
        logger.error("Cannot query account")
        return daily_pnl, consecutive_losses, last_loss_time

    def _safe_float(v, default=0.0):
        if v is None or v == "" or v == "N/A":
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    us_total = _safe_float(acct.iloc[0].get("total_assets"))
    us_cash = _safe_float(acct.iloc[0].get("cash"))
    us_market_val = _safe_float(acct.iloc[0].get("market_val"))
    us_frozen = _safe_float(acct.iloc[0].get("frozen_cash"))
    us_power = _safe_float(acct.iloc[0].get("power"))

    ret_hk, acct_hk = hk_trd.accinfo_query(trd_env=TrdEnv.SIMULATE)
    hk_total = hk_cash = hk_market_val = hk_frozen = hk_power = 0.0
    hk_total_hkd = hk_cash_hkd = hk_market_val_hkd = hk_frozen_hkd = hk_power_hkd = 0.0
    if ret_hk == RET_OK and not acct_hk.empty:
        hk_total_hkd = _safe_float(acct_hk.iloc[0].get("total_assets"))
        hk_cash_hkd = _safe_float(acct_hk.iloc[0].get("cash"))
        hk_market_val_hkd = _safe_float(acct_hk.iloc[0].get("market_val"))
        hk_frozen_hkd = _safe_float(acct_hk.iloc[0].get("frozen_cash"))
        hk_power_hkd = _safe_float(acct_hk.iloc[0].get("power"))
        hk_total = hk_total_hkd / HKD_PER_USD
        hk_cash = hk_cash_hkd / HKD_PER_USD
        hk_market_val = hk_market_val_hkd / HKD_PER_USD
        hk_frozen = hk_frozen_hkd / HKD_PER_USD
        hk_power = hk_power_hkd / HKD_PER_USD

    balance = us_total + hk_total
    cash = us_cash + hk_cash

    # Stage 1 live-deployment controller — tick with US equity (Stage 1 is
    # US-only). Shadow mode is on until scripts/enable_live.py flips it.
    # The controller tracks HWM / floors / kill switches independently of
    # the paper risk layer above.
    if _STAGE_ENABLED:
        try:
            stage_ctl = _get_stage_controller()
            stage_actions = stage_ctl.tick(float(us_total))
            if stage_actions:
                logger.warning(f"[STAGE] tick actions: {stage_actions}")
        except Exception as e:
            logger.error(f"[STAGE] tick failed: {e}")

    # Daily loss check
    if daily_pnl < -(balance * MAX_DAILY_LOSS_PCT):
        logger.warning(f"DAILY LOSS LIMIT: ${daily_pnl:.2f} — halting")
        return daily_pnl, consecutive_losses, last_loss_time

    # Cooldown check
    if consecutive_losses >= COOLDOWN_AFTER_LOSSES:
        if last_loss_time and datetime.now() < last_loss_time + timedelta(minutes=COOLDOWN_MINUTES):
            remaining = (last_loss_time + timedelta(minutes=COOLDOWN_MINUTES) - datetime.now()).seconds // 60
            logger.warning(f"COOLDOWN: {consecutive_losses} losses, {remaining}min remaining")
            return daily_pnl, consecutive_losses, last_loss_time
        else:
            consecutive_losses = 0

    # Max trades check
    if len(open_trades) >= MAX_OPEN_TRADES:
        logger.info(f"Max open trades ({MAX_OPEN_TRADES}) reached — scan only")

    # Gross exposure check + broker-authoritative position snapshot.
    # Everything written to broker_snapshot.json is the single source of truth
    # for the dashboard's 持仓市值 / 持仓盈亏 / 现金 / 剩余流动性 numbers.
    def _f(v, default=0.0):
        """Robust float() that handles moomoo's 'N/A' string returns."""
        if v is None or v == "" or v == "N/A":
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    gross_exposure = 0
    broker_positions = []
    broker_holdings_pnl = 0.0
    broker_today_pnl = 0.0
    for tctx, market in [(us_trd, "US"), (hk_trd, "HK")]:
        ret, positions = tctx.position_list_query(trd_env=TrdEnv.SIMULATE)
        if ret == RET_OK:
            for _, r in positions.iterrows():
                mv_local = abs(_f(r.get("market_val")))
                pl_val_local = _f(r.get("pl_val"))
                today_pl_local = _f(r.get("today_pl_val"))
                cost_local = _f(r.get("cost_price_val"))
                qty = _f(r.get("qty"))
                fx = HKD_PER_USD if market == "HK" else 1.0
                mv_usd = mv_local / fx
                pl_val_usd = pl_val_local / fx
                today_pl_usd = today_pl_local / fx
                gross_exposure += mv_usd
                broker_holdings_pnl += pl_val_usd
                broker_today_pnl += today_pl_usd
                broker_positions.append({
                    "code": r.get("code", ""),
                    "stock_name": str(r.get("stock_name", "") or ""),
                    "market": market,
                    "qty": qty,
                    "market_val_usd": round(mv_usd, 2),
                    "market_val_local": round(mv_local, 2),
                    "cost_price_local": round(cost_local, 4),
                    "pl_val_usd": round(pl_val_usd, 2),
                    "pl_ratio_pct": round(_f(r.get("pl_ratio")), 2),
                    "today_pl_val_usd": round(today_pl_usd, 2),
                    "nominal_price": _f(r.get("nominal_price")),
                })

    # Write broker snapshot for the dashboard
    try:
        snap = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "balance_total": round(balance, 2),
            "us": {
                "total_assets": round(us_total, 2),
                "cash": round(us_cash, 2),
                "market_val": round(us_market_val, 2),
                "frozen_cash": round(us_frozen, 2),
                "power": round(us_power, 2),
            },
            "hk": {
                "total_assets_usd": round(hk_total, 2),
                "cash_usd": round(hk_cash, 2),
                "market_val_usd": round(hk_market_val, 2),
                "frozen_cash_usd": round(hk_frozen, 2),
                "power_usd": round(hk_power, 2),
                "total_assets_hkd": round(hk_total_hkd, 2),
                "cash_hkd": round(hk_cash_hkd, 2),
                "market_val_hkd": round(hk_market_val_hkd, 2),
                "frozen_cash_hkd": round(hk_frozen_hkd, 2),
                "power_hkd": round(hk_power_hkd, 2),
                "fx_hkd_per_usd": HKD_PER_USD,
            },
            "cash_total": round(cash, 2),
            "market_val_total": round(us_market_val + hk_market_val, 2),
            "holdings_pnl": round(broker_holdings_pnl, 2),
            "today_pnl_broker": round(broker_today_pnl, 2),
            "positions": broker_positions,
            "n_positions": len(broker_positions),
        }
        (LOG_DIR / "broker_snapshot.json").write_text(json.dumps(snap, indent=2, default=str))
    except Exception as e:
        logger.debug(f"broker snapshot write failed: {e}")

    exposure_pct = gross_exposure / balance if balance > 0 else 0
    # Use regime-adjusted exposure cap (bear=0.60, flat=1.00, bull=1.50)
    regime_max_exposure = regime_cfg.get("max_exposure", MAX_GROSS_EXPOSURE)
    effective_max_exposure = min(MAX_GROSS_EXPOSURE, regime_max_exposure)
    can_trade = len(open_trades) < MAX_OPEN_TRADES and exposure_pct < effective_max_exposure

    logger.info(f"Account: ${balance:,.0f} | Cash: ${cash:,.0f} | "
                f"Exposure: {exposure_pct:.1%} (cap={effective_max_exposure:.0%}) | "
                f"Open: {len(open_trades)}/{MAX_OPEN_TRADES} | "
                f"Regime: {regime.upper()} | Daily P&L: ${daily_pnl:+,.2f}")

    # 3. SCAN UNIVERSE (only scan markets that are open and not about to close)
    all_universe = []
    min_minutes_for_entry = 15  # don't open positions within 15 min of close

    # HK scanning DISABLED — focus on US equities (per strategic decision Jun 2026)
    # Existing HK positions still tracked & exited normally; just no new HK entries.
    HK_SCAN_ENABLED = os.environ.get("HK_SCAN_ENABLED", "0") == "1"
    if HK_SCAN_ENABLED and session["hk_open"] and session["hk_minutes_to_close"] > min_minutes_for_entry:
        all_universe += HK_UNIVERSE
        logger.info(f"HK market open — scanning {len(HK_UNIVERSE)} HK tickers")
    else:
        logger.info("HK scanning disabled (focus on US equities). Existing HK positions still managed.")

    if session["us_open"] and session["us_minutes_to_close"] > min_minutes_for_entry:
        all_universe += US_UNIVERSE + ETF_UNIVERSE
        logger.info(f"US market open — scanning {len(US_UNIVERSE) + len(ETF_UNIVERSE)} US tickers "
                    f"({session['us_minutes_to_close']}min to close)")
    elif not session["us_open"]:
        logger.info("US market closed — skipping US scan")
    else:
        logger.info(f"US closing in {session['us_minutes_to_close']}min — no new US entries")

    signals = []

    for code in all_universe:
        if code in open_codes:
            continue  # skip tickers we already have positions in

        # R1: Skip tickers assigned to core sleeve (no overlap)
        bare_ticker = code.replace("US.", "").replace("HK.", "")
        if bare_ticker in _core_tickers:
            continue

        time.sleep(0.35)  # rate limit
        df = get_enriched_data(ctx, code)
        if df is None:
            continue

        # Run strategies (filtered by regime)
        allowed_strats = regime_cfg.get("allowed_strategies")  # None = all
        for strat_fn in ALL_STRATEGIES:
            try:
                # Regime gating: skip strategies not allowed in current regime
                strat_name_check = strat_fn.__name__.replace("evaluate_", "")
                if allowed_strats is not None and strat_name_check not in allowed_strats:
                    continue

                sig = strat_fn(code, df)
                if sig is None:
                    continue

                # Apply learned adjustments
                strat_name = sig["strategy"]
                if strat_name in strat_stats:
                    adj = strat_stats[strat_name].get("score_adjustment", 0)
                    sig["score"] = round(min(max(sig["score"] + adj, 0.05), 1.0), 3)
                    sig["size_mult"] = strat_stats[strat_name].get("size_multiplier", 1.0)
                else:
                    sig["size_mult"] = 1.0

                # v3: REMOVED weight_overrides — signal scores reflect pure conviction

                if sig["score"] >= MIN_SIGNAL_SCORE:
                    sig["code"] = code
                    signals.append(sig)
                    # Unified lifecycle log — origination event
                    try:
                        from data.signal_log import log_originated as _log_orig
                        sig["signal_id"] = _log_orig(
                            symbol=code, strategy=sig["strategy"],
                            direction=sig["direction"], score=float(sig["score"]),
                            thesis=sig.get("thesis", ""),
                        )
                    except Exception as _sle:
                        logger.debug(f"signal_log origination failed: {_sle}")
                    logger.info(
                        f"SIGNAL: {code} {sig['direction']} score={sig['score']} "
                        f"via {sig['strategy']} [{regime}] — {sig['thesis']}"
                    )
                else:
                    # Below-threshold ideas — log as scored_fail so we can see
                    # how many ideas die at the score filter (pre-gate)
                    try:
                        from data.signal_log import log_event as _log_ev
                        _log_ev(
                            symbol=code, strategy=sig["strategy"],
                            direction=sig["direction"],
                            stage="scored", verdict="FAIL",
                            detail=f"score {sig['score']:.2f} < min {MIN_SIGNAL_SCORE:.2f}",
                            score=float(sig["score"]),
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"Strategy error on {code}: {e}")

    # 3b. SCAN OPTIONS — run on top US names directly (not just signal list)
    try:
        stock_data_for_options = {}
        # Scan top 10 liquid US names for options, independent of equity signals
        option_scan_codes = ["US.AAPL", "US.MSFT", "US.NVDA", "US.AMZN", "US.GOOGL",
                             "US.META", "US.TSLA", "US.AMD", "US.GS", "US.SPY"]
        # Add any stocks we have open positions in
        for t in open_trades:
            c = t.get("code", "")
            if c.startswith("US.") and c not in option_scan_codes:
                option_scan_codes.append(c)

        # Get snapshot + indicator data for each
        snap_batch = get_snapshot_data(ctx, option_scan_codes)
        for c in option_scan_codes:
            if c not in snap_batch:
                continue
            time.sleep(0.3)
            df = get_enriched_data(ctx, c)
            if df is not None:
                curr = df.iloc[-1]
                stock_data_for_options[c] = {
                    "price": snap_batch[c]["price"],
                    "rsi": float(curr.get("rsi_14", 50)),
                    "atr": float(curr.get("atr_14", snap_batch[c]["price"] * 0.02)),
                }

        logger.info(f"Options scan: {len(stock_data_for_options)} stocks queued")

        # Publish underlying prices for options MTM — merge into latest_prices.json.
        # These tickers may not be in the equity book but the dashboard needs them
        # to compute unrealized P&L on option ideas.
        try:
            lp_path = LOG_DIR / "latest_prices.json"
            latest_lp = {}
            if lp_path.exists():
                try:
                    latest_lp = json.loads(lp_path.read_text())
                except Exception:
                    latest_lp = {}
            now_iso = datetime.now().isoformat(timespec="seconds")
            for c, d in stock_data_for_options.items():
                if c not in latest_lp:
                    latest_lp[c] = {
                        "price": round(float(d.get("price") or 0), 4),
                        "ts": now_iso,
                        "unrealized_pnl": 0.0,
                        "unrealized_pnl_pct": 0.0,
                    }
                else:
                    # Refresh price even if the ticker has an open position row —
                    # the options scan may be more recent.
                    latest_lp[c]["price"] = round(float(d.get("price") or 0), 4)
                    latest_lp[c]["ts"] = now_iso
            lp_path.write_text(json.dumps(latest_lp))
        except Exception as e:
            logger.debug(f"latest_prices option merge failed: {e}")

        option_ideas = scan_option_ideas(ctx, stock_data_for_options, max_ideas=5)
        for idea in option_ideas:
            logger.info(
                f"OPTION IDEA: {idea.get('stock_code','')} {idea['strategy']} "
                f"score={idea['score']} — {idea['thesis']}"
            )
        # Log option ideas to a separate file.
        # Key rule: once an idea's entry premium is locked, subsequent scans
        # for the same (strategy, stock_code) must NOT overwrite it — only
        # refresh the underlying price for MTM. Otherwise P&L is always 0.
        if option_ideas:
            option_log = LOG_DIR / "option_ideas.json"
            import json as _json
            existing = []
            if option_log.exists():
                try:
                    existing = _json.loads(option_log.read_text())
                except Exception:
                    existing = []
            # Build index of existing ideas by (strategy, stock_code)
            existing_idx = {}
            for i, ex in enumerate(existing):
                key = (ex.get("strategy"), ex.get("stock_code"))
                existing_idx[key] = i  # last occurrence wins
            now_iso = datetime.now().isoformat()
            for idea in option_ideas:
                key = (idea.get("strategy"), idea.get("stock_code"))
                idea["scan_time"] = now_iso
                idea["cycle"] = cycle_num
                if key in existing_idx:
                    # Existing idea: keep original entry premium + entry_underlying,
                    # update current_underlying for MTM and refresh scan_time.
                    orig = existing[existing_idx[key]]
                    orig["current_underlying"] = idea.get("entry_underlying", orig.get("entry_underlying"))
                    orig["scan_time"] = now_iso
                    orig["cycle"] = cycle_num
                    orig["score"] = idea.get("score", orig.get("score"))
                    # Don't overwrite premium, entry_underlying, option_code
                else:
                    # New idea: lock entry premium at first scan
                    existing.append(idea)
                    existing_idx[key] = len(existing) - 1
            # Keep last 200 ideas
            option_log.write_text(_json.dumps(existing[-200:], indent=2, default=str))

        # ── Mark-to-market all ACTIVE option ideas with live option mids ──
        # The dashboard was previously using payoff-at-expiry (intrinsic only),
        # which massively overstates unrealized P&L on OTM shorts because it
        # ignores remaining time value (theta). We now query the live last
        # price of each idea's option_code(s) and persist the marks so the
        # web server can compute a real MTM.
        try:
            option_log = LOG_DIR / "option_ideas.json"
            if option_log.exists():
                all_ideas = json.loads(option_log.read_text())
                # Deduplicate to latest per (strategy, stock_code) — same rule
                # the dashboard uses so we don't waste API calls on stale ideas.
                seen = {}
                for it in all_ideas:
                    key = (it.get("strategy"), it.get("stock_code"))
                    seen[key] = it
                active = list(seen.values())

                # Build list of option contracts to quote
                contract_codes = set()
                for it in active:
                    if it.get("option_code"):
                        contract_codes.add(it["option_code"])
                    if it.get("long_option"):
                        contract_codes.add(it["long_option"])
                    if it.get("short_option"):
                        contract_codes.add(it["short_option"])

                if contract_codes:
                    quotes = get_option_quotes(ctx, sorted(contract_codes))
                    marks_path = LOG_DIR / "option_marks.json"
                    marks = {
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                        "quotes": {k: v.get("price") for k, v in quotes.items()},
                    }
                    marks_path.write_text(json.dumps(marks))
                    logger.info(f"Option marks refreshed: {len(quotes)}/{len(contract_codes)} contracts")
        except Exception as e:
            logger.debug(f"Option mark refresh failed: {e}")
    except Exception as e:
        logger.warning(f"Options scan error: {e}")

    # 4. RANK AND SELECT
    # Group by ticker, keep strongest per ticker
    best_per_ticker = {}
    for sig in signals:
        code = sig["code"]
        if code not in best_per_ticker or sig["score"] > best_per_ticker[code]["score"]:
            best_per_ticker[code] = sig

    ranked = sorted(best_per_ticker.values(), key=lambda s: s["score"], reverse=True)
    hk_raw = sum(1 for s in signals if s.get("code", "").startswith("HK."))
    us_raw = len(signals) - hk_raw
    logger.info(f"Signals: {len(signals)} raw ({us_raw} US / {hk_raw} HK) -> {len(ranked)} ranked")

    # 5. EXECUTE top signals (with sector concentration guard)
    SECTOR_MAP = {
        "US.NVDA": "Tech", "US.AAPL": "Tech", "US.MSFT": "Tech", "US.GOOGL": "Tech",
        "US.AMZN": "Tech", "US.META": "Tech", "US.AMD": "Tech", "US.AVGO": "Tech",
        "US.CRM": "Tech", "US.ARM": "Tech", "US.SMCI": "Tech", "US.MSTR": "Tech",
        "US.PLTR": "Tech", "US.XLK": "Tech", "US.QQQ": "Tech",
        "US.TSLA": "Consumer", "US.BA": "Industrial", "US.XOM": "Energy",
        "US.XLE": "Energy", "US.GLD": "Commodity", "US.TLT": "Bonds",
        "US.JPM": "Finance", "US.GS": "Finance", "US.UNH": "Healthcare",
        "US.XLF": "Finance", "US.COIN": "Crypto",
        "US.SPY": "Index", "US.IWM": "Index",
        "HK.00700": "Tech", "HK.09988": "Tech", "HK.01810": "Tech",
        "HK.00005": "Finance", "HK.00388": "Finance",
        "HK.02318": "Finance", "HK.00883": "Energy",
        "HK.02800": "Index", "HK.03033": "Index",
    }
    MAX_SECTOR_PCT = 0.50  # no more than 50% of open trades in one sector

    # Count current sector exposure
    sector_counts = {}
    for t in open_trades:
        sec = SECTOR_MAP.get(t.get("code", ""), "Other")
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    trades_placed = 0

    if can_trade and ranked:
        # No per-cycle cap — execute every qualifying signal until MAX_OPEN_TRADES,
        # gross exposure cap, or sector concentration limit stops us.
        for sig in ranked:
            if len(open_trades) + trades_placed >= MAX_OPEN_TRADES:
                break
            if exposure_pct >= effective_max_exposure:
                break

            # Sector concentration guard
            sig_sector = SECTOR_MAP.get(sig["code"], "Other")
            total_open = len(open_trades) + trades_placed
            if total_open > 0:
                sector_count = sector_counts.get(sig_sector, 0)
                if sector_count / max(total_open, 1) >= MAX_SECTOR_PCT:
                    logger.info(f"SKIP {sig['code']}: sector {sig_sector} at {sector_count}/{total_open} ({sector_count/total_open:.0%}) — limit {MAX_SECTOR_PCT:.0%}")
                    continue

            # Position sizing — uses working capital, not raw balance, so that
            # locked principal from prior tiers is not at risk.
            working_cap, _locked, tier = working_capital(balance)
            _, _, tier_pos_pct, tier_pos_usd, tier_risk_pct = tier
            size_budget = min(working_cap * tier_pos_pct, tier_pos_usd)
            size_budget *= sig.get("size_mult", 1.0)  # learning adjustment

            # VIX-based volatility scaling: reduce size when VIX is elevated
            vix_scale = max(0.4, min(1.0, 20.0 / vix_level)) if vix_level > 0 else 1.0
            size_budget *= vix_scale

            # Regime-based size multiplier
            size_budget *= regime_cfg.get("size_mult", 1.0)

            # v3: REMOVED STRATEGY_SIZE_MULT — equal capital per strategy

            if sig["entry"] <= 0:
                continue

            qty = int(size_budget / sig["entry"])
            if qty <= 0:
                continue

            # Risk-based sizing: cap per-trade risk at the tier's risk %
            if sig["stop"] > 0:
                risk_per_share = abs(sig["entry"] - sig["stop"])
                if risk_per_share > 0:
                    max_risk_qty = int(working_cap * tier_risk_pct / risk_per_share)
                    qty = min(qty, max_risk_qty)

            if qty <= 0:
                continue

            # Round to lot size for HK stocks
            qty = round_to_lot(qty, sig["code"])
            if qty <= 0:
                continue

            # Block HK shorts (paper trading doesn't support them)
            if sig["direction"] == "SHORT" and not sig["code"].startswith("US."):
                logger.debug(f"Skipping HK short on {sig['code']} — not supported")
                continue

            # === FUNDAMENTAL GATE ===
            # Technical signals only decide TIMING; fundamentals decide WHETHER
            # we can hold the name at all. Rejected here = bot never enters.
            try:
                from risk.fundamental_gate import should_enter
                fund_ok, fund_reason = should_enter(sig["code"], sig["direction"])
                if not fund_ok:
                    logger.info(
                        f"[fund-gate] REJECT {sig['code']} {sig['direction']}: "
                        f"{fund_reason} (technical signal was {sig['strategy']} "
                        f"score={sig.get('score',0):.2f})"
                    )
                    try:
                        from data.signal_log import log_event
                        log_event(
                            symbol=sig["code"], strategy=sig["strategy"],
                            direction=sig["direction"], stage="fundamental_gate",
                            verdict="REJECT", detail=fund_reason,
                            score=sig.get("score"),
                        )
                    except Exception:
                        pass
                    continue
                else:
                    logger.debug(f"[fund-gate] PASS {sig['code']}: {fund_reason}")
            except Exception as e:
                logger.warning(f"[fund-gate] failed for {sig['code']}, allowing: {e}")

            # Place order via correct market context
            side = TrdSide.BUY if sig["direction"] == "LONG" else TrdSide.SELL
            trade_ctx = get_trd_ctx_for_code(sig["code"], us_trd, hk_trd)
            time.sleep(2.5)  # respect 15 orders / 30s limit
            ret, data = trade_ctx.place_order(
                price=0, qty=qty, code=sig["code"],
                trd_side=side,
                order_type=OrderType.MARKET,
                trd_env=TrdEnv.SIMULATE,
                remark=f"auto:{sig['strategy']}",
            )

            if ret != RET_OK:
                logger.warning(f"Order failed {sig['code']}: {data}")
                continue

            order_id = str(data.iloc[0].get("order_id", ""))

            # Get ACTUAL fill price from live snapshot (not stale kline close)
            fill_snap = get_snapshot_data(ctx, [sig["code"]])
            actual_price = fill_snap.get(sig["code"], {}).get("price", sig["entry"])
            name = fill_snap.get(sig["code"], {}).get("name", "")

            # Also try to get fill price from deal list (more accurate)
            try:
                time.sleep(0.5)
                ret_deals, deals = trade_ctx.deal_list_query(
                    code=sig["code"], trd_env=TrdEnv.SIMULATE
                )
                if ret_deals == RET_OK and len(deals) > 0:
                    # Find deal matching our order
                    matching = deals[deals["order_id"] == order_id]
                    if len(matching) > 0:
                        actual_price = float(matching.iloc[-1]["price"])
            except Exception as e:
                logger.debug(f"Could not get deal price for {order_id}: {e}")

            # Recalculate stops/targets from actual fill price (not kline close)
            atr = sig.get("_atr", abs(actual_price * 0.02))
            if sig["direction"] == "LONG":
                actual_stop = round(actual_price - abs(actual_price - sig["stop"]) / max(sig["entry"], 1) * actual_price, 2) if sig["entry"] > 0 else round(actual_price * 0.97, 2)
                actual_target = round(actual_price + abs(sig["target"] - actual_price) / max(sig["entry"], 1) * actual_price, 2) if sig["entry"] > 0 else round(actual_price * 1.05, 2)
            else:
                actual_stop = round(actual_price + abs(sig["stop"] - actual_price) / max(sig["entry"], 1) * actual_price, 2) if sig["entry"] > 0 else round(actual_price * 1.03, 2)
                actual_target = round(actual_price - abs(actual_price - sig["target"]) / max(sig["entry"], 1) * actual_price, 2) if sig["entry"] > 0 else round(actual_price * 0.95, 2)

            logger.info(
                f"TRADE: {sig['direction']} {qty} x {sig['code']} @ ${actual_price:.2f} "
                f"(signal was ${sig['entry']:.2f}) "
                f"| stop=${actual_stop:.2f} target=${actual_target:.2f} "
                f"| {sig['strategy']} score={sig['score']}"
            )

            # Determine pod
            if sig["code"].startswith("HK."):
                pod = "Global Macro" if sig["code"] in ["HK.02800", "HK.03033", "HK.00883"] else "L/S Equity"
            elif sig["code"] in ETF_UNIVERSE:
                pod = "Global Macro"
            else:
                pod = "L/S Equity" if sig["strategy"] in ["ma_crossover", "mean_reversion", "breakout"] else "Event-Driven"

            append_trade({
                "timestamp": datetime.now().isoformat(),
                "cycle": cycle_num,
                "code": sig["code"],
                "name": name,
                "side": sig["direction"],
                "qty": qty,
                "entry_price": actual_price,
                "stop_loss": actual_stop,
                "take_profit": actual_target,
                "strategy": sig["strategy"],
                "signal_score": sig["score"],
                "order_id": order_id,
                "status": "OPEN",
                "pod": pod,
                "thesis": sig["thesis"],
            })

            trades_placed += 1
            sector_counts[sig_sector] = sector_counts.get(sig_sector, 0) + 1

            # Unified lifecycle log — fill event (paper)
            try:
                from data.signal_log import log_filled as _log_fill
                _log_fill(
                    symbol=sig["code"], strategy=sig["strategy"],
                    direction=sig["direction"],
                    qty=qty, price=actual_price,
                    stop=actual_stop, target=actual_target,
                    score=float(sig.get("score") or 0),
                )
            except Exception as _lfe:
                logger.debug(f"signal_log fill write failed: {_lfe}")

            # Stage 1 shadow mirror: run the same signal through the stage
            # controller's pre-trade gate and (if it passes) shadow-queue it.
            # This lets us see what Stage 1 would have done without changing
            # paper behavior at all.
            if _STAGE_ENABLED:
                try:
                    stage_ctl = _get_stage_controller()
                    open_live_count = len(stage_ctl.state.open_symbols)
                    ok, why = stage_ctl.precheck_order(
                        symbol=sig["code"],
                        side=sig["direction"],
                        qty=qty,
                        price=actual_price,
                        strategy=sig["strategy"],
                        regime=regime.upper() if isinstance(regime, str) else "BULL",
                        open_count=open_live_count,
                        signal_score=float(sig.get("score") or 0),
                    )
                    if ok:
                        _stage_queue_order(
                            stage_ctl, sig["code"], sig["direction"], qty,
                            actual_price, sig["strategy"],
                            thesis=sig.get("thesis", ""),
                            signal_score=float(sig.get("score") or 0),
                        )
                        stage_ctl.record_fill(sig["code"], qty * actual_price)
                        try:
                            from data.signal_log import log_queued as _log_q
                            _log_q(symbol=sig["code"], strategy=sig["strategy"],
                                   direction=sig["direction"], qty=qty,
                                   price=actual_price, thesis=sig.get("thesis",""),
                                   score=float(sig.get("score") or 0))
                        except Exception:
                            pass
                    else:
                        logger.info(f"[STAGE-DENY] {sig['code']} {sig['strategy']}: {why}")
                except Exception as e:
                    logger.error(f"[STAGE] shadow mirror failed: {e}")

            time.sleep(0.5)

    # 6. UPDATE DASHBOARD STATE
    _wc, _locked, _tier = working_capital(balance)
    _tier_idx = SCALING_TIERS.index(_tier)
    write_system_state({
        "mode": "PAPER",
        "connected": True,
        "kill_switch": False,
        "daily_pnl": round(daily_pnl, 2),
        "day_start_balance": round(_load_day_start_balance(balance), 2),
        "open_trade_count": len(open_trades) + trades_placed,
        "max_open_trades": MAX_OPEN_TRADES,
        "consecutive_losses": consecutive_losses,
        "last_cycle": cycle_num,
        "cycle_time": datetime.now().isoformat(),
        "signals_found": len(signals),
        "trades_placed": trades_placed,
        "balance": round(balance, 2),
        "gross_exposure_pct": round(exposure_pct * 100, 1),
        "scaling_tier": _tier_idx,
        "scaling_tier_label": f"T{_tier_idx}",
        "working_capital": round(_wc, 2),
        "locked_reserve": round(_locked, 2),
        "tier_pos_pct": _tier[2],
        "tier_pos_usd": _tier[3],
        "tier_risk_pct": _tier[4],
        "hk_open": session["hk_open"],
        "hk_time": session["hk_time"],
        "us_open": session["us_open"],
        "us_time": session["us_time"],
        "hk_minutes_to_close": session["hk_minutes_to_close"],
        "us_minutes_to_close": session["us_minutes_to_close"],
    })

    # Append to balance history for the portfolio value chart.
    try:
        bh_path = LOG_DIR / "balance_history.json"
        history = []
        if bh_path.exists():
            try:
                history = json.loads(bh_path.read_text())
            except Exception:
                history = []
        history.append({
            "t": datetime.now().isoformat(timespec="seconds"),
            "balance": round(balance, 2),
            "us_balance": round(us_total, 2),
            "hk_balance_hkd": round(hk_total_hkd, 2),
            "exposure_pct": round(exposure_pct * 100, 2),
            "daily_pnl": round(daily_pnl, 2),
            "open_trades": len(open_trades) + trades_placed,
        })
        # Keep last 2000 snapshots (~2.8 days at 2-min cycles)
        bh_path.write_text(json.dumps(history[-2000:], default=str))
    except Exception as e:
        logger.debug(f"balance history write failed: {e}")

    # Also write to the dashboard trades.csv for web display
    dashboard_csv = LOG_DIR / "trades.csv"
    journal = read_journal()
    with open(dashboard_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "code", "side", "quantity", "entry_price",
                         "exit_price", "pnl", "strategy", "order_id", "status"])
        for t in journal:
            writer.writerow([
                t.get("timestamp", ""), t.get("code", ""), t.get("side", ""),
                t.get("qty", ""), t.get("entry_price", ""),
                t.get("exit_price", ""), t.get("pnl", ""),
                t.get("strategy", ""), t.get("order_id", ""), t.get("status", ""),
            ])

    logger.info(f"Cycle {cycle_num} complete: {trades_placed} trades placed, "
                f"{len(open_trades)} open, daily P&L=${daily_pnl:+,.2f}")

    return daily_pnl, consecutive_losses, last_loss_time


def main():
    SysConfig.set_all_thread_daemon(True)
    init_journal()

    logger.info("=" * 60)
    logger.info("  AUTOMATED TRADING SYSTEM — STARTING")
    logger.info("  Mode: PAPER | Dashboard: http://localhost:8877")
    logger.info("=" * 60)

    # Start dashboard
    start_dashboard(background=True)

    # Connect — one quote context, two trade contexts (US + HK)
    ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    us_trd = OpenUSTradeContext(host=OPEND_HOST, port=OPEND_PORT)
    hk_trd = OpenSecTradeContext(host=OPEND_HOST, port=OPEND_PORT)

    ret, _ = ctx.get_global_state()
    if ret != RET_OK:
        logger.critical("Cannot connect to OpenD")
        return

    logger.info("Connected to OpenD")

    # Check accounts
    ret, acct = us_trd.accinfo_query(trd_env=TrdEnv.SIMULATE)
    if ret == RET_OK:
        logger.info(f"US paper account: ${float(acct.iloc[0]['total_assets']):,.2f}")
    ret, acct = hk_trd.accinfo_query(trd_env=TrdEnv.SIMULATE)
    if ret == RET_OK:
        logger.info(f"HK paper account: ${float(acct.iloc[0]['total_assets']):,.2f}")

    cycle_num = 0
    daily_pnl = 0.0
    consecutive_losses = 0
    last_loss_time = None
    cycle_interval = 120  # seconds between cycles

    try:
        while not _shutdown:
            cycle_num += 1

            try:
                daily_pnl, consecutive_losses, last_loss_time = run_cycle(
                    ctx, us_trd, hk_trd, cycle_num,
                    daily_pnl, consecutive_losses, last_loss_time,
                )
            except Exception as e:
                logger.error(f"Cycle {cycle_num} error: {e}", exc_info=True)

            # Print learning stats
            stats = load_strategy_stats()
            if stats:
                logger.info("Strategy performance:")
                for name, s in stats.items():
                    logger.info(f"  {name}: {s['trades']} trades, "
                                f"{s['win_rate']}% win, "
                                f"avg P&L=${s['avg_pnl']:.2f}, "
                                f"size_mult={s['size_multiplier']}")

            # Wait for next cycle
            logger.info(f"Next cycle in {cycle_interval}s... (Ctrl+C to stop)")
            for _ in range(cycle_interval):
                if _shutdown:
                    break
                time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        ctx.close()
        us_trd.close()
        hk_trd.close()

        # Final summary
        journal = read_journal()
        closed = [t for t in journal if t.get("status") == "CLOSED"]
        open_t = [t for t in journal if t.get("status") == "OPEN"]

        logger.info("=" * 60)
        logger.info("  SESSION SUMMARY")
        logger.info(f"  Cycles: {cycle_num}")
        logger.info(f"  Total trades: {len(journal)}")
        logger.info(f"  Open: {len(open_t)}")
        logger.info(f"  Closed: {len(closed)}")
        if closed:
            total_pnl = sum(float(t.get("pnl", 0)) for t in closed)
            wins = sum(1 for t in closed if float(t.get("pnl", 0)) > 0)
            logger.info(f"  Total P&L: ${total_pnl:+,.2f}")
            logger.info(f"  Win rate: {wins}/{len(closed)} ({wins/len(closed)*100:.0f}%)")
        logger.info("=" * 60)
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
