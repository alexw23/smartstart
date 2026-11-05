from typing import List

from .base import RateProvider, RatePeriod


class ManualProvider(RateProvider):
    """Stores rates pushed via the smartstart.publish_rates service call.

    Any automation or script can push rate data — solar export, custom
    tariffs, future provider integrations — by calling that service with a
    list of {start, end, rate_cost_per_kwh} dicts.
    """

    def __init__(self):
        self._rates: List[RatePeriod] = []

    @property
    def name(self) -> str:
        return "Manual"

    def set_rates(self, rates: List[RatePeriod]) -> None:
        self._rates = sorted(rates, key=lambda x: x.start)

    async def async_get_rates(self) -> List[RatePeriod]:
        return list(self._rates)
