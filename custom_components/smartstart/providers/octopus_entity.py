from datetime import datetime, timezone
from typing import List, Optional
import logging
import re

from homeassistant.helpers.storage import Store

from .base import RateProvider, RatePeriod

_LOGGER = logging.getLogger(__name__)

_CACHE_VERSION = 1


class OctopusEntityProvider(RateProvider):
    """Read rates from the Octopus Energy HA integration event entities."""

    def __init__(self, hass, current_day_entity: str, next_day_entity: str):
        self._hass = hass
        self._current_day_entity = current_day_entity
        self._next_day_entity = next_day_entity
        # Last good slots, persisted. After an HA restart the Octopus event
        # entities come back WITHOUT their `rates` attribute until Octopus next
        # fires them (up to ~30 min) — published Agile prices don't change, so
        # the cached future slots stand in until then.
        slug = re.sub(r"[^a-z0-9]+", "_", current_day_entity.lower()).strip("_")
        self._cache = Store(hass, _CACHE_VERSION, f"smartstart_rates_cache_{slug}")
        self._cached: Optional[List[RatePeriod]] = None
        self._saved_sig: Optional[tuple] = None

    @property
    def name(self) -> str:
        return "Octopus Energy"

    async def async_get_rates(self) -> List[RatePeriod]:
        rates: List[RatePeriod] = []
        seen_starts: set = set()

        for entity_id in [self._current_day_entity, self._next_day_entity]:
            state = self._hass.states.get(entity_id)
            if state is None:
                _LOGGER.warning("SmartStart: entity %s not found", entity_id)
                continue

            raw = (state.attributes or {}).get("rates") or []
            _LOGGER.debug("SmartStart: %s → %d raw slots", entity_id, len(raw))

            for r in raw:
                try:
                    start_raw = r["start"]
                    end_raw = r["end"]
                    start_dt = (
                        start_raw
                        if isinstance(start_raw, datetime)
                        else datetime.fromisoformat(str(start_raw))
                    )
                    end_dt = (
                        end_raw
                        if isinstance(end_raw, datetime)
                        else datetime.fromisoformat(str(end_raw))
                    )
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=timezone.utc)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)

                    val = float(r["value_inc_vat"])

                    if start_dt not in seen_starts:
                        seen_starts.add(start_dt)
                        rates.append(
                            RatePeriod(start=start_dt, end=end_dt, rate_cost_per_kwh=val)
                        )
                except (KeyError, ValueError, TypeError) as exc:
                    _LOGGER.warning("SmartStart: skipping bad rate slot %s: %s", r, exc)

        rates.sort(key=lambda x: x.start)
        if rates:
            await self._save_cache(rates)
        else:
            rates = await self._load_cache()
            if rates:
                _LOGGER.info(
                    "SmartStart: OctopusEntityProvider — entities have no rates yet "
                    "(just restarted?); using %d cached slots until %s",
                    len(rates), rates[-1].end.strftime("%a %H:%M"),
                )
                return rates
        if rates:
            _LOGGER.info(
                "SmartStart: OctopusEntityProvider — %d slots loaded "
                "(%.5f – %.5f, cheapest %.5f @ %s)",
                len(rates),
                rates[0].rate_cost_per_kwh,
                rates[-1].rate_cost_per_kwh,
                min(r.rate_cost_per_kwh for r in rates),
                min(rates, key=lambda r: r.rate_cost_per_kwh).start.strftime("%H:%M"),
            )
        else:
            _LOGGER.warning(
                "SmartStart: OctopusEntityProvider — no slots from %s or %s "
                "(entities may not be loaded yet)",
                self._current_day_entity, self._next_day_entity,
            )
        return rates

    async def _save_cache(self, rates: List[RatePeriod]) -> None:
        sig = (len(rates), rates[0].start, rates[-1].end)
        self._cached = rates
        if sig == self._saved_sig:
            return
        self._saved_sig = sig
        await self._cache.async_save({"rates": [
            {"start": r.start.isoformat(), "end": r.end.isoformat(), "rate": r.rate_cost_per_kwh}
            for r in rates
        ]})

    async def _load_cache(self) -> List[RatePeriod]:
        if self._cached is None:
            raw = await self._cache.async_load() or {}
            cached = []
            for r in raw.get("rates") or []:
                try:
                    cached.append(RatePeriod(
                        start=datetime.fromisoformat(r["start"]),
                        end=datetime.fromisoformat(r["end"]),
                        rate_cost_per_kwh=float(r["rate"]),
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
            self._cached = cached
        now = datetime.now(timezone.utc)
        return [r for r in self._cached if r.end > now]
