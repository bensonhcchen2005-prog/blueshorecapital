"""
Kill switch — emergency halt for all trading activity.
Can be triggered manually or automatically.
"""

import logging
from datetime import datetime
from typing import Optional


logger = logging.getLogger(__name__)


class KillSwitch:
    """Emergency stop mechanism."""

    def __init__(self):
        self._active = False
        self._triggered_at: Optional[datetime] = None
        self._reason: str = ""

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def reason(self) -> str:
        return self._reason

    def activate(self, reason: str = "Manual activation") -> None:
        """Activate the kill switch. All trading halts immediately."""
        self._active = True
        self._triggered_at = datetime.now()
        self._reason = reason
        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")

    def deactivate(self) -> None:
        """Deactivate the kill switch. Requires explicit action."""
        self._active = False
        self._reason = ""
        logger.warning("Kill switch deactivated — trading may resume")

    def status(self) -> dict:
        return {
            "active": self._active,
            "triggered_at": str(self._triggered_at) if self._triggered_at else None,
            "reason": self._reason,
        }
