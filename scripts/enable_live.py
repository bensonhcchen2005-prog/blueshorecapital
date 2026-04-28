#!/usr/bin/env python3
"""
enable_live.py — the ONLY way to flip live_mode to True.

Usage:
    python scripts/enable_live.py           # interactive confirm
    python scripts/enable_live.py --disable # turn live_mode off

Live mode means: the live order gate will queue orders for per-order human
approval, and (once approved) they will be submitted to your moomoo live
account. Until you run this script, the system runs in SHADOW mode — the
stage controller evaluates everything and logs it, but no real order is
ever placed.
"""
import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from risk.stage_controller import StageController, STAGES  # noqa: E402


def show_status(ctl: StageController) -> None:
    s = ctl.state
    cfg = STAGES[s.stage]
    print()
    print("=" * 64)
    print(f"  LIVE STAGE STATUS — Stage {s.stage} ({cfg['name']})")
    print("=" * 64)
    print(f"  live_mode:         {'LIVE' if s.live_mode else 'SHADOW'}")
    print(f"  live_enabled_at:   {s.live_enabled_at or '—'}")
    print(f"  capital_cap:       ${cfg['capital_cap']:>10,.0f}")
    print(f"  starting_equity:   ${s.starting_equity:>10,.2f}")
    print(f"  current_equity:    ${s.current_equity:>10,.2f}")
    print(f"  hwm:               ${s.hwm:>10,.2f}")
    print(f"  hard_floor:        ${s.hard_floor:>10,.2f}")
    print(f"  emergency_floor:   ${s.emergency_floor:>10,.2f}")
    print(f"  trailing_floor:    ${s.trailing_floor:>10,.2f}")
    print(f"  halt_flag:         {s.halt_flag}  {'('+s.halt_reason+')' if s.halt_reason else ''}")
    print(f"  de_risk_flag:      {s.de_risk_flag}")
    print(f"  closed_trades:     {s.closed_trade_count} / {cfg['min_closed_trades_for_promotion']}")
    print("-" * 64)
    print(f"  max_position:      ${cfg['capital_cap']*cfg['max_position_pct']:>10,.0f}  "
          f"({cfg['max_position_pct']*100:.0f}% of cap)")
    print(f"  max_daily_expose:  ${cfg['capital_cap']*cfg['max_new_exposure_per_day_pct']:>10,.0f}  "
          f"({cfg['max_new_exposure_per_day_pct']*100:.0f}% of cap)")
    print(f"  max_open_pos:      {cfg['max_open_positions']}")
    print(f"  allowed_symbols:   {', '.join(cfg['allowed_symbols'])}")
    print(f"  allowed_strats:    {', '.join(cfg['allowed_strategies'])}")
    print(f"  min_signal_score:  {cfg['min_signal_score']}")
    print(f"  regime_required:   {', '.join(cfg['regime_required'])}")
    print(f"  options_mode:      {cfg['options_mode']}")
    print(f"  per_trade_stop:    {cfg['per_trade_stop_pct']*100:.1f}%")
    print(f"  per_trade_tp:      {cfg['per_trade_tp_pct']*100:.1f}%")
    print(f"  daily_halt:        -{cfg['daily_loss_halt_pct']*100:.1f}%  "
          f"(${cfg['capital_cap']*cfg['daily_loss_halt_pct']:,.0f})")
    print(f"  weekly_halt:       -{cfg['weekly_loss_halt_pct']*100:.1f}%  "
          f"(${cfg['capital_cap']*cfg['weekly_loss_halt_pct']:,.0f})")
    print(f"  human_approval:    {cfg['human_approval_required']}")
    print("=" * 64)
    print()


def enable(ctl: StageController) -> int:
    show_status(ctl)
    if ctl.state.live_mode:
        print("  Live mode is already ENABLED.")
        return 0
    print("  ⚠  This enables REAL ORDER PLACEMENT on your moomoo account.")
    print("     Every live order will still require per-order human approval")
    print("     in the dashboard (Stage 1 rule). But the gate will be OPEN.")
    print()
    phrase = f"ENABLE LIVE STAGE {ctl.state.stage}"
    print(f"  Type exactly:  {phrase}")
    print()
    try:
        got = input("  > ").strip()
    except EOFError:
        got = ""
    if got != phrase:
        print("  ABORTED. live_mode remains OFF.")
        return 1
    ctl.state.live_mode = True
    ctl.state.live_enabled_at = datetime.now(timezone.utc).isoformat()
    ctl._save()
    print()
    print(f"  ✓ live_mode ENABLED at Stage {ctl.state.stage}")
    print(f"  ✓ To disable: python scripts/enable_live.py --disable")
    print()
    return 0


def disable(ctl: StageController) -> int:
    if not ctl.state.live_mode:
        print("  live_mode already OFF (shadow mode).")
        return 0
    ctl.state.live_mode = False
    ctl._save()
    print(f"  ✓ live_mode DISABLED. System is back in shadow mode.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--disable", action="store_true", help="Turn live_mode OFF")
    ap.add_argument("--status", action="store_true", help="Show status only")
    args = ap.parse_args()

    ctl = StageController()
    if args.status:
        show_status(ctl)
        return 0
    if args.disable:
        return disable(ctl)
    return enable(ctl)


if __name__ == "__main__":
    sys.exit(main())
