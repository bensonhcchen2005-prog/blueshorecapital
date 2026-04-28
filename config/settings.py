"""
Central configuration for the moomoo trading system.
Loads from .env file and provides typed access to all settings.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).parent.parent


@dataclass(frozen=True)
class Settings:
    # OpenD Gateway
    opend_host: str = "127.0.0.1"
    opend_port: int = 11111

    # Trading Mode
    live_trading_enabled: bool = False  # MUST be explicitly enabled
    trading_market: str = "US"
    trade_password: str = ""

    # Risk Limits
    max_position_size_pct: float = 0.05       # 5% of account per trade
    max_position_size_usd: float = 5000.0     # hard dollar cap
    max_daily_loss_pct: float = 0.02          # 2% daily loss halt
    max_open_trades: int = 5
    max_ticker_concentration: float = 0.10    # 10% in one ticker
    cooldown_after_consecutive_losses: int = 3
    cooldown_duration_minutes: int = 30
    min_volume_filter: int = 500_000
    max_spread_pct: float = 0.005

    # Kill Switch
    kill_switch: bool = False

    # Logging
    log_level: str = "INFO"
    log_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")

    # Data
    data_cache_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data_cache")

    @property
    def is_paper_trading(self) -> bool:
        return not self.live_trading_enabled


def get_settings() -> Settings:
    """Load settings from .env file and return a Settings instance."""
    env_path = PROJECT_ROOT / ".env"
    load_dotenv(env_path)

    def _bool(key: str, default: bool) -> bool:
        val = os.getenv(key, str(default)).lower()
        return val in ("true", "1", "yes")

    def _int(key: str, default: int) -> int:
        return int(os.getenv(key, str(default)))

    def _float(key: str, default: float) -> float:
        return float(os.getenv(key, str(default)))

    def _str(key: str, default: str) -> str:
        return os.getenv(key, default)

    settings = Settings(
        opend_host=_str("OPEND_HOST", "127.0.0.1"),
        opend_port=_int("OPEND_PORT", 11111),
        live_trading_enabled=_bool("LIVE_TRADING_ENABLED", False),
        trading_market=_str("TRADING_MARKET", "US"),
        trade_password=_str("MOOMOO_TRADE_PASSWORD", ""),
        max_position_size_pct=_float("MAX_POSITION_SIZE_PCT", 0.05),
        max_position_size_usd=_float("MAX_POSITION_SIZE_USD", 5000.0),
        max_daily_loss_pct=_float("MAX_DAILY_LOSS_PCT", 0.02),
        max_open_trades=_int("MAX_OPEN_TRADES", 5),
        max_ticker_concentration=_float("MAX_TICKER_CONCENTRATION", 0.10),
        cooldown_after_consecutive_losses=_int("COOLDOWN_AFTER_CONSECUTIVE_LOSSES", 3),
        cooldown_duration_minutes=_int("COOLDOWN_DURATION_MINUTES", 30),
        min_volume_filter=_int("MIN_VOLUME_FILTER", 500000),
        max_spread_pct=_float("MAX_SPREAD_PCT", 0.005),
        kill_switch=_bool("KILL_SWITCH", False),
        log_level=_str("LOG_LEVEL", "INFO"),
    )

    # Ensure directories exist
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.data_cache_dir.mkdir(parents=True, exist_ok=True)

    return settings
