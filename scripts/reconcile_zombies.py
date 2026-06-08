"""
Reconcile zombie shorts — flatten broker positions that the bot doesn't track.

Default: DRY-RUN — prints the plan, no orders sent.
Run with --execute to actually place buy-to-cover orders.

Usage:
    ./venv/bin/python -m scripts.reconcile_zombies              # dry-run plan
    ./venv/bin/python -m scripts.reconcile_zombies --execute    # send orders
    ./venv/bin/python -m scripts.reconcile_zombies --execute --max-loss 30000
                                                                # safety circuit-breaker

Logic:
- Walks every broker position
- Cross-references against auto_trades.csv (the bot's known book)
- Any position NOT in the bot's book is a zombie
- Plans an opposing market order (buy-to-cover shorts, sell to flatten orphan longs)
- Hard cap on total realised loss this run (--max-loss, default $50K)
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

from moomoo import (
    OpenUSTradeContext, OpenSecTradeContext, OpenQuoteContext,
    OrderType, TrdEnv, TrdSide, RET_OK, SecurityFirm,
)

PROJECT_ROOT = Path(__file__).parent.parent
TRADES_CSV = PROJECT_ROOT / "logs" / "auto_trades.csv"


def load_bot_book() -> set[str]:
    """Codes the bot currently tracks as OPEN."""
    if not TRADES_CSV.exists():
        return set()
    with TRADES_CSV.open() as f:
        return {r["code"] for r in csv.DictReader(f) if r.get("status") == "OPEN"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="Actually send orders. Default is dry-run.")
    ap.add_argument("--max-loss", type=float, default=50_000,
                    help="Abort if planned realised loss exceeds this ($).")
    ap.add_argument("--include-longs", action="store_true",
                    help="Also flatten orphan LONG positions (default: shorts only).")
    args = ap.parse_args()

    pwd = os.environ.get("MOOMOO_TRADE_PASSWORD", "")
    if args.execute and not pwd:
        print("ERROR: --execute requires MOOMOO_TRADE_PASSWORD env var")
        sys.exit(1)

    bot_book = load_bot_book()
    print(f"Bot's known open positions: {len(bot_book)} → {sorted(bot_book)}\n")

    us = OpenUSTradeContext(host="127.0.0.1", port=11111)
    if pwd:
        us.unlock_trade(pwd)

    plan = []
    total_planned_pnl = 0.0

    # NOTE: zombies are US-only (HK paper doesn't allow shorts and the lone HK
    # long is tracked by the bot). Skip HK trade ctx to avoid init hangs.
    for ctx, market in [(us, "US")]:
        ret, positions = ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
        if ret != RET_OK or positions is None or len(positions) == 0:
            continue
        for _, r in positions.iterrows():
            code = r["code"]
            qty = float(r["qty"])
            pnl = float(r.get("pl_val", 0))
            if qty == 0:
                continue
            if code in bot_book:
                continue  # bot is managing this — leave alone
            is_short = qty < 0
            if not is_short and not args.include_longs:
                continue  # only flatten shorts unless explicitly told otherwise
            side = TrdSide.BUY if is_short else TrdSide.SELL
            plan.append({
                "code": code, "qty": int(abs(qty)),
                "side": "BUY-TO-COVER" if is_short else "SELL",
                "pnl_at_close": pnl,
                "ctx": ctx, "trd_side": side,
            })
            total_planned_pnl += pnl

    if not plan:
        print("Nothing to reconcile — broker book matches bot book.")
        return

    print("=" * 72)
    print(f"{'ACTION':14}{'CODE':20}{'QTY':>8}  EST P&L AT CLOSE")
    print("=" * 72)
    for p in plan:
        print(f"  {p['side']:12}  {p['code']:20}{p['qty']:>8}  ${p['pnl_at_close']:+,.0f}")
    print("-" * 72)
    print(f"  {'TOTAL':12}  {len(plan)} orders             ${total_planned_pnl:+,.0f}")
    print("=" * 72)

    if total_planned_pnl < -args.max_loss:
        print(f"\n🛑 ABORT: planned realised loss ${total_planned_pnl:,.0f} exceeds "
              f"--max-loss ${args.max_loss:,.0f}.")
        print("   Re-run with a higher --max-loss if you accept the loss.")
        sys.exit(2)

    if not args.execute:
        print("\nDry-run only. To execute, re-run with --execute.")
        return

    print("\n⚠ Executing in 5 seconds — Ctrl+C to abort...")
    for i in range(5, 0, -1):
        print(f"  {i}...", end="", flush=True)
        time.sleep(1)
    print(" GO\n")

    successes = failures = 0
    for p in plan:
        try:
            ret, data = p["ctx"].place_order(
                price=0, qty=p["qty"], code=p["code"],
                trd_side=p["trd_side"],
                order_type=OrderType.MARKET,
                trd_env=TrdEnv.SIMULATE,
                remark="zombie-reconcile",
            )
            if ret == RET_OK:
                successes += 1
                print(f"  ✓ {p['side']:12} {p['code']:20} {p['qty']:>6}")
            else:
                failures += 1
                print(f"  ✗ {p['side']:12} {p['code']:20} {p['qty']:>6}  ERR: {data}")
        except Exception as e:
            failures += 1
            print(f"  ✗ {p['side']:12} {p['code']:20} {p['qty']:>6}  EXC: {e}")
        time.sleep(2.5)  # respect order rate limit

    print(f"\nDone: {successes} ok, {failures} failed.")

    us.close()


if __name__ == "__main__":
    main()
