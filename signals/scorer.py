"""
Signal scoring and ranking.
Aggregates signals from multiple strategies, resolves conflicts, ranks by conviction.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List

from strategy.base import Signal


logger = logging.getLogger(__name__)


@dataclass
class ScoredSignal:
    """A signal with an aggregate score and confluence info."""
    signal: Signal
    final_score: float          # weighted composite score
    confluence_count: int       # how many strategies agree
    confluence_bonus: float     # extra score from agreement


class SignalScorer:
    """Score and rank trading signals."""

    def __init__(
        self,
        min_score_threshold: float = 0.3,
        confluence_weight: float = 0.2,
        max_signals_per_ticker: int = 1,
    ):
        self.min_score_threshold = min_score_threshold
        self.confluence_weight = confluence_weight
        self.max_signals_per_ticker = max_signals_per_ticker

    def score_and_rank(self, signals: List[Signal]) -> List[ScoredSignal]:
        """
        Score all signals, apply confluence bonuses, filter, and rank.

        Returns:
            List of ScoredSignal sorted by final_score descending.
        """
        if not signals:
            return []

        # Group by ticker + direction for confluence detection
        groups: Dict[str, List[Signal]] = {}
        for sig in signals:
            key = f"{sig.code}:{sig.direction.value}"
            groups.setdefault(key, []).append(sig)

        scored: List[ScoredSignal] = []
        for key, group in groups.items():
            confluence = len(group)

            # Pick the strongest signal in the group
            best = max(group, key=lambda s: s.strength)

            # Confluence bonus: multiple strategies agree
            bonus = min((confluence - 1) * self.confluence_weight, 0.4)
            final_score = min(best.strength + bonus, 1.0)

            if final_score < self.min_score_threshold:
                logger.debug(
                    f"Signal for {best.code} below threshold "
                    f"({final_score:.2f} < {self.min_score_threshold})"
                )
                continue

            scored.append(ScoredSignal(
                signal=best,
                final_score=round(final_score, 3),
                confluence_count=confluence,
                confluence_bonus=round(bonus, 3),
            ))

        # Sort by score descending
        scored.sort(key=lambda s: s.final_score, reverse=True)

        # Limit signals per ticker
        seen: Dict[str, int] = {}
        filtered: List[ScoredSignal] = []
        for s in scored:
            count = seen.get(s.signal.code, 0)
            if count < self.max_signals_per_ticker:
                filtered.append(s)
                seen[s.signal.code] = count + 1

        logger.info(f"Scored {len(signals)} signals -> {len(filtered)} actionable")
        return filtered
