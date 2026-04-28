"""
Execute the multi-strategy fund trades on paper trading.
Places all 10 trades across 3 pods.

Usage: python scripts/execute_fund_trades.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from moomoo import RET_OK, TrdSide, OrderType, TrdEnv, SysConfig
from config.settings import get_settings
from connection.gateway import Gateway

SysConfig.set_all_thread_daemon(True)


TRADES = [
    # Pod 1: Long/Short Equities
    {"pod": "L/S Equity", "code": "HK.01810", "side": TrdSide.BUY,  "qty": 4650, "remark": "pod1:xiaomi_long"},
    {"pod": "L/S Equity", "code": "HK.00005", "side": TrdSide.BUY,  "qty": 875,  "remark": "pod1:hsbc_long"},
    {"pod": "L/S Equity", "code": "HK.09988", "side": TrdSide.SELL, "qty": 815,  "remark": "pod1:baba_short"},
    {"pod": "L/S Equity", "code": "HK.03690", "side": TrdSide.SELL, "qty": 930,  "remark": "pod1:meituan_short"},

    # Pod 2: Event-Driven
    {"pod": "Event-Driven", "code": "HK.00388", "side": TrdSide.BUY,  "qty": 365,  "remark": "pod2:hkex_vol_play"},
    {"pod": "Event-Driven", "code": "HK.02318", "side": TrdSide.BUY,  "qty": 2060, "remark": "pod2:pingan_policy"},
    {"pod": "Event-Driven", "code": "HK.09866", "side": TrdSide.SELL, "qty": 1605, "remark": "pod2:nio_short"},

    # Pod 3: Global Macro
    {"pod": "Global Macro", "code": "HK.02800", "side": TrdSide.BUY,  "qty": 7655, "remark": "pod3:hsi_long"},
    {"pod": "Global Macro", "code": "HK.07500", "side": TrdSide.SELL, "qty": 86700,"remark": "pod3:nasdaq_short"},
    {"pod": "Global Macro", "code": "HK.00883", "side": TrdSide.BUY,  "qty": 3785, "remark": "pod3:cnooc_long"},
]


def main():
    settings = get_settings()

    if settings.live_trading_enabled:
        print("ERROR: Live trading is enabled. This script is paper-only.")
        return

    gw = Gateway(settings)
    gw.connect()
    print(f"Connected: {gw.is_connected()}")
    print(f"Mode: PAPER (TrdEnv.SIMULATE)")

    # Check balance first
    ret, acct = gw.trade_ctx.accinfo_query(trd_env=TrdEnv.SIMULATE)
    if ret == RET_OK:
        cash = float(acct.iloc[0].get("cash", 0))
        print(f"Paper account cash: ${cash:,.2f}")
    print()

    print("=" * 80)
    print("  EXECUTING MULTI-STRATEGY FUND TRADES")
    print("=" * 80)

    results = []
    for trade in TRADES:
        side_str = "BUY" if trade["side"] == TrdSide.BUY else "SELL"
        print(f"\n  [{trade['pod']}] {side_str} {trade['qty']} x {trade['code']}")

        ret, data = gw.trade_ctx.place_order(
            price=0,
            qty=trade["qty"],
            code=trade["code"],
            trd_side=trade["side"],
            order_type=OrderType.MARKET,
            trd_env=TrdEnv.SIMULATE,
            remark=trade["remark"],
        )

        if ret != RET_OK:
            print(f"    FAILED: {data}")
            results.append({"trade": trade, "success": False, "error": str(data)})
        else:
            oid = data.iloc[0].get("order_id", "")
            status = data.iloc[0].get("order_status", "")
            print(f"    OK — order_id={oid}, status={status}")
            results.append({"trade": trade, "success": True, "order_id": str(oid)})

        time.sleep(0.5)  # respect rate limits

    # Summary
    print("\n" + "=" * 80)
    print("  EXECUTION SUMMARY")
    print("=" * 80)
    success = sum(1 for r in results if r["success"])
    failed = len(results) - success
    print(f"  Placed: {success} / {len(results)}")
    if failed:
        print(f"  Failed: {failed}")
        for r in results:
            if not r["success"]:
                t = r["trade"]
                print(f"    {t['code']}: {r['error']}")

    # Show updated positions
    print("\n  --- Current Paper Positions ---")
    time.sleep(1)
    ret, positions = gw.trade_ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
    if ret == RET_OK and len(positions) > 0:
        total_val = 0
        print(f"  {'Code':<12} {'Qty':>8} {'Cost':>10} {'MktVal':>12} {'P&L':>10}")
        print("  " + "-" * 55)
        for _, row in positions.iterrows():
            code = row.get("code", "")
            qty = int(row.get("qty", 0))
            cost = float(row.get("cost_price", 0))
            mval = float(row.get("market_val", 0))
            pl = float(row.get("pl_val", 0))
            if qty != 0:
                total_val += abs(mval)
                print(f"  {code:<12} {qty:>8} {cost:>10.2f} {mval:>12,.2f} {pl:>10,.2f}")
        print(f"\n  Total position value: ${total_val:,.2f}")

    # Show updated account
    ret, acct = gw.trade_ctx.accinfo_query(trd_env=TrdEnv.SIMULATE)
    if ret == RET_OK:
        total = float(acct.iloc[0].get("total_assets", 0))
        cash = float(acct.iloc[0].get("cash", 0))
        mval = float(acct.iloc[0].get("market_val", 0))
        print(f"\n  Account: Total=${total:,.2f} | Cash=${cash:,.2f} | Positions=${mval:,.2f}")

    gw.close()
    print("\n  Done.")


if __name__ == "__main__":
    main()
