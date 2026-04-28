"""
Risk manager — the last gate before any order is placed.
Every trade must pass all checks here.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from config.settings import Settings
from strategy.base import Signal
from risk.kill_switch import KillSwitch


logger = logging.getLogger(__name__)


@dataclass
class TradeInstruction:
    """An approved trade ready for execution."""
    signal: Signal
    quantity: int
    order_type: str       # "MARKET" or "LIMIT"
    limit_price: float    # 0 for market orders
    stop_loss: float
    take_profit: float


@dataclass
class RiskState:
    """Tracks running risk metrics for the session."""
    daily_pnl: float = 0.0
    open_trade_count: int = 0
    open_positions: dict = field(default_factory=dict)  # code -> dollar exposure
    consecutive_losses: int = 0
    last_loss_time: Optional[datetime] = None
    trades_today: int = 0


class RiskManager:
    """Pre-trade risk checks and position sizing."""

    def __init__(self, settings: Settings, kill_switch: KillSwitch):
        self.settings = settings
        self.kill_switch = kill_switch
        self.state = RiskState()

    def check_and_size(
        self, signal: Signal, account_balance: float
    ) -> Optional[TradeInstruction]:
        """
        Run all risk checks on a signal. If approved, return a sized TradeInstruction.
        Returns None if any check fails.
        """
        checks = [
            self._check_kill_switch,
            self._check_live_mode_safety,
            self._check_daily_loss,
            self._check_max_open_trades,
            self._check_ticker_concentration,
            self._check_cooldown,
        ]

        for check in checks:
            reason = check(signal, account_balance)
            if reason:
                logger.warning(f"Risk REJECTED {signal.code}: {reason}")
                return None

        # Position sizing
        quantity = self._calculate_position_size(signal, account_balance)
        if quantity <= 0:
            logger.warning(f"Risk REJECTED {signal.code}: position size = 0")
            return None

        instruction = TradeInstruction(
            signal=signal,
            quantity=quantity,
            order_type="LIMIT",
            limit_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
        )

        logger.info(
            f"Risk APPROVED {signal.code}: {quantity} shares @ ${signal.entry_price:.2f} "
            f"(stop=${signal.stop_loss:.2f}, target=${signal.take_profit:.2f})"
        )
        return instruction

    def _check_kill_switch(self, signal: Signal, balance: float) -> Optional[str]:
        if self.kill_switch.is_active or self.settings.kill_switch:
            return "Kill switch is active"
        return None

    def _check_live_mode_safety(self, signal: Signal, balance: float) -> Optional[str]:
        # This is an informational check — paper trading always allowed
        if self.settings.live_trading_enabled:
            logger.info("LIVE TRADING MODE — order will be real")
        return None

    def _check_daily_loss(self, signal: Signal, balance: float) -> Optional[str]:
        max_loss = balance * self.settings.max_daily_loss_pct
        if self.state.daily_pnl < -max_loss:
            return (
                f"Daily loss limit hit: ${self.state.daily_pnl:.2f} "
                f"exceeds -${max_loss:.2f}"
            )
        return None

    def _check_max_open_trades(self, signal: Signal, balance: float) -> Optional[str]:
        if self.state.open_trade_count >= self.settings.max_open_trades:
            return (
                f"Max open trades reached: {self.state.open_trade_count}"
                f"/{self.settings.max_open_trades}"
            )
        return None

    def _check_ticker_concentration(self, signal: Signal, balance: float) -> Optional[str]:
        existing_exposure = self.state.open_positions.get(signal.code, 0)
        max_exposure = balance * self.settings.max_ticker_concentration
        if existing_exposure >= max_exposure:
            return (
                f"Ticker concentration limit: ${existing_exposure:.2f} "
                f"in {signal.code} (max ${max_exposure:.2f})"
            )
        return None

    def _check_cooldown(self, signal: Signal, balance: float) -> Optional[str]:
        if self.state.consecutive_losses < self.settings.cooldown_after_consecutive_losses:
            return None

        if self.state.last_loss_time is None:
            return None

        cooldown_end = self.state.last_loss_time + timedelta(
            minutes=self.settings.cooldown_duration_minutes
        )
        if datetime.now() < cooldown_end:
            remaining = (cooldown_end - datetime.now()).seconds // 60
            return (
                f"Cooldown active: {self.state.consecutive_losses} consecutive losses. "
                f"{remaining}min remaining"
            )

        # Cooldown expired — reset counter
        self.state.consecutive_losses = 0
        return None

    def _calculate_position_size(self, signal: Signal, balance: float) -> int:
        """Calculate number of shares based on risk budget."""
        # Method: risk-based sizing
        # Risk per trade = min(pct-based, dollar-based)
        pct_budget = balance * self.settings.max_position_size_pct
        dollar_budget = self.settings.max_position_size_usd
        budget = min(pct_budget, dollar_budget)

        if signal.entry_price <= 0:
            return 0

        # Number of shares we can buy within budget
        quantity = int(budget / signal.entry_price)

        # If we have a stop loss, check risk per share
        if signal.stop_loss > 0:
            risk_per_share = abs(signal.entry_price - signal.stop_loss)
            if risk_per_share > 0:
                # Don't risk more than 1% of account per trade
                max_risk = balance * 0.01
                risk_based_qty = int(max_risk / risk_per_share)
                quantity = min(quantity, risk_based_qty)

        return max(quantity, 0)

    def record_trade_result(self, pnl: float) -> None:
        """Update risk state after a trade closes."""
        self.state.daily_pnl += pnl
        self.state.trades_today += 1

        if pnl < 0:
            self.state.consecutive_losses += 1
            self.state.last_loss_time = datetime.now()
        else:
            self.state.consecutive_losses = 0

    def update_positions(self, positions: Dict[str, float]) -> None:
        """Update current position exposure from broker data."""
        self.state.open_positions = positions
        self.state.open_trade_count = len(positions)

    def reset_daily(self) -> None:
        """Reset daily counters (call at start of each trading day)."""
        self.state.daily_pnl = 0.0
        self.state.trades_today = 0
        self.state.consecutive_losses = 0
        logger.info("Risk manager daily counters reset")
