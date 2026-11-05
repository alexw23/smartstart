"""Shared fixtures for SmartStart tests."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.smartstart.providers.base import RatePeriod


# ── Rate helpers ──────────────────────────────────────────────────────────────

MIDNIGHT = datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc)


def make_rates(
    rates: list[float],
    start: datetime = MIDNIGHT,
    slot_minutes: int = 30,
) -> list[RatePeriod]:
    """Build a list of contiguous RatePeriod objects from a flat list of rates."""
    periods = []
    for i, rate in enumerate(rates):
        s = start + timedelta(minutes=i * slot_minutes)
        e = s + timedelta(minutes=slot_minutes)
        periods.append(RatePeriod(start=s, end=e, rate_cost_per_kwh=rate))
    return periods


# ── Mock CurveStore ───────────────────────────────────────────────────────────

@pytest.fixture
def mock_curve_store():
    """A CurveStore with no data (no curves learned yet)."""
    store = MagicMock()
    store.get_confidence_info.return_value = {
        "n_curves": 0,
        "confidence": 0.0,
        "status": "waiting",
        "last_captured_at": None,
        "warning": False,
        "profile_type": "unknown",
        "avg_duration_minutes": None,
        "duration_std_minutes": None,
    }
    store.get_profile.return_value = None
    store.get_avg_duration_minutes.return_value = None
    return store


# ── Minimal device / hass mocks ───────────────────────────────────────────────

@pytest.fixture
def mock_hass():
    hass = MagicMock()
    hass.states.get.return_value = None
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro)
    return hass


@pytest.fixture
def sample_device():
    return {
        "device_slug":      "washer",
        "name":             "Washing Machine",
        "start_service":    "script.start_washer",
        "stop_service":   "",
        "delay_handling":   "start_delay",
        "min_delay_minutes": 0,
        "max_delay_minutes": 1440,
        "running_entity":   "binary_sensor.washer_running",
        "running_states":   ["on"],
        "power_sensor":     "",
        "cycles": [
            {
                "cycle_id":         "eco_40",
                "name":             "Eco 40",
                "duration_minutes": 90,
                "total_kwh":        0.8,
            }
        ],
    }
