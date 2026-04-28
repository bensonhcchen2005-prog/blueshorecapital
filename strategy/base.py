"""
Base strategy interface and Signal data class.
All strategies inherit from BaseStrategy.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import pandas as pd


class Direction(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class Signal:
    """Represents a trading signal emitted by a strategy."""
    code: str                          # ticker code (e.g., "US.AAPL")
    direction: Direction               # LONG or SHORT
    strength: float                    # 0.0 to 1.0
    strategy_name: str                 # which strategy generated this
    entry_price: float                 # suggested entry price
    stop_loss: float = 0.0            # suggested stop loss
    take_profit: float = 0.0          # suggested take profit
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)  # strategy-specific context

    @property
    def is_valid(self) -> bool:
        return 0.0 < self.strength <= 1.0 and self.entry_price > 0


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies."""

    name: str = "base"

    @abstractmethod
    def evaluate(self, code: str, data: pd.DataFrame) -> Optional[Signal]:
        """
        Evaluate the strategy on enriched OHLCV data.

        Args:
            code: Ticker code
            data: DataFrame with OHLCV + indicator columns

        Returns:
            Signal if conditions are met, None otherwise.
        """

    @abstractmethod
    def invalidation(self, data: pd.DataFrame, signal: Signal) -> bool:
        """
        Check if an existing signal has been invalidated.

        Returns:
            True if signal is no longer valid (should exit or cancel).
        """

    def required_bars(self) -> int:
        """Minimum number of bars needed for this strategy to evaluate."""
        return 50
