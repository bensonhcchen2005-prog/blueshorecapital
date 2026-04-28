"""
Quick execution: Buy 10 shares of NVIDIA on paper trading.
Usage: python scripts/buy_nvda.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from moomoo import RET_OK, TrdSide, OrderType, TrdEnv, SysConfig
from config.settings import get_settings
from connection.gateway import Gateway


def main():
    settings = get_settings()
    SysConfig.set_all_thread_daemon(True)

    print("=" * 50)
    print("  BUY 10 SHARES NVDA — PAPER TRADE")
    print("=" * 50)

    # Safety check
    if settings.live_trading_enabled:
        print("\n  WARNING: Live trading is enabled!")
        print("  This script only runs in PAPER mode.")
        print("  Set LIVE_TRADING_ENABLED=False in .env")
        return

    gw = Gateway(settings)
    gw.connect()
    print(f"\n  Connected to OpenD: {gw.is_connected()}")

    # Get current NVDA price via snapshot
    ret, snap = gw.quote_ctx.get_market_snapshot(["US.NVDA"])
    if ret != RET_OK:
        print(f"  Failed to get NVDA quote: {snap}")
        gw.close()
        return

    last_price = float(snap.iloc[0]["last_price"])
    print(f"  NVDA last price: ${last_price:.2f}")
    print(f"  Order: BUY 10 shares @ market")
    print(f"  Estimated cost: ${last_price * 10:,.2f}")
    print(f"  Mode: PAPER (TrdEnv.SIMULATE)")

    # Place paper trade — market order
    ret, data = gw.trade_ctx.place_order(
        price=0,
        qty=10,
        code="US.NVDA",
        trd_side=TrdSide.BUY,
        order_type=OrderType.MARKET,
        trd_env=TrdEnv.SIMULATE,
        remark="manual:buy_nvda_10",
    )

    if ret != RET_OK:
        print(f"\n  ORDER FAILED: {data}")
    else:
        order_id = data.iloc[0].get("order_id", "")
        status = data.iloc[0].get("order_status", "")
        print(f"\n  ORDER PLACED SUCCESSFULLY")
        print(f"  Order ID: {order_id}")
        print(f"  Status: {status}")

    # Show current paper positions
    print("\n  --- Paper Trading Positions ---")
    ret, positions = gw.trade_ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
    if ret == RET_OK and len(positions) > 0:
        for _, row in positions.iterrows():
            code = row.get("code", "")
            qty = row.get("qty", 0)
            cost = row.get("cost_price", 0)
            market_val = row.get("market_val", 0)
            pl = row.get("pl_val", 0)
            if qty > 0:
                print(f"  {code}: {int(qty)} shares @ ${float(cost):.2f} | "
                      f"Value: ${float(market_val):,.2f} | P&L: ${float(pl):,.2f}")
    else:
        print("  No positions")

    # Show paper account balance
    ret, acct = gw.trade_ctx.accinfo_query(trd_env=TrdEnv.SIMULATE)
    if ret == RET_OK:
        total = float(acct.iloc[0].get("total_assets", 0))
        cash = float(acct.iloc[0].get("cash", 0))
        print(f"\n  Account: Total=${total:,.2f} | Cash=${cash:,.2f}")

    gw.close()
    print("\n  Done.")


if __name__ == "__main__":
    main()
