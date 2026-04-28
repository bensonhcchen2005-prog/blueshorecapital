"""
Signal filters — additional pre-execution filtering.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List

from strategy.base import Signal


logger = logging.getLogger(__name__)


def filter_stale_signals(
    signals: List[Signal], max_age_seconds: int = 300
) -> List[Signal]:
    """Remove signals older than max_age_seconds."""
    cutoff = datetime.now() - timedelta(seconds=max_age_seconds)
    fresh = [s for s in signals if s.timestamp >= cutoff]
    removed = len(signals) - len(fresh)
    if removed:
        logger.debug(f"Filtered {removed} stale signals (>{max_age_seconds}s old)")
    return fresh


def filter_duplicate_tickers(signals: List[Signal]) -> List[Signal]:
    """Keep only the strongest signal per ticker."""
    best: Dict[str, Signal] = {}
    for sig in signals:
        if sig.code not in best or sig.strength > best[sig.code].strength:
            best[sig.code] = sig
    return list(best.values())
