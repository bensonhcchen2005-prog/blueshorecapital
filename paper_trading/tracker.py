"""
Paper trading performance tracker.
Records every trade and computes running statistics.
"""

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from execution.order_types import OrderResult


logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    timestamp: str
    code: str
    side: str
    quantity: int
    entry_price: float
    strategy: str
    order_id: str
    exit_price: float = 0.0
    pnl: float = 0.0
    status: str = "OPEN"


class PerformanceTracker:
    """Track paper trading performance."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.trades = []
        self._csv_path = log_dir / "trades.csv"
        self._init_csv()

    def _init_csv(self) -> None:
        if not self._csv_path.exists():
            with open(self._csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "code", "side", "quantity", "entry_price",
                    "exit_price", "pnl", "strategy", "order_id", "status",
                ])

    def record_entry(self, result: OrderResult, strategy_name: str) -> None:
        """Record a new trade entry."""
        record = TradeRecord(
            timestamp=datetime.now().isoformat(),
            code=result.code,
            side=result.side,
            quantity=result.quantity,
            entry_price=result.price,
            strategy=strategy_name,
            order_id=result.order_id,
        )
        self.trades.append(record)
        self._append_csv(record)
        logger.info(f"Trade recorded: {result.code} {result.side} x{result.quantity}")

    def record_exit(self, order_id: str, exit_price: float) -> None:
        """Record a trade exit and compute P&L."""
        for trade in self.trades:
            if trade.order_id == order_id and trade.status == "OPEN":
                trade.exit_price = exit_price
                trade.status = "CLOSED"
                if trade.side == "LONG":
                    trade.pnl = (exit_price - trade.entry_price) * trade.quantity
                else:
                    trade.pnl = (trade.entry_price - exit_price) * trade.quantity
                logger.info(
                    f"Trade closed: {trade.code} PnL=${trade.pnl:.2f}"
                )
                return
        logger.warning(f"No open trade found for order_id={order_id}")

    def _append_csv(self, record: TradeRecord) -> None:
        with open(self._csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                record.timestamp, record.code, record.side, record.quantity,
                record.entry_price, record.exit_price, record.pnl,
                record.strategy, record.order_id, record.status,
            ])

    @property
    def summary(self) -> dict:
        closed = [t for t in self.trades if t.status == "CLOSED"]
        if not closed:
            return {"total_trades": 0, "win_rate": 0, "total_pnl": 0}

        wins = [t for t in closed if t.pnl > 0]
        total_pnl = sum(t.pnl for t in closed)

        return {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "win_rate": round(len(wins) / len(closed) * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(closed), 2),
        }
