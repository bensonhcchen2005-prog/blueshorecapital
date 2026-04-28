"""
Point-in-Time (PIT) Fundamental Scoring for Core Sleeve Backtest — v2.

Revised factor model:
  1. Valuation (25%)   — trailing multiples vs industry peers, then sector fallback
  2. Quality (25%)     — profitability, capital efficiency, margin trajectory, earnings quality
  3. Revision (20%)    — earnings surprise direction, margin change, estimate-revision proxy
  4. Trend (15%)       — SMA alignment + relative strength (confirmation only, not primary)
  5. Stability (15%)   — downside risk budget: vol, drawdown, beta

Key changes from v1:
  - Peer comparison now uses INDUSTRY groups (5-8 peers), falling back to SECTOR (10-15)
  - Added Revision factor: captures "things are getting better/worse" from lagged financials
  - Expanded Quality: operating margin, net margin, ROE, ROIC proxy, margin trajectory
  - Reduced Momentum/Stability weights from 45% combined to 30%
  - Trend is confirmation only — can't carry a stock without fundamental support
  - No DCF in PIT scoring (requires forward estimates = look-ahead)
  - DCF lives only in the live research layer (fundamental/valuation.py)

Two-layer architecture:
  PIT BACKTEST LAYER (this file)     LIVE RESEARCH LAYER (quality.py + valuation.py)
  ├── Lagged annual financials        ├── yfinance .info (current TTM)
  ├── Historical prices               ├── Forward estimates (earningsGrowth)
  ├── Trailing multiples              ├── Conservative DCF
  ├── Price-derived signals           ├── Real-time peer comps
  └── No look-ahead allowed           └── Live sleeve classification
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("fundamental.pit_scoring")

CACHE_DIR = Path("data_cache")
CACHE_DIR.mkdir(exist_ok=True)

# Publication lag: annual financials aren't available until ~60 days after
# fiscal year end. Quarterly financials lag ~45 days.
ANNUAL_PUB_LAG_DAYS = 60
QUARTERLY_PUB_LAG_DAYS = 45

# ── Factor weights (v2) ─────────────────────────────────────
# Fundamentals-heavy: 70% fundamental (valuation + quality + revision)
# Price-derived:      30% (trend + stability)
WEIGHTS = {
    "valuation": 0.25,
    "quality":   0.25,
    "revision":  0.20,
    "trend":     0.15,
    "stability": 0.15,
}

# ── Industry & Sector peer groups ────────────────────────────
# Industry = narrow (same business model). Sector = broad fallback.
# Peer comparison uses industry first. If <3 industry peers have data,
# falls back to sector.

INDUSTRY_PEERS = {
    # Tech — split by business model
    "cloud_platform":   ["MSFT", "GOOGL", "AMZN", "CRM", "ADBE"],
    "consumer_tech":    ["AAPL", "META", "NFLX", "DIS"],
    "semis":            ["NVDA", "AMD", "AVGO", "INTC", "TXN"],
    # Financials — split by type
    "investment_bank":  ["JPM", "GS", "MS"],
    "commercial_bank":  ["BAC", "WFC"],
    # Healthcare
    "managed_care":     ["UNH"],
    "pharma":           ["JNJ", "PFE", "ABBV", "MRK"],
    # Consumer
    "staples":          ["PG", "KO", "PEP"],
    "retail":           ["WMT", "COST"],
    # Energy
    "oil_major":        ["XOM", "CVX"],
    # Industrial
    "industrial":       ["CAT", "HON", "GE", "BA"],
    # Payments
    "payments":         ["V", "MA"],
}

SECTOR_GROUPS = {
    "tech":        ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "CRM", "ADBE", "NFLX"],
    "semis":       ["NVDA", "AMD", "AVGO", "INTC", "TXN"],
    "financials":  ["JPM", "GS", "MS", "BAC", "WFC", "V", "MA"],
    "healthcare":  ["UNH", "JNJ", "PFE", "ABBV", "MRK"],
    "consumer":    ["PG", "KO", "PEP", "WMT", "COST", "DIS"],
    "energy":      ["XOM", "CVX"],
    "industrial":  ["CAT", "HON", "GE", "BA"],
}

# Broader universe — all S&P 500 members since 2018 (no survivorship bias)
CORE_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META",
    # Semis
    "NVDA", "AMD", "AVGO", "INTC", "TXN",
    # Financials
    "JPM", "GS", "BAC", "WFC", "MS",
    # Healthcare
    "UNH", "JNJ", "PFE", "ABBV", "MRK",
    # Consumer staples / discretionary
    "PG", "KO", "PEP", "WMT", "COST",
    # Energy
    "XOM", "CVX",
    # Industrial
    "CAT", "HON", "GE", "BA",
    # Other large-cap
    "CRM", "NFLX", "DIS", "ADBE", "V", "MA",
]

# Maximum name changes per quarterly rebalance (limits turnover)
MAX_TURNOVER_PER_REBALANCE = 3

# Sector diversification: max N names from one sector in the basket
MAX_SECTOR_IN_BASKET = 3


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


@dataclass
class PITScore:
    """Point-in-time score for one ticker at one date."""
    ticker: str
    as_of: str
    valuation: float = 0.5
    quality: float = 0.5
    revision: float = 0.5
    trend: float = 0.5
    stability: float = 0.5
    composite: float = 0.5
    details: Dict = field(default_factory=dict)


@dataclass
class PITFinancials:
    """Annual financials for a single fiscal year."""
    ticker: str
    fiscal_year_end: str  # e.g. "2023-12-31"
    available_after: str  # fiscal_year_end + pub lag
    revenue: float = 0.0
    gross_profit: float = 0.0
    operating_income: float = 0.0
    net_income: float = 0.0
    total_equity: float = 0.0
    total_assets: float = 0.0
    shares_outstanding: float = 0.0
    # Derived
    gross_margin: float = 0.0
    operating_margin: float = 0.0
    net_margin: float = 0.0
    roe: float = 0.0
    roa: float = 0.0
    eps: float = 0.0
    revenue_per_share: float = 0.0


def _get_industry_peers(ticker: str) -> List[str]:
    """Get narrow industry peers for a ticker."""
    for group, members in INDUSTRY_PEERS.items():
        if ticker in members:
            return [m for m in members if m != ticker]
    return []


def _get_sector_peers(ticker: str) -> List[str]:
    """Get broad sector peers as fallback."""
    for sector, members in SECTOR_GROUPS.items():
        if ticker in members:
            return [m for m in members if m != ticker]
    return []


def _get_sector(ticker: str) -> str:
    """Get sector name for a ticker."""
    for sector, members in SECTOR_GROUPS.items():
        if ticker in members:
            return sector
    return "other"


class PITScorer:
    """Scores stocks at historical dates using only point-in-time data.

    v2: 5-factor model (valuation, quality, revision, trend, stability)
    with industry-first peer comparison and revision/expectation-change factor.
    """

    def __init__(self, universe: List[str] = None,
                 start: str = "2018-01-01", end: str = "2026-04-01"):
        self.universe = universe or CORE_UNIVERSE
        self.start = start
        self.end = end
        # Caches
        self._price_cache: Dict[str, pd.DataFrame] = {}
        self._financials_cache: Dict[str, List[PITFinancials]] = {}
        self._spy_cache: Optional[pd.DataFrame] = None
        self._loaded = False

    def load_data(self):
        """Fetch and cache all price history and financials."""
        if self._loaded:
            return
        import yfinance as yf
        import time

        prefetch_start = (pd.Timestamp(self.start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        all_tickers = list(set(self.universe + ["SPY"]))

        for tk in all_tickers:
            cache_path = CACHE_DIR / f"pit_{tk}_{prefetch_start}_{self.end}.parquet"
            if cache_path.exists():
                try:
                    self._price_cache[tk] = pd.read_parquet(cache_path)
                    continue
                except Exception:
                    pass

            try:
                df = yf.download(tk, start=prefetch_start, end=self.end,
                                 interval="1d", progress=False, auto_adjust=True)
                if df is not None and len(df) > 0:
                    # Handle yfinance MultiIndex columns (e.g. ('Close','AAPL'))
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower()
                                      for c in df.columns]
                    else:
                        df.columns = [str(c).lower() for c in df.columns]
                    df.index = pd.to_datetime(df.index)
                    if hasattr(df.index, 'tz') and df.index.tz is not None:
                        df.index = df.index.tz_localize(None)
                    self._price_cache[tk] = df
                    try:
                        df.to_parquet(cache_path)
                    except Exception:
                        pass
                time.sleep(0.15)
            except Exception as e:
                logger.debug(f"Failed to fetch price for {tk}: {e}")

        self._spy_cache = self._price_cache.get("SPY")

        for tk in self.universe:
            cache_path = CACHE_DIR / f"pit_fin2_{tk}.json"
            if cache_path.exists():
                try:
                    import json
                    data = json.loads(cache_path.read_text())
                    self._financials_cache[tk] = [PITFinancials(**d) for d in data]
                    continue
                except Exception:
                    pass

            try:
                t = yf.Ticker(tk)
                inc = t.financials
                bs = t.balance_sheet

                if inc is None or inc.empty:
                    time.sleep(0.3)
                    continue

                years = []
                info = t.info or {}
                shares = info.get("sharesOutstanding") or 1

                for col in inc.columns:
                    fye = pd.Timestamp(col)
                    avail = (fye + pd.Timedelta(days=ANNUAL_PUB_LAG_DAYS)).strftime("%Y-%m-%d")

                    rev = _safe_get(inc, col, ["Total Revenue", "Revenue"])
                    gp = _safe_get(inc, col, ["Gross Profit"])
                    oi = _safe_get(inc, col, ["Operating Income", "EBIT"])
                    ni = _safe_get(inc, col, ["Net Income", "Net Income Common Stockholders"])

                    eq = 0.0
                    ta = 0.0
                    if bs is not None and col in bs.columns:
                        eq = _safe_get(bs, col, ["Total Stockholder Equity",
                                                  "Stockholders Equity",
                                                  "Total Equity Gross Minority Interest",
                                                  "Common Stock Equity"])
                        ta = _safe_get(bs, col, ["Total Assets"])

                    gm = gp / rev if rev > 0 else 0
                    om = oi / rev if rev > 0 else 0
                    nm = ni / rev if rev > 0 else 0
                    roe = ni / eq if eq > 0 else 0
                    roa = ni / ta if ta > 0 else 0
                    eps = ni / shares if shares > 0 else 0
                    rps = rev / shares if shares > 0 else 0

                    years.append(PITFinancials(
                        ticker=tk,
                        fiscal_year_end=fye.strftime("%Y-%m-%d"),
                        available_after=avail,
                        revenue=rev, gross_profit=gp, operating_income=oi,
                        net_income=ni, total_equity=eq, total_assets=ta,
                        shares_outstanding=shares,
                        gross_margin=round(gm, 4), operating_margin=round(om, 4),
                        net_margin=round(nm, 4), roe=round(roe, 4), roa=round(roa, 4),
                        eps=round(eps, 4), revenue_per_share=round(rps, 4),
                    ))

                self._financials_cache[tk] = sorted(years, key=lambda x: x.fiscal_year_end)

                import json
                cache_path.write_text(json.dumps(
                    [vars(y) for y in self._financials_cache[tk]], indent=2
                ))
                time.sleep(0.3)

            except Exception as e:
                logger.debug(f"Failed to fetch financials for {tk}: {e}")

        self._loaded = True
        logger.info(f"PIT data loaded: {len(self._price_cache)} price series, "
                     f"{len(self._financials_cache)} financials")

    # ── Data access helpers ──────────────────────────────────

    def _get_latest_financials(self, ticker: str, as_of: str) -> Optional[PITFinancials]:
        """Most recent annual financials available as of date (respects pub lag)."""
        years = self._financials_cache.get(ticker, [])
        available = [y for y in years if y.available_after <= as_of]
        return available[-1] if available else None

    def _get_prev_financials(self, ticker: str, as_of: str) -> Optional[PITFinancials]:
        """Second most recent annual financials (for YoY comparisons)."""
        years = self._financials_cache.get(ticker, [])
        available = [y for y in years if y.available_after <= as_of]
        return available[-2] if len(available) >= 2 else None

    def _get_two_prev_financials(self, ticker: str, as_of: str) -> Optional[PITFinancials]:
        """Third most recent (for 2-year trend analysis)."""
        years = self._financials_cache.get(ticker, [])
        available = [y for y in years if y.available_after <= as_of]
        return available[-3] if len(available) >= 3 else None

    def _price_at(self, ticker: str, as_of: str) -> Optional[float]:
        """Closing price on or just before as_of."""
        df = self._price_cache.get(ticker)
        if df is None or df.empty:
            return None
        mask = df.index <= pd.Timestamp(as_of)
        return float(df.loc[mask, "close"].iloc[-1]) if mask.any() else None

    def _price_series(self, ticker: str, as_of: str, lookback_days: int = 252
                      ) -> Optional[pd.Series]:
        """Closing price series ending at as_of."""
        df = self._price_cache.get(ticker)
        if df is None or df.empty:
            return None
        end = pd.Timestamp(as_of)
        start = end - pd.Timedelta(days=lookback_days + 30)
        mask = (df.index >= start) & (df.index <= end)
        series = df.loc[mask, "close"]
        return series if len(series) > 20 else None

    def _get_peer_multiples(self, ticker: str, as_of: str
                            ) -> Tuple[List[float], List[float], str]:
        """Get P/E and P/S multiples for peers of ticker.

        Uses industry peers first. Falls back to sector if <3 industry peers
        have valid data.

        Returns (peer_pes, peer_pss, peer_level) where peer_level is
        "industry" or "sector".
        """
        # Try industry first
        peers = _get_industry_peers(ticker)
        pes, pss = self._collect_peer_multiples(peers, as_of)
        if len(pes) >= 2:
            return pes, pss, "industry"

        # Fall back to sector
        peers = _get_sector_peers(ticker)
        pes, pss = self._collect_peer_multiples(peers, as_of)
        return pes, pss, "sector"

    def _collect_peer_multiples(self, peers: List[str], as_of: str
                                ) -> Tuple[List[float], List[float]]:
        """Compute trailing P/E and P/S for a list of peers."""
        pes, pss = [], []
        for p in peers:
            fin = self._get_latest_financials(p, as_of)
            price = self._price_at(p, as_of)
            if fin is None or price is None or price <= 0:
                continue
            if fin.eps > 0:
                pes.append(price / fin.eps)
            if fin.revenue_per_share > 0:
                pss.append(price / fin.revenue_per_share)
        return pes, pss

    # ── Factor 1: Valuation (25%) ────────────────────────────

    def _score_valuation(self, ticker: str, as_of: str,
                         universe_pes: List[float],
                         universe_pss: List[float]) -> Tuple[float, Dict]:
        """Valuation: trailing multiples vs industry/sector peers + universe."""
        fin = self._get_latest_financials(ticker, as_of)
        price = self._price_at(ticker, as_of)
        if fin is None or price is None or price <= 0:
            return 0.5, {"note": "no data"}

        details = {}

        # Own trailing multiples
        pe = price / fin.eps if fin.eps > 0 else None
        ps = price / fin.revenue_per_share if fin.revenue_per_share > 0 else None

        # ── Peer-relative valuation (industry first, sector fallback)
        peer_pes, peer_pss, peer_level = self._get_peer_multiples(ticker, as_of)
        details["peer_level"] = peer_level

        peer_pe_score = 0.5
        if pe is not None and peer_pes:
            median = float(np.median(peer_pes))
            ratio = pe / median if median > 0 else 1.0
            peer_pe_score = _clamp(1.2 - ratio * 0.7)
            details["pe"] = round(pe, 1)
            details["peer_pe_median"] = round(median, 1)
            details["pe_vs_peers"] = round(ratio, 2)

        peer_ps_score = 0.5
        if ps is not None and peer_pss:
            median = float(np.median(peer_pss))
            ratio = ps / median if median > 0 else 1.0
            peer_ps_score = _clamp(1.2 - ratio * 0.7)
            details["ps"] = round(ps, 1)
            details["peer_ps_median"] = round(median, 1)

        # ── Universe-relative (broader check)
        univ_pe_score = 0.5
        if pe is not None and universe_pes:
            median = float(np.median(universe_pes))
            ratio = pe / median if median > 0 else 1.0
            univ_pe_score = _clamp(1.2 - ratio * 0.7)

        # ── 52-week range position
        series = self._price_series(ticker, as_of, lookback_days=260)
        range_score = 0.5
        if series is not None and len(series) > 50:
            hi52, lo52 = series.max(), series.min()
            rng = hi52 - lo52
            if rng > 0:
                position = (price - lo52) / rng
                range_score = _clamp(1.0 - position)
                details["52w_position"] = round(position, 2)

        # Weighted: peer-relative is primary, universe is sanity check
        score = (0.35 * peer_pe_score + 0.25 * peer_ps_score
                 + 0.20 * univ_pe_score + 0.20 * range_score)
        return round(_clamp(score), 3), details

    # ── Factor 2: Quality (25%) ──────────────────────────────

    def _score_quality(self, ticker: str, as_of: str) -> Tuple[float, Dict]:
        """Quality: profitability, capital efficiency, earnings quality.

        Expanded beyond just gross margin / ROE / revenue growth:
          - Gross margin (pricing power)
          - Operating margin (cost control + scale)
          - Net margin (bottom-line conversion)
          - ROE (capital efficiency)
          - ROA (asset-light indicator)
          - Earnings quality: net income / gross profit ratio (how much drops through)
        """
        fin = self._get_latest_financials(ticker, as_of)
        if fin is None:
            return 0.5, {"note": "no financials"}

        details = {}

        # Gross margin: 0% = 0, 50%+ = 1
        gm_score = _clamp(fin.gross_margin / 0.50)
        details["gross_margin"] = round(fin.gross_margin, 3)

        # Operating margin: 0% = 0, 30%+ = 1
        om_score = _clamp(fin.operating_margin / 0.30)
        details["operating_margin"] = round(fin.operating_margin, 3)

        # Net margin: 0% = 0, 20%+ = 1
        nm_score = _clamp(fin.net_margin / 0.20)
        details["net_margin"] = round(fin.net_margin, 3)

        # ROE: 0% = 0, 25%+ = 1
        roe_score = _clamp(fin.roe / 0.25)
        details["roe"] = round(fin.roe, 3)

        # ROA: 0% = 0, 10%+ = 1  (asset-light businesses score higher)
        roa_score = _clamp(fin.roa / 0.10)
        details["roa"] = round(fin.roa, 3)

        # Earnings quality: NI / GP — how much gross profit survives to net?
        # High = lean operations. Low = bloated SG&A or interest expense.
        eq_ratio = fin.net_income / fin.gross_profit if fin.gross_profit > 0 else 0
        eq_score = _clamp(eq_ratio / 0.40)  # 40%+ pass-through = 1.0
        details["earnings_quality_ratio"] = round(eq_ratio, 3)

        # Weighted composite — profitability breadth matters
        score = (0.20 * gm_score + 0.20 * om_score + 0.15 * nm_score
                 + 0.20 * roe_score + 0.10 * roa_score + 0.15 * eq_score)
        return round(_clamp(score), 3), details

    # ── Factor 3: Revision / Expectation Change (20%) ────────

    def _score_revision(self, ticker: str, as_of: str) -> Tuple[float, Dict]:
        """Revision: are fundamentals getting better or worse?

        Since we can't get analyst estimate revisions historically, we use
        a point-in-time proxy: changes in reported financials between the
        two most recent annual reports. This captures the same signal —
        "earnings trajectory is improving" — but with a quarterly lag.

        Sub-factors:
          - EPS growth (YoY annual) — did earnings grow?
          - Operating margin change — are margins expanding?
          - Revenue acceleration — is growth accelerating vs prior year?
          - Earnings surprise proxy: actual EPS vs trailing trend
        """
        fin = self._get_latest_financials(ticker, as_of)
        prev = self._get_prev_financials(ticker, as_of)
        prev2 = self._get_two_prev_financials(ticker, as_of)

        if fin is None or prev is None:
            return 0.5, {"note": "need 2+ years of financials"}

        details = {}

        # EPS growth (YoY)
        eps_growth_score = 0.5
        if prev.eps != 0:
            eps_g = (fin.eps - prev.eps) / abs(prev.eps)
            # Moderate positive growth (5-30%) scores highest
            if eps_g < -0.20:
                eps_growth_score = 0.1
            elif eps_g < 0:
                eps_growth_score = _clamp(0.3 + eps_g)
            elif eps_g <= 0.30:
                eps_growth_score = _clamp(0.5 + eps_g * 1.5)
            else:
                eps_growth_score = _clamp(0.95 - (eps_g - 0.30) * 0.5)
            details["eps_growth_yoy"] = round(eps_g, 3)

        # Operating margin change (expansion = positive signal)
        margin_change_score = 0.5
        margin_delta = fin.operating_margin - prev.operating_margin
        # +5pp or more = great (1.0), -5pp or worse = bad (0.0)
        margin_change_score = _clamp(0.5 + margin_delta / 0.10)
        details["op_margin_delta"] = round(margin_delta, 4)

        # Revenue acceleration: current growth vs prior growth
        accel_score = 0.5
        if prev2 is not None and prev2.revenue > 0 and prev.revenue > 0:
            g_current = (fin.revenue - prev.revenue) / prev.revenue
            g_prior = (prev.revenue - prev2.revenue) / prev2.revenue
            accel = g_current - g_prior
            # Positive acceleration = things getting better
            accel_score = _clamp(0.5 + accel / 0.20)
            details["rev_growth_current"] = round(g_current, 3)
            details["rev_growth_prior"] = round(g_prior, 3)
            details["rev_acceleration"] = round(accel, 3)
        elif prev.revenue > 0:
            # Only one prior year — just use current growth
            g_current = (fin.revenue - prev.revenue) / prev.revenue
            accel_score = _clamp(0.5 + g_current / 0.30)
            details["rev_growth_current"] = round(g_current, 3)

        # Earnings surprise proxy: actual EPS vs simple trend extrapolation
        # If we have 3 years, the "expected" EPS is prev + (prev - prev2)
        surprise_score = 0.5
        if prev2 is not None and prev.eps != 0:
            trend_eps = prev.eps + (prev.eps - prev2.eps)
            if trend_eps > 0:
                surprise = (fin.eps - trend_eps) / abs(trend_eps)
                surprise_score = _clamp(0.5 + surprise / 0.30)
                details["eps_surprise_proxy"] = round(surprise, 3)

        score = (0.30 * eps_growth_score + 0.25 * margin_change_score
                 + 0.25 * accel_score + 0.20 * surprise_score)
        return round(_clamp(score), 3), details

    # ── Factor 4: Trend (15%) ────────────────────────────────

    def _score_trend(self, ticker: str, as_of: str) -> Tuple[float, Dict]:
        """Trend: SMA alignment + relative strength. Confirmation only.

        This factor cannot carry a stock — it just confirms or denies
        fundamental attractiveness. A stock scoring 0.8 on fundamentals
        but 0.3 on trend gets a small penalty, not a veto.
        """
        series = self._price_series(ticker, as_of, lookback_days=280)
        if series is None or len(series) < 60:
            return 0.5, {"note": "insufficient price history"}

        details = {}
        price = float(series.iloc[-1])

        # SMA alignment: SMA50 > SMA200 = uptrend
        sma50 = float(series.iloc[-50:].mean()) if len(series) >= 50 else price
        sma200 = float(series.iloc[-200:].mean()) if len(series) >= 200 else sma50
        sma_aligned = sma50 > sma200
        sma_score = 0.8 if sma_aligned else 0.3
        details["sma50_gt_sma200"] = sma_aligned

        # Price vs SMA200: how far above/below?
        sma_distance = (price / sma200 - 1) if sma200 > 0 else 0
        # +10% above = strong trend (0.9), -10% below = weak (0.2)
        distance_score = _clamp(0.5 + sma_distance / 0.20)
        details["price_vs_sma200"] = round(sma_distance, 3)

        # Relative strength vs SPY (6-month)
        rs_score = 0.5
        spy_series = self._price_series("SPY", as_of, lookback_days=280)
        if spy_series is not None and len(spy_series) >= 126 and len(series) >= 126:
            spy_price = float(spy_series.iloc[-1])
            spy_6m = float(spy_series.iloc[-126])
            tk_6m = float(series.iloc[-126])
            tk_ret = (price / tk_6m - 1) if tk_6m > 0 else 0
            spy_ret = (spy_price / spy_6m - 1) if spy_6m > 0 else 0
            excess = tk_ret - spy_ret
            rs_score = _clamp(0.5 + excess / 0.30)
            details["relative_strength_6m"] = round(excess, 3)

        score = 0.40 * sma_score + 0.30 * distance_score + 0.30 * rs_score
        return round(_clamp(score), 3), details

    # ── Factor 5: Stability (15%) ────────────────────────────

    def _score_stability(self, ticker: str, as_of: str) -> Tuple[float, Dict]:
        """Stability: downside risk budget. Low vol + low drawdown preferred."""
        series = self._price_series(ticker, as_of, lookback_days=280)
        if series is None or len(series) < 60:
            return 0.5, {"note": "insufficient history"}

        details = {}
        rets = series.pct_change().dropna()

        # 60-day annualized vol: <20% = 1.0, >45% = 0.0
        vol_60 = float(rets.iloc[-60:].std() * np.sqrt(252)) if len(rets) >= 60 else 0.25
        vol_score = _clamp(1.0 - (vol_60 - 0.20) / 0.25)
        details["volatility_60d_ann"] = round(vol_60, 3)

        # Max drawdown (trailing 12mo): >-10% = 1.0, <-35% = 0.0
        eq = (1 + rets).cumprod()
        max_dd = float((eq / eq.cummax() - 1).min())
        dd_score = _clamp(1.0 + (max_dd + 0.10) / 0.25)
        details["max_drawdown_12m"] = round(max_dd, 3)

        # Downside deviation (Sortino-style): penalize only negative returns
        neg_rets = rets[rets < 0]
        downside_dev = float(neg_rets.std() * np.sqrt(252)) if len(neg_rets) > 10 else vol_60
        dd_dev_score = _clamp(1.0 - (downside_dev - 0.10) / 0.25)
        details["downside_deviation_ann"] = round(downside_dev, 3)

        score = 0.35 * vol_score + 0.35 * dd_score + 0.30 * dd_dev_score
        return round(_clamp(score), 3), details

    # ── Composite scoring ────────────────────────────────────

    def score_ticker(self, ticker: str, as_of: str,
                     universe_pes: List[float] = None,
                     universe_pss: List[float] = None) -> PITScore:
        """Compute composite 5-factor PIT score for one ticker.

        Graceful degradation: if financials are missing, score on available
        factors only and redistribute weights proportionally.
        """
        fin = self._get_latest_financials(ticker, as_of)
        prev = self._get_prev_financials(ticker, as_of)

        val, val_d = self._score_valuation(ticker, as_of,
                                            universe_pes or [], universe_pss or [])
        qual, qual_d = self._score_quality(ticker, as_of)
        rev, rev_d = self._score_revision(ticker, as_of)
        trend, trend_d = self._score_trend(ticker, as_of)
        stab, stab_d = self._score_stability(ticker, as_of)

        # Adaptive weighting: redistribute weight of unavailable factors
        w = dict(WEIGHTS)  # copy
        if fin is None:
            # No financials at all — only price-derived factors
            # Redistribute fundamental weights to trend+stability
            fundamental_w = w["valuation"] + w["quality"] + w["revision"]
            w["valuation"] = 0.0
            w["quality"] = 0.0
            w["revision"] = 0.0
            w["trend"] += fundamental_w * 0.5
            w["stability"] += fundamental_w * 0.5
        elif prev is None:
            # Only 1 year of financials — revision factor is unreliable
            # Redistribute revision weight to valuation + quality
            rev_w = w["revision"]
            w["revision"] = 0.0
            w["valuation"] += rev_w * 0.5
            w["quality"] += rev_w * 0.5

        total_w = sum(w.values())
        composite = ((w["valuation"] * val + w["quality"] * qual
                      + w["revision"] * rev + w["trend"] * trend
                      + w["stability"] * stab) / total_w if total_w > 0 else 0.5)

        return PITScore(
            ticker=ticker,
            as_of=as_of,
            valuation=val,
            quality=qual,
            revision=rev,
            trend=trend,
            stability=stab,
            composite=round(composite, 3),
            details={"valuation": val_d, "quality": qual_d, "revision": rev_d,
                     "trend": trend_d, "stability": stab_d},
        )

    def score_universe(self, as_of: str) -> Dict[str, PITScore]:
        """Score all tickers at a given date.

        Two-pass: first gather universe multiples, then score each ticker
        with peer context.
        """
        self.load_data()

        # First pass: universe-wide trailing multiples
        universe_pes, universe_pss = [], []
        for tk in self.universe:
            fin = self._get_latest_financials(tk, as_of)
            price = self._price_at(tk, as_of)
            if fin is None or price is None or price <= 0:
                continue
            if fin.eps > 0:
                universe_pes.append(price / fin.eps)
            if fin.revenue_per_share > 0:
                universe_pss.append(price / fin.revenue_per_share)

        # Second pass: score each ticker
        scores = {}
        for tk in self.universe:
            scores[tk] = self.score_ticker(tk, as_of, universe_pes, universe_pss)

        return scores

    def select_core_basket(self, as_of: str, n: int = 8,
                           prev_basket: List[str] = None) -> List[str]:
        """Select top N stocks with turnover cap and sector diversification.

        Constraints:
          - Max MAX_TURNOVER_PER_REBALANCE name changes vs previous basket
          - Max MAX_SECTOR_IN_BASKET names from one sector
          - Must have valid price data
        """
        scores = self.score_universe(as_of)

        eligible = [(tk, s) for tk, s in scores.items()
                    if s.composite > 0.25 and self._price_at(tk, as_of) is not None]
        eligible.sort(key=lambda x: x[1].composite, reverse=True)

        if prev_basket is None:
            return self._apply_sector_cap(eligible, n)

        # Turnover-constrained selection
        prev_set = set(prev_basket)
        new_basket = []
        changes = 0

        # Keep names from prev_basket that are still in top 2*N
        top_2n = set(tk for tk, _ in eligible[:2 * n])
        for tk in prev_basket:
            if tk in top_2n:
                new_basket.append(tk)

        # Add new names up to turnover limit
        for tk, _ in eligible:
            if len(new_basket) >= n:
                break
            if tk not in new_basket:
                if tk not in prev_set:
                    changes += 1
                    if changes > MAX_TURNOVER_PER_REBALANCE:
                        continue
                # Sector cap check
                sector = _get_sector(tk)
                sector_count = sum(1 for t in new_basket if _get_sector(t) == sector)
                if sector_count >= MAX_SECTOR_IN_BASKET:
                    continue
                new_basket.append(tk)

        return new_basket[:n]

    def _apply_sector_cap(self, eligible: List[Tuple[str, PITScore]],
                          n: int) -> List[str]:
        """Select top N with sector diversification cap."""
        basket = []
        sector_counts: Dict[str, int] = {}
        for tk, _ in eligible:
            if len(basket) >= n:
                break
            sector = _get_sector(tk)
            if sector_counts.get(sector, 0) >= MAX_SECTOR_IN_BASKET:
                continue
            basket.append(tk)
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        return basket

    def earliest_usable_date(self, min_scored: int = 8) -> str:
        """Find earliest date where at least min_scored tickers have financials.

        This prevents starting a PIT backtest before any fundamental data
        is available from yfinance.
        """
        self.load_data()
        # Collect all available_after dates
        all_avail = []
        for tk, years in self._financials_cache.items():
            if years:
                all_avail.append(years[0].available_after)
        all_avail.sort()
        if len(all_avail) >= min_scored:
            return all_avail[min_scored - 1]
        elif all_avail:
            return all_avail[-1]
        return self.start

    def get_quarterly_rebalance_dates(self) -> List[str]:
        """Generate quarterly rebalance dates within the backtest period."""
        dates = []
        current = pd.Timestamp(self.start)
        end = pd.Timestamp(self.end)

        quarter_months = [3, 6, 9, 12]
        while current <= end:
            for m in quarter_months:
                qend = pd.Timestamp(f"{current.year}-{m:02d}-01") + pd.offsets.MonthEnd(0)
                rebal = qend + pd.offsets.BDay(1)
                if pd.Timestamp(self.start) <= rebal <= end:
                    dates.append(rebal.strftime("%Y-%m-%d"))
            current = current + pd.DateOffset(years=1)

        return sorted(set(dates))


def _safe_get(df: pd.DataFrame, col, row_names: List[str]) -> float:
    """Safely extract a value from a financials DataFrame."""
    for name in row_names:
        try:
            if name in df.index:
                val = df.loc[name, col]
                if pd.notna(val):
                    return float(val)
        except Exception:
            pass
    return 0.0
