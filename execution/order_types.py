"""Order-related data classes."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class OrderResult:
    """Result from placing an order."""
    success: bool
    order_id: str = ""
    code: str = ""
    side: str = ""
    quantity: int = 0
    price: float = 0.0
    status: str = ""
    message: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
