"""
Strategy registry — discover and manage active strategies.
"""

from typing import Dict, List, Optional

from strategy.base import BaseStrategy
from strategy.ma_crossover import MACrossoverStrategy
from strategy.mean_reversion import MeanReversionStrategy
from strategy.breakout import BreakoutStrategy
from strategy.momentum_strategy import MomentumStrategy
from strategy.short_momentum import ShortMomentumStrategy
from strategy.rsi_divergence import RSIDivergenceStrategy
from strategy.gap_reversal import GapReversalStrategy


# All available strategies
_STRATEGIES: Dict[str, type] = {
    "ma_crossover": MACrossoverStrategy,
    "mean_reversion": MeanReversionStrategy,
    "breakout": BreakoutStrategy,
    "momentum": MomentumStrategy,
    "short_momentum": ShortMomentumStrategy,
    "rsi_divergence": RSIDivergenceStrategy,
    "gap_reversal": GapReversalStrategy,
}


class StrategyRegistry:
    """Manages which strategies are active and instantiates them."""

    def __init__(self):
        self._active: Dict[str, BaseStrategy] = {}

    def register(self, name: str, **kwargs) -> None:
        """Activate a strategy by name with optional config."""
        if name not in _STRATEGIES:
            raise ValueError(f"Unknown strategy: {name}. Available: {list(_STRATEGIES)}")
        self._active[name] = _STRATEGIES[name](**kwargs)

    def register_all(self) -> None:
        """Activate all available strategies with defaults."""
        for name, cls in _STRATEGIES.items():
            self._active[name] = cls()

    def get_strategies_for_ticker(
        self, allowed: Optional[List[str]] = None
    ) -> List[BaseStrategy]:
        """
        Return active strategies, optionally filtered by allowed names.
        """
        if allowed is None:
            return list(self._active.values())
        return [s for name, s in self._active.items() if name in allowed]

    @property
    def available(self) -> List[str]:
        return list(_STRATEGIES.keys())

    @property
    def active(self) -> List[str]:
        return list(self._active.keys())
