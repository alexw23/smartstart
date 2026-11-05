from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass
class RatePeriod:
    start: datetime
    end: datetime
    rate_cost_per_kwh: float
    # Forecast band (predicted slots only): low/high bounds around the
    # central estimate. None for real rates or a source without bands.
    rate_low: Optional[float] = None
    rate_high: Optional[float] = None

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("RatePeriod datetimes must be timezone-aware")


class RateProvider(ABC):
    """Abstract base — any source of electricity rate periods."""

    @abstractmethod
    async def async_get_rates(self) -> List[RatePeriod]:
        """Return rate periods sorted ascending by start time."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name."""
