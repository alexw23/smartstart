"""Blackout windows read from Home Assistant calendars.

Some tariffs publish periods when you're paid to use less (Octopus Octoplus
power-down and saving sessions). A cycle that straddles one can't be paused on
most appliances, so the scorer treats them as spans a window must not overlap.

The source keeps a cached list of upcoming spans, refreshed on a timer and
whenever a watched calendar changes state; scoring only ever reads the cache
(`spans`), so it stays synchronous. Any calendar entity works — the Octopus
ones are just the default.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Callable, List, Optional, Sequence, Tuple

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# How far ahead to read. Rates only extend ~48 h, so a window beyond this
# can't be chosen anyway.
LOOKAHEAD = timedelta(days=3)
REFRESH_INTERVAL = timedelta(minutes=10)
# After a failed read (typically HA starting up: the calendar integration isn't
# loaded yet), retry quickly instead of waiting for the next interval.
RETRY_DELAY = 30

Span = Tuple[datetime, datetime]


def autodetect_calendars(hass: HomeAssistant) -> List[str]:
    """Octopus Energy's Octoplus power-down / saving-session calendars, if the
    integration is installed. Matched on unique_id so renamed entities still
    work."""
    from homeassistant.helpers import entity_registry as er

    found = []
    for entry in er.async_get(hass).entities.values():
        if entry.domain != "calendar" or entry.platform != "octopus_energy":
            continue
        uid = (entry.unique_id or "").lower()
        if "octoplus_power_down" in uid or "octoplus_saving_session" in uid:
            found.append(entry.entity_id)
    return sorted(found)


def _to_dt(value) -> Optional[datetime]:
    """Calendar event bounds arrive as ISO strings: datetimes for timed events,
    plain dates for all-day ones (treated as the whole local day)."""
    if isinstance(value, datetime):
        return dt_util.as_utc(value) if value.tzinfo else dt_util.as_utc(value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE))
    if isinstance(value, date):
        return dt_util.as_utc(dt_util.start_of_local_day(value))
    if isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
        if parsed is not None:
            return _to_dt(parsed)
        day = dt_util.parse_date(value)
        if day is not None:
            return _to_dt(day)
    return None


def merge_spans(spans: Sequence[Span]) -> List[Span]:
    """Sort and merge overlapping or touching spans."""
    merged: List[Span] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


class BlackoutSource:
    """Cached upcoming blackout spans from one or more calendar entities."""

    def __init__(self, hass: HomeAssistant, entity_ids: Sequence[str]) -> None:
        self._hass = hass
        self._entity_ids = list(dict.fromkeys(entity_ids))
        self._spans: List[Span] = []
        self._on_change: Optional[Callable[[], None]] = None
        self._unsubs: List[Callable[[], None]] = []
        self._retry_unsub: Optional[Callable[[], None]] = None

    @property
    def entity_ids(self) -> List[str]:
        return list(self._entity_ids)

    def set_on_change(self, cb: Callable[[], None]) -> None:
        """Called (on the event loop) when the span list changes, so scoring
        can be redone against it."""
        self._on_change = cb

    async def async_start(self) -> None:
        if not self._entity_ids:
            return
        await self.async_refresh()
        self._unsubs.append(
            async_track_time_interval(self._hass, self._tick, REFRESH_INTERVAL)
        )
        self._unsubs.append(
            async_track_state_change_event(self._hass, self._entity_ids, self._calendar_changed)
        )

    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_retry()

    def _cancel_retry(self) -> None:
        if self._retry_unsub is not None:
            self._retry_unsub()
            self._retry_unsub = None

    @callback
    def _retry(self, _now) -> None:
        self._retry_unsub = None
        self._hass.async_create_task(self.async_refresh())

    async def _tick(self, _now) -> None:
        await self.async_refresh()

    @callback
    def _calendar_changed(self, _event) -> None:
        self._hass.async_create_task(self.async_refresh())

    async def async_refresh(self) -> None:
        now = dt_util.utcnow()
        collected: List[Span] = []
        failed = False
        for entity_id in self._entity_ids:
            try:
                resp = await self._hass.services.async_call(
                    "calendar", "get_events",
                    {
                        "entity_id": entity_id,
                        "start_date_time": now - timedelta(hours=12),
                        "end_date_time": now + LOOKAHEAD,
                    },
                    blocking=True, return_response=True,
                )
            except Exception as exc:  # noqa: BLE001 — a calendar outage must not break scoring
                _LOGGER.warning("SmartStart: couldn't read blackout calendar %s: %s", entity_id, exc)
                failed = True
                continue
            for event in ((resp or {}).get(entity_id) or {}).get("events", []):
                start, end = _to_dt(event.get("start")), _to_dt(event.get("end"))
                if start is not None and end is not None:
                    collected.append((start, end))
        self._cancel_retry()
        if failed:
            self._retry_unsub = async_call_later(self._hass, RETRY_DELAY, self._retry)
        # Total failure (nothing readable) keeps the last good spans rather than
        # wiping them; the retry above fills things in once calendars are up.
        if failed and not collected:
            return
        spans = merge_spans(collected)
        if spans != self._spans:
            self._spans = spans
            _LOGGER.info(
                "SmartStart: %d blackout span(s) from %s%s",
                len(spans), ", ".join(self._entity_ids),
                "".join(f"\n  {s:%a %H:%M} → {e:%H:%M}" for s, e in spans[:6]),
            )
            if self._on_change:
                self._on_change()

    def spans(self, now: datetime) -> List[Span]:
        """Cached spans that haven't finished yet."""
        return [(s, e) for s, e in self._spans if e > now]
