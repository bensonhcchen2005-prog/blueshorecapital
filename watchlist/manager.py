"""
Watchlist manager — loads ticker universe and filters by criteria.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml


logger = logging.getLogger(__name__)


@dataclass
class WatchlistItem:
    code: str
    name: str
    sector: str
    strategies: List[str]


class WatchlistManager:
    """Load and filter the trading watchlist."""

    def __init__(self, watchlist_path: Optional[Path] = None):
        if watchlist_path is None:
            watchlist_path = Path(__file__).parent.parent / "config" / "watchlist.yaml"
        self._path = watchlist_path
        self._items: List[WatchlistItem] = []
        self._load()

    def _load(self) -> None:
        with open(self._path) as f:
            raw = yaml.safe_load(f)

        for entry in raw.get("tickers", []):
            self._items.append(WatchlistItem(
                code=entry["code"],
                name=entry.get("name", ""),
                sector=entry.get("sector", ""),
                strategies=entry.get("strategies", []),
            ))

        logger.info(f"Watchlist loaded: {len(self._items)} tickers")

    @property
    def codes(self) -> List[str]:
        return [item.code for item in self._items]

    @property
    def items(self) -> List[WatchlistItem]:
        return self._items

    def get_strategies_for(self, code: str) -> List[str]:
        """Return strategy names allowed for a given ticker."""
        for item in self._items:
            if item.code == code:
                return item.strategies
        return []

    def filter_by_sector(self, sector: str) -> List[WatchlistItem]:
        return [item for item in self._items if item.sector == sector]
