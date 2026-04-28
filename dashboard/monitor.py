"""
Console-based monitoring dashboard.
Prints current system state in a readable format.
"""

import logging
from datetime import datetime

from risk.manager import RiskManager
from paper_trading.tracker import PerformanceTracker


logger = logging.getLogger(__name__)


def print_status(
    risk: RiskManager,
    tracker: PerformanceTracker,
    active_signals: int = 0,
    is_paper: bool = True,
) -> None:
    """Print a formatted status summary to the console."""
    mode = "PAPER" if is_paper else "*** LIVE ***"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    summary = tracker.summary
    state = risk.state

    print(f"""
{'='*60}
  MOOMOO TRADING SYSTEM  [{mode}]  {now}
{'='*60}

  Daily P&L:       ${state.daily_pnl:>10.2f}
  Open Trades:     {state.open_trade_count:>10} / {risk.settings.max_open_trades}
  Trades Today:    {state.trades_today:>10}
  Active Signals:  {active_signals:>10}
  Consec Losses:   {state.consecutive_losses:>10}
  Kill Switch:     {'ACTIVE' if risk.kill_switch.is_active else 'OFF':>10}

  --- Performance ---
  Total Closed:    {summary.get('total_trades', 0):>10}
  Win Rate:        {summary.get('win_rate', 0):>9.1f}%
  Total P&L:       ${summary.get('total_pnl', 0):>10.2f}
  Avg P&L/Trade:   ${summary.get('avg_pnl', 0):>10.2f}
{'='*60}
""")
