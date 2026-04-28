"""
Market scanner — pull snapshots and recent klines for trade idea generation.
Covers equities, ETFs, macro instruments across all 3 pods.
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from moomoo import RET_OK, KLType, AuType, SysConfig
from config.settings import get_settings
from connection.gateway import Gateway

SysConfig.set_all_thread_daemon(True)

# Tickers organized by pod relevance
TICKERS = {
    "ls_equity": [
        "US.NVDA", "US.AAPL", "US.MSFT", "US.GOOGL", "US.AMZN",
        "US.META", "US.TSLA", "US.AMD", "US.INTC", "US.CRM",
        "US.NFLX", "US.AVGO", "US.ORCL", "US.UBER", "US.SHOP",
        "US.BA", "US.DIS", "US.NKE", "US.PFE", "US.JPM",
        "US.GS", "US.V", "US.UNH", "US.XOM", "US.CVX",
    ],
    "event_driven": [
        "US.NVDA", "US.TSLA", "US.SMCI", "US.ARM", "US.PLTR",
        "US.MSTR", "US.GME", "US.RIVN", "US.LCID", "US.SNAP",
        "US.PINS", "US.ROKU", "US.SQ", "US.COIN", "US.HOOD",
        "US.AI", "US.IONQ", "US.RKLB", "US.SOFI", "US.AFRM",
    ],
    "macro": [
        "US.SPY", "US.QQQ", "US.IWM", "US.DIA", "US.TLT",
        "US.GLD", "US.SLV", "US.USO", "US.UNG", "US.FXI",
        "US.EWJ", "US.EWZ", "US.EEM", "US.XLF", "US.XLE",
        "US.XLK", "US.XLV", "US.XLU", "US.VIX", "US.UVXY",
    ],
}

ALL_TICKERS = list(set(
    TICKERS["ls_equity"] + TICKERS["event_driven"] + TICKERS["macro"]
))


def main():
    settings = get_settings()
    gw = Gateway(settings)
    gw.connect()
    print(f"Connected: {gw.is_connected()}")

    results = {}

    # Batch snapshots (max ~400 per call)
    print(f"\nFetching snapshots for {len(ALL_TICKERS)} tickers...")
    batch_size = 50
    for i in range(0, len(ALL_TICKERS), batch_size):
        batch = ALL_TICKERS[i:i + batch_size]
        ret, snap = gw.quote_ctx.get_market_snapshot(batch)
        if ret != RET_OK:
            print(f"  Snapshot batch failed: {snap}")
            continue

        for _, row in snap.iterrows():
            code = row.get("code", "")
            results[code] = {
                "code": code,
                "name": str(row.get("name", "")),
                "last_price": float(row.get("last_price", 0)),
                "open": float(row.get("open_price", 0)),
                "high": float(row.get("high_price", 0)),
                "low": float(row.get("low_price", 0)),
                "prev_close": float(row.get("prev_close_price", 0)),
                "volume": int(row.get("volume", 0)),
                "turnover": float(row.get("turnover", 0)),
                "market_cap": float(row.get("market_val", 0)),
                "pe_ratio": float(row.get("pe_ttm_ratio", 0)),
                "change_pct": 0,
            }
            if results[code]["prev_close"] > 0:
                results[code]["change_pct"] = round(
                    (results[code]["last_price"] - results[code]["prev_close"])
                    / results[code]["prev_close"] * 100, 2
                )

    print(f"Got data for {len(results)} tickers")

    # Fetch 20-day klines for key tickers (for momentum/volatility calc)
    key_tickers = [
        "US.NVDA", "US.AAPL", "US.MSFT", "US.TSLA", "US.META",
        "US.AMD", "US.GOOGL", "US.AMZN", "US.SPY", "US.QQQ",
        "US.TLT", "US.GLD", "US.XOM", "US.JPM", "US.BA",
        "US.SMCI", "US.PLTR", "US.COIN", "US.ARM", "US.IWM",
        "US.XLE", "US.XLF", "US.XLK", "US.FXI", "US.EEM",
        "US.AVGO", "US.CRM", "US.NFLX", "US.UNH", "US.GS",
    ]

    print(f"\nFetching 60-day klines for {len(key_tickers)} key tickers...")
    for code in key_tickers:
        import time
        time.sleep(0.3)
        ret, data, _ = gw.quote_ctx.request_history_kline(
            code, ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=60
        )
        if ret != RET_OK or data is None or len(data) < 20:
            continue

        closes = data["close"].tolist()
        volumes = data["volume"].tolist()
        highs = data["high"].tolist()
        lows = data["low"].tolist()

        # 20-day metrics
        c20 = closes[-20:]
        v20 = volumes[-20:]

        import numpy as np
        returns = np.diff(np.log(c20))
        vol_20d = float(np.std(returns) * np.sqrt(252) * 100)  # annualized %
        avg_vol = float(np.mean(v20))
        momentum_20d = round((c20[-1] / c20[0] - 1) * 100, 2)

        # 5-day momentum
        c5 = closes[-5:]
        momentum_5d = round((c5[-1] / c5[0] - 1) * 100, 2)

        # Simple SMA
        sma_20 = float(np.mean(c20))
        sma_50 = float(np.mean(closes[-50:])) if len(closes) >= 50 else 0

        # ATR (14-day)
        atr_data = data.tail(15)
        trs = []
        for i in range(1, len(atr_data)):
            h = float(atr_data.iloc[i]["high"])
            l = float(atr_data.iloc[i]["low"])
            pc = float(atr_data.iloc[i - 1]["close"])
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        atr_14 = float(np.mean(trs[-14:])) if len(trs) >= 14 else 0

        # 52-week high/low approximation (from 60 days available)
        high_60d = max(highs[-60:]) if len(highs) >= 60 else max(highs)
        low_60d = min(lows[-60:]) if len(lows) >= 60 else min(lows)

        if code in results:
            results[code].update({
                "vol_20d_ann": round(vol_20d, 1),
                "momentum_5d": momentum_5d,
                "momentum_20d": momentum_20d,
                "avg_volume_20d": int(avg_vol),
                "sma_20": round(sma_20, 2),
                "sma_50": round(sma_50, 2),
                "atr_14": round(atr_14, 2),
                "high_60d": round(high_60d, 2),
                "low_60d": round(low_60d, 2),
                "above_sma20": c20[-1] > sma_20,
                "above_sma50": c20[-1] > sma_50 if sma_50 > 0 else None,
            })

    gw.close()

    # Save to JSON
    output_path = Path(__file__).parent.parent / "logs" / "market_scan.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\nSaved to {output_path}")

    # Print summary tables
    print("\n" + "=" * 100)
    print("  MARKET SCAN SUMMARY")
    print("=" * 100)

    for pod_name, tickers in TICKERS.items():
        print(f"\n--- {pod_name.upper()} ---")
        print(f"{'Ticker':<12} {'Price':>10} {'Chg%':>8} {'Vol20d%':>8} {'Mom5d%':>8} {'Mom20d%':>9} {'ATR':>8} {'>SMA20':>8}")
        print("-" * 80)
        for t in tickers:
            if t in results:
                r = results[t]
                print(f"{t:<12} ${r['last_price']:>9.2f} {r['change_pct']:>7.2f}% "
                      f"{r.get('vol_20d_ann', 'N/A'):>7} "
                      f"{r.get('momentum_5d', 'N/A'):>7} "
                      f"{r.get('momentum_20d', 'N/A'):>8} "
                      f"{r.get('atr_14', 'N/A'):>7} "
                      f"{'Y' if r.get('above_sma20') else 'N':>7}")

    print("\n" + "=" * 100)


if __name__ == "__main__":
    main()
