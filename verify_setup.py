"""
Verification script — run this to test your setup step by step.
Usage: python verify_setup.py
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))


def step(num: int, name: str):
    print(f"\n{'='*50}")
    print(f"  Step {num}: {name}")
    print(f"{'='*50}")


def main():
    # Step 1: Check imports
    step(1, "Check Python imports")
    try:
        import moomoo
        print(f"  moomoo-api version: {moomoo.__version__ if hasattr(moomoo, '__version__') else 'installed'}")
    except ImportError:
        print("  FAIL: moomoo-api not installed. Run: pip install moomoo-api")
        return

    import pandas as pd
    import numpy as np
    import yaml
    print(f"  pandas: {pd.__version__}")
    print(f"  numpy: {np.__version__}")
    print("  All imports OK")

    # Step 2: Check config
    step(2, "Load configuration")
    from config.settings import get_settings
    settings = get_settings()
    print(f"  OpenD: {settings.opend_host}:{settings.opend_port}")
    print(f"  Live trading: {settings.live_trading_enabled}")
    print(f"  Market: {settings.trading_market}")
    print(f"  Max position: ${settings.max_position_size_usd}")
    print("  Config loaded OK")

    # Step 3: Check watchlist
    step(3, "Load watchlist")
    from watchlist.manager import WatchlistManager
    wl = WatchlistManager()
    print(f"  Tickers: {wl.codes}")
    print(f"  Count: {len(wl.codes)}")
    print("  Watchlist loaded OK")

    # Step 4: Connect to OpenD
    step(4, "Connect to OpenD gateway")
    from connection.gateway import Gateway
    gw = Gateway(settings)
    try:
        gw.connect()
        print(f"  Connected: {gw.is_connected()}")
        print(f"  Trading env: {gw.trd_env}")
    except Exception as e:
        print(f"  FAIL: {e}")
        print("  Make sure OpenD is running!")
        return

    # Step 5: Pull a snapshot
    step(5, "Fetch market snapshot")
    from data.market_data import MarketData
    md = MarketData(gw)
    snap = md.get_snapshot(["US.AAPL"])
    if snap is not None:
        print(f"  AAPL snapshot:")
        print(f"    Last price: ${snap.iloc[0].get('last_price', 'N/A')}")
        print(f"    Volume: {snap.iloc[0].get('volume', 'N/A')}")
        print("  Snapshot OK")
    else:
        print("  FAIL: Could not fetch snapshot")

    # Step 6: Pull historical klines
    step(6, "Fetch historical klines")
    klines = md.get_historical_klines("US.AAPL", ktype="K_DAY", max_count=10)
    if klines is not None:
        print(f"  Fetched {len(klines)} daily candles for AAPL")
        print(f"  Latest: {klines.iloc[-1][['time_key', 'close', 'volume']].to_dict()}")
        print("  Klines OK")
    else:
        print("  FAIL: Could not fetch klines")

    # Step 7: Test indicators
    step(7, "Test indicator engine")
    if klines is not None and len(klines) >= 10:
        # Need more data for indicators
        klines_full = md.get_historical_klines("US.AAPL", ktype="K_DAY", max_count=100)
        if klines_full is not None and len(klines_full) >= 50:
            from indicators.core import enrich
            enriched = enrich(klines_full)
            latest = enriched.iloc[-1]
            print(f"  SMA(20): {latest.get('sma_20', 'N/A'):.2f}")
            print(f"  RSI(14): {latest.get('rsi_14', 'N/A'):.2f}")
            print(f"  ATR(14): {latest.get('atr_14', 'N/A'):.2f}")
            print(f"  RVOL: {latest.get('rvol', 'N/A'):.2f}")
            print("  Indicators OK")

    # Step 8: Test paper trading account
    step(8, "Check paper trading account")
    from execution.executor import Executor
    executor = Executor(settings, gw)
    balance = executor.get_account_balance()
    positions = executor.get_positions()
    print(f"  Account balance: ${balance:,.2f}")
    print(f"  Open positions: {len(positions)}")
    print("  Paper account OK")

    # Cleanup
    gw.close()

    step(9, "SETUP COMPLETE")
    print("  All systems verified. You're ready to run:")
    print("    python main.py")
    print()


if __name__ == "__main__":
    main()
