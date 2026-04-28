"""
Unified execution layer.
Routes orders to paper trading (SIMULATE) or live (REAL) based on config.
"""

import logging
from typing import Dict

from moomoo import RET_OK, TrdSide, OrderType, TrdEnv

from config.settings import Settings
from connection.gateway import Gateway
from risk.manager import TradeInstruction
from execution.order_types import OrderResult


logger = logging.getLogger(__name__)

ORDER_TYPE_MAP = {
    "MARKET": OrderType.MARKET,
    "LIMIT": OrderType.NORMAL,
}


class Executor:
    """Places orders through the moomoo API."""

    def __init__(self, settings: Settings, gateway: Gateway):
        self.settings = settings
        self.gw = gateway

    @property
    def trd_env(self) -> TrdEnv:
        if self.settings.live_trading_enabled:
            return TrdEnv.REAL
        return TrdEnv.SIMULATE

    def execute(self, instruction: TradeInstruction) -> OrderResult:
        """
        Execute a trade instruction.

        Double-checks safety flags before placing any order.
        """
        # Safety gate: refuse live orders if flag is off
        if self.trd_env == TrdEnv.REAL and not self.settings.live_trading_enabled:
            return OrderResult(
                success=False,
                message="Live trading not enabled — refusing real order",
            )

        side = TrdSide.BUY if instruction.signal.direction.value == "LONG" else TrdSide.SELL
        order_type = ORDER_TYPE_MAP.get(instruction.order_type, OrderType.NORMAL)

        price = instruction.limit_price if instruction.order_type == "LIMIT" else 0
        mode = "PAPER" if self.trd_env == TrdEnv.SIMULATE else "LIVE"

        logger.info(
            f"[{mode}] Placing {instruction.order_type} {instruction.signal.direction.value} "
            f"order: {instruction.quantity} x {instruction.signal.code} @ ${price:.2f}"
        )

        ret, data = self.gw.trade_ctx.place_order(
            price=price,
            qty=instruction.quantity,
            code=instruction.signal.code,
            trd_side=side,
            order_type=order_type,
            trd_env=self.trd_env,
            remark=f"strategy:{instruction.signal.strategy_name}",
        )

        if ret != RET_OK:
            logger.error(f"Order failed: {data}")
            return OrderResult(
                success=False,
                code=instruction.signal.code,
                message=str(data),
            )

        order_id = str(data.iloc[0].get("order_id", ""))
        status = str(data.iloc[0].get("order_status", ""))

        logger.info(f"Order placed: id={order_id}, status={status}")

        return OrderResult(
            success=True,
            order_id=order_id,
            code=instruction.signal.code,
            side=instruction.signal.direction.value,
            quantity=instruction.quantity,
            price=price,
            status=status,
        )

    def get_positions(self) -> Dict[str, float]:
        """Get current position exposure (code -> market value)."""
        ret, data = self.gw.trade_ctx.position_list_query(trd_env=self.trd_env)
        if ret != RET_OK:
            logger.error(f"Position query failed: {data}")
            return {}

        positions = {}
        for _, row in data.iterrows():
            code = row.get("code", "")
            market_val = row.get("market_val", 0)
            if code and market_val != 0:
                positions[code] = float(market_val)

        return positions

    def get_account_balance(self) -> float:
        """Get total account cash balance."""
        ret, data = self.gw.trade_ctx.accinfo_query(trd_env=self.trd_env)
        if ret != RET_OK:
            logger.error(f"Account info query failed: {data}")
            return 0.0

        return float(data.iloc[0].get("total_assets", 0))
