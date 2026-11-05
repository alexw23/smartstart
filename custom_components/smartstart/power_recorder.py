"""Samples a power sensor at a fixed interval while a cycle is running."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import CURVE_SAMPLE_INTERVAL_S

_LOGGER = logging.getLogger(__name__)


class PowerRecorder:
    """Samples a HA power entity at a fixed interval. Call start() / stop()."""

    def __init__(self, hass: HomeAssistant, power_entity: str) -> None:
        self._hass         = hass
        self._entity       = power_entity
        self._samples: List[dict] = []
        self._start_ts: float     = 0.0
        self._paused_total_s: float = 0.0  # accumulated paused duration
        self._paused_since: Optional[float] = None
        self._cancel: Optional[Callable] = None

    def start(self) -> None:
        if self._cancel:
            return
        self._start_ts = datetime.now(timezone.utc).timestamp()
        self._samples  = []
        self._paused_total_s = 0.0
        self._paused_since = None
        self._record()  # immediate first reading

        @callback
        def _tick(_now: datetime) -> None:
            self._record()

        self._cancel = async_track_time_interval(
            self._hass,
            _tick,
            timedelta(seconds=CURVE_SAMPLE_INTERVAL_S),
        )
        _LOGGER.debug("SmartStart: power recorder started for %s", self._entity)

    def pause(self) -> None:
        """Suspend sampling. kWh/duration calcs exclude paused intervals."""
        if self._cancel is None or self._paused_since is not None:
            return
        self._record()  # take a final pre-pause reading
        self._cancel()
        self._cancel = None
        self._paused_since = datetime.now(timezone.utc).timestamp()
        _LOGGER.debug("SmartStart: power recorder paused for %s", self._entity)

    def resume(self) -> None:
        """Resume sampling after pause(). No-op if not paused."""
        if self._paused_since is None:
            return
        self._paused_total_s += datetime.now(timezone.utc).timestamp() - self._paused_since
        self._paused_since = None
        self._record()  # immediate post-resume reading

        @callback
        def _tick(_now: datetime) -> None:
            self._record()

        self._cancel = async_track_time_interval(
            self._hass,
            _tick,
            timedelta(seconds=CURVE_SAMPLE_INTERVAL_S),
        )
        _LOGGER.debug(
            "SmartStart: power recorder resumed for %s (paused total: %.0fs)",
            self._entity, self._paused_total_s,
        )

    def stop(self) -> List[dict]:
        """Stop recording and return all captured samples (may be empty)."""
        if self._paused_since is not None:
            # Account for time spent paused at the moment we stopped
            self._paused_total_s += datetime.now(timezone.utc).timestamp() - self._paused_since
            self._paused_since = None
        if self._cancel:
            self._cancel()
            self._cancel = None
            self._record()  # final reading — only if not paused at stop time

        samples = self._samples
        self._samples = []

        _LOGGER.debug(
            "SmartStart: power recorder stopped for %s — %d samples captured",
            self._entity, len(samples),
        )
        return samples

    @property
    def current_samples(self) -> list:
        """Return a snapshot of samples recorded so far (read-only copy)."""
        return list(self._samples)

    @property
    def paused_total_seconds(self) -> float:
        """Total seconds spent paused since start()."""
        if self._paused_since is not None:
            return self._paused_total_s + (
                datetime.now(timezone.utc).timestamp() - self._paused_since
            )
        return self._paused_total_s

    def _record(self) -> None:
        state = self._hass.states.get(self._entity)
        if state is None:
            return
        try:
            power_w = float(state.state)
        except (ValueError, TypeError):
            return
        # t_s tracks elapsed *running* time only — paused intervals are excluded
        now = datetime.now(timezone.utc)
        wall = now.timestamp() - self._start_ts
        self._samples.append({
            "t_s":     wall - self._paused_total_s,
            "power_w": power_w,
            # Wall-clock timestamp lets cost lookups find the rate slot in effect
            # when this energy was actually consumed (not when scheduled)
            "wall_ts": now.isoformat(),
        })
