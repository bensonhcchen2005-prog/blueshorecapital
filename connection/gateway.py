"""
OpenD Gateway connection manager.
Provides quote and trade contexts with automatic lifecycle management.
"""

import logging
from typing import Optional
from moomoo import (
    OpenQuoteContext,
    OpenSecTradeContext,
    RET_OK,
    TrdEnv,
    TrdMarket,
    SysConfig,
)
from config.settings import Settings


logger = logging.getLogger(__name__)

# Map string market names to moomoo enums
MARKET_MAP = {
    "US": TrdMarket.US,
    "HK": TrdMarket.HK,
    "CN": TrdMarket.CN,
    "SG": TrdMarket.SG,
}


class Gateway:
    """Manages connections to the moomoo OpenD gateway."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._quote_ctx: Optional[OpenQuoteContext] = None
        self._trade_ctx: Optional[OpenSecTradeContext] = None
        self._trade_unlocked = False

    @property
    def trd_env(self) -> TrdEnv:
        if self.settings.live_trading_enabled:
            return TrdEnv.REAL
        return TrdEnv.SIMULATE

    @property
    def trd_market(self) -> TrdMarket:
        return MARKET_MAP.get(self.settings.trading_market, TrdMarket.US)

    def connect(self) -> None:
        """Establish connections to OpenD for quotes and trading."""
        SysConfig.set_all_thread_daemon(True)

        host = self.settings.opend_host
        port = self.settings.opend_port

        logger.info(f"Connecting to OpenD at {host}:{port}")

        self._quote_ctx = OpenQuoteContext(host=host, port=port)
        self._trade_ctx = OpenSecTradeContext(
            host=host, port=port,
        )

        # Verify connections
        ret, data = self._quote_ctx.get_global_state()
        if ret != RET_OK:
            raise ConnectionError(f"Failed to connect quote context: {data}")

        mode = "LIVE" if self.settings.live_trading_enabled else "PAPER"
        logger.info(f"Connected to OpenD successfully. Trading mode: {mode}")

    def unlock_trade(self) -> None:
        """Unlock live trading. Only needed for TrdEnv.REAL."""
        if not self.settings.live_trading_enabled:
            logger.info("Paper trading mode — no unlock needed")
            return

        if not self.settings.trade_password:
            raise ValueError("MOOMOO_TRADE_PASSWORD required for live trading")

        ret, data = self._trade_ctx.unlock_trade(
            password=self.settings.trade_password
        )
        if ret != RET_OK:
            raise ConnectionError(f"Failed to unlock trading: {data}")

        self._trade_unlocked = True
        logger.info("Live trading unlocked")

    @property
    def quote_ctx(self) -> OpenQuoteContext:
        if self._quote_ctx is None:
            raise RuntimeError("Gateway not connected. Call connect() first.")
        return self._quote_ctx

    @property
    def trade_ctx(self) -> OpenSecTradeContext:
        if self._trade_ctx is None:
            raise RuntimeError("Gateway not connected. Call connect() first.")
        return self._trade_ctx

    def is_connected(self) -> bool:
        """Check if the gateway connections are alive."""
        if self._quote_ctx is None:
            return False
        try:
            ret, _ = self._quote_ctx.get_global_state()
            return ret == RET_OK
        except Exception:
            return False

    def close(self) -> None:
        """Close all connections gracefully."""
        if self._quote_ctx is not None:
            self._quote_ctx.close()
            self._quote_ctx = None
        if self._trade_ctx is not None:
            self._trade_ctx.close()
            self._trade_ctx = None
        self._trade_unlocked = False
        logger.info("Gateway connections closed")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
