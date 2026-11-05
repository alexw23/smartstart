"""Tests for scheduler.py — covers delay recalculation, button handling, state transitions."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from custom_components.smartstart.const import (
    DELAY_HANDLING_END_TIME,
    DELAY_HANDLING_MANAGED,
    DELAY_HANDLING_START_DELAY,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_SCHEDULED,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from custom_components.smartstart.scheduler import SmartStartScheduler
from custom_components.smartstart.window_scorer import ScoredWindow


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_window(
    start_offset_minutes: int,
    duration_minutes: int = 90,
    rate: float = 0.10,
    stale_delay_minutes: int = None,
) -> ScoredWindow:
    """Create a ScoredWindow with a given start offset from now."""
    now = datetime.now(timezone.utc)
    start = now + timedelta(minutes=start_offset_minutes)
    end   = start + timedelta(minutes=duration_minutes)
    # stale_delay_minutes simulates a window scored by the coordinator some time ago
    delay = stale_delay_minutes if stale_delay_minutes is not None else max(0, start_offset_minutes)
    return ScoredWindow(
        start=start,
        end=end,
        avg_rate_cost_per_kwh=rate,
        delay_minutes=delay,
        n_slots=duration_minutes // 30,
    )


def _make_scheduler(hass, devices: list[dict], curve_store=None) -> SmartStartScheduler:
    with patch("custom_components.smartstart.scheduler.Store") as MockStore:
        store = MagicMock()
        store.async_load  = AsyncMock(return_value=None)
        store.async_save  = AsyncMock()
        MockStore.return_value = store
        sched = SmartStartScheduler(hass, devices, curve_store)
    return sched


@pytest.fixture
def mock_hass():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task   = MagicMock(side_effect=lambda c: c)
    return hass


@pytest.fixture
def native_device():
    return {
        "device_slug":      "washer",
        "name":             "Washer",
        "start_service":    "script.start_washer",
        "stop_service":   "",
        "delay_handling":   DELAY_HANDLING_START_DELAY,
        "min_delay_minutes": 0,
        "max_delay_minutes": 1440,
        "running_entity":   "",
        "running_states":   ["on"],
        "power_sensor":     "",
        "cycles":           [{"cycle_key": "eco_40", "cycle_id": "eco_40", "name": "Eco 40", "duration_minutes": 90, "total_kwh": 0.8}],
    }


@pytest.fixture
def button_device():
    return {
        "device_slug":      "dishwasher",
        "name":             "Dishwasher",
        "start_service":    "button.start_dishwasher",
        "stop_service":   "",
        "delay_handling":   DELAY_HANDLING_START_DELAY,  # intentionally wrong for a button
        "min_delay_minutes": 0,
        "max_delay_minutes": 1440,
        "running_entity":   "",
        "running_states":   ["on"],
        "power_sensor":     "",
        "cycles":           [{"cycle_key": "eco_50", "cycle_id": "eco_50", "name": "Eco 50", "duration_minutes": 120, "total_kwh": 1.2}],
    }


@pytest.fixture
def managed_device():
    return {
        "device_slug":      "dryer",
        "name":             "Dryer",
        "start_service":    "script.start_dryer",
        "stop_service":   "",
        "delay_handling":   DELAY_HANDLING_MANAGED,
        "min_delay_minutes": 0,
        "max_delay_minutes": 1440,
        "running_entity":   "",
        "running_states":   ["on"],
        "power_sensor":     "",
        "cycles":           [{"cycle_key": "normal", "cycle_id": "normal", "name": "Normal", "duration_minutes": 90, "total_kwh": 3.0}],
    }


@pytest.fixture
def end_time_device():
    """LG-style appliance — max_delay represents latest finish-from-now."""
    return {
        "device_slug":      "lg_dryer",
        "name":             "LG Dryer",
        "start_service":    "script.start_lg_dryer",
        "stop_service":   "",
        "delay_handling":   DELAY_HANDLING_END_TIME,
        "min_delay_minutes": 0,
        "max_delay_minutes": 1140,   # 19h — LG's max reservation
        "running_entity":   "",
        "running_states":   ["on"],
        "power_sensor":     "",
        "cycles":           [{"cycle_key": "eco", "cycle_id": "eco", "name": "Eco", "duration_minutes": 150, "total_kwh": 2.0}],
    }


# ── Delay recalculation ───────────────────────────────────────────────────────

class TestDelayRecalculation:
    async def test_future_window_passes_correct_delay(self, mock_hass, native_device):
        """Window starting in 60 min → delay_seconds should be ~3600."""
        sched = _make_scheduler(mock_hass, [native_device])
        window = _make_window(start_offset_minutes=60, stale_delay_minutes=60)

        await sched.async_schedule(
            device_slug="washer",
            cycle_id="eco_40",
            window=window,
            cycle_kwh=0.8,
            now_rate=0.20,
            start_service="script.start_washer",
            service_payload={"delay_seconds": window.delay_minutes * 60, "cycle_id": "eco_40"},
        )

        call_args = mock_hass.services.async_call.call_args
        assert call_args is not None
        payload = call_args[0][2]  # (domain, service, payload)
        assert payload["delay_seconds"] == pytest.approx(3600, abs=5)

    async def test_stale_window_in_past_gives_zero_delay(self, mock_hass, native_device):
        """Window.start is 30 min in the past (stale coordinator data) → delay_seconds=0."""
        sched = _make_scheduler(mock_hass, [native_device])
        # Window start is 30 min ago; stale delay_minutes recorded as 60 (from before coordinator ran)
        window = _make_window(start_offset_minutes=-30, stale_delay_minutes=60)

        await sched.async_schedule(
            device_slug="washer",
            cycle_id="eco_40",
            window=window,
            cycle_kwh=0.8,
            now_rate=0.20,
            start_service="script.start_washer",
            service_payload={"delay_seconds": 3600, "cycle_id": "eco_40"},  # stale payload
        )

        call_args = mock_hass.services.async_call.call_args
        payload = call_args[0][2]
        assert payload["delay_seconds"] == 0  # recalculated, not stale

    async def test_schedule_state_stores_fresh_delay(self, mock_hass, native_device):
        """The stored schedule should reflect the freshly computed delay, not stale."""
        sched = _make_scheduler(mock_hass, [native_device])
        window = _make_window(start_offset_minutes=45, stale_delay_minutes=90)

        await sched.async_schedule(
            device_slug="washer",
            cycle_id="eco_40",
            window=window,
            cycle_kwh=0.8,
            now_rate=0.20,
            start_service="script.start_washer",
            service_payload={"delay_seconds": 5400, "cycle_id": "eco_40"},
        )

        stored = sched.get_schedule("washer")
        # Stored delay should be ~2700s (45 min), not 5400s (stale 90 min)
        assert stored["delay_seconds"] == pytest.approx(45 * 60, abs=5)


# ── Button handling ───────────────────────────────────────────────────────────

class TestButtonHandling:
    async def test_button_pressed_via_button_press_service(self, mock_hass, button_device):
        """start_service=button.* → must call button.press with entity_id, not button.<name>."""
        sched = _make_scheduler(mock_hass, [button_device])
        window = _make_window(start_offset_minutes=0)

        await sched.async_schedule(
            device_slug="dishwasher",
            cycle_id="eco_50",
            window=window,
            cycle_kwh=1.2,
            now_rate=0.20,
            start_service="button.start_dishwasher",
            service_payload={"delay_seconds": 0, "cycle_id": "eco_50"},
        )

        mock_hass.services.async_call.assert_called_once_with(
            "button", "press", {"entity_id": "button.start_dishwasher"}, blocking=False
        )

    async def test_button_with_delay_upgrades_to_managed(self, mock_hass, button_device):
        """button + delay_seconds > 0 + native mode → auto-upgrade to managed (register timer)."""
        sched = _make_scheduler(mock_hass, [button_device])
        window = _make_window(start_offset_minutes=90, stale_delay_minutes=90)

        with patch.object(sched, "_register_managed_timer") as mock_timer:
            await sched.async_schedule(
                device_slug="dishwasher",
                cycle_id="eco_50",
                window=window,
                cycle_kwh=1.2,
                now_rate=0.20,
                start_service="button.start_dishwasher",
                service_payload={"delay_seconds": 5400, "cycle_id": "eco_50"},
            )
            # Should register a timer, NOT call the button immediately
            mock_timer.assert_called_once_with("dishwasher", window.start)
        mock_hass.services.async_call.assert_not_called()

    async def test_button_zero_delay_pressed_immediately(self, mock_hass, button_device):
        """button + delay=0 + native mode → press immediately (no upgrade needed)."""
        sched = _make_scheduler(mock_hass, [button_device])
        window = _make_window(start_offset_minutes=0, stale_delay_minutes=0)

        await sched.async_schedule(
            device_slug="dishwasher",
            cycle_id="eco_50",
            window=window,
            cycle_kwh=1.2,
            now_rate=0.20,
            start_service="button.start_dishwasher",
            service_payload={"delay_seconds": 0, "cycle_id": "eco_50"},
        )

        mock_hass.services.async_call.assert_called_once_with(
            "button", "press", {"entity_id": "button.start_dishwasher"}, blocking=False
        )


# ── Script / native mode ──────────────────────────────────────────────────────

class TestNativeMode:
    async def test_script_called_immediately_with_payload(self, mock_hass, native_device):
        sched = _make_scheduler(mock_hass, [native_device])
        window = _make_window(start_offset_minutes=60)

        await sched.async_schedule(
            device_slug="washer",
            cycle_id="eco_40",
            window=window,
            cycle_kwh=0.8,
            now_rate=0.20,
            start_service="script.start_washer",
            service_payload={"delay_seconds": 3600, "cycle_id": "eco_40"},
        )

        mock_hass.services.async_call.assert_called_once()
        args = mock_hass.services.async_call.call_args[0]
        assert args[0] == "script"
        assert args[1] == "start_washer"
        assert "delay_seconds" in args[2]

    async def test_schedule_status_set_to_scheduled(self, mock_hass, native_device):
        sched = _make_scheduler(mock_hass, [native_device])
        window = _make_window(start_offset_minutes=30)

        await sched.async_schedule(
            device_slug="washer", cycle_id="eco_40", window=window,
            cycle_kwh=0.8, now_rate=0.20, start_service="script.start_washer",
            service_payload={"delay_seconds": 1800},
        )

        assert sched.get_schedule("washer")["status"] == STATE_SCHEDULED


# ── Managed mode ──────────────────────────────────────────────────────────────

class TestManagedMode:
    async def test_managed_registers_timer_not_immediate_call(self, mock_hass, managed_device):
        sched = _make_scheduler(mock_hass, [managed_device])
        window = _make_window(start_offset_minutes=60)

        with patch.object(sched, "_register_managed_timer") as mock_timer:
            await sched.async_schedule(
                device_slug="dryer", cycle_id="normal", window=window,
                cycle_kwh=3.0, now_rate=0.20, start_service="script.start_dryer",
                service_payload={"delay_seconds": 3600},
            )
            mock_timer.assert_called_once_with("dryer", window.start)

        mock_hass.services.async_call.assert_not_called()


# ── Cancel ────────────────────────────────────────────────────────────────────

class TestCancel:
    async def test_cancel_sets_idle(self, mock_hass, native_device):
        sched = _make_scheduler(mock_hass, [native_device])
        window = _make_window(start_offset_minutes=0)
        await sched.async_schedule(
            device_slug="washer", cycle_id="eco_40", window=window,
            cycle_kwh=0.8, now_rate=0.20, start_service="script.start_washer",
            service_payload={"delay_seconds": 0},
        )
        assert sched.get_schedule("washer")["status"] == STATE_SCHEDULED

        await sched.async_cancel("washer")
        assert sched.get_schedule("washer")["status"] == STATE_IDLE

    async def test_cancel_clears_managed_timer(self, mock_hass, managed_device):
        sched = _make_scheduler(mock_hass, [managed_device])
        cancel_fn = MagicMock()
        sched._timers["dryer"] = cancel_fn

        await sched.async_cancel("dryer")
        cancel_fn.assert_called_once()
        assert "dryer" not in sched._timers


# ── Per-entry storage ─────────────────────────────────────────────────────────

class TestPerEntryStorage:
    async def test_load_migrates_own_devices_from_legacy_store(self, mock_hass, native_device):
        """Empty per-entry store + legacy shared store → migrate only own slugs."""
        per_entry = MagicMock()
        per_entry.async_load = AsyncMock(return_value=None)
        per_entry.async_save = AsyncMock()
        legacy = MagicMock()
        legacy.async_load = AsyncMock(return_value={"schedules": {
            "washer":     {"status": STATE_SCHEDULED, "cycle_id": "eco_40"},
            "dishwasher": {"status": STATE_SCHEDULED, "cycle_id": "eco_50"},
        }})
        mock_hass.async_create_task = MagicMock(side_effect=lambda c: c.close())

        with patch(
            "custom_components.smartstart.scheduler.Store",
            side_effect=[per_entry, legacy],
        ) as MockStore:
            sched = SmartStartScheduler(mock_hass, [native_device], None, entry_id="entry_a")
            await sched.async_load()

        # Store keyed per entry, not the shared legacy key
        assert MockStore.call_args_list[0][0][2] == f"{STORAGE_KEY}_entry_a"
        # Own device migrated; the other entry's device is not adopted
        assert sched.get_schedule("washer")["status"] == STATE_SCHEDULED
        assert sched.get_schedule("dishwasher")["status"] == STATE_IDLE
        # Migration is persisted so the legacy store isn't re-read next load
        per_entry.async_save.assert_called_once()

    async def test_load_filters_foreign_devices_from_own_store(self, mock_hass, native_device):
        """Slugs not owned by this entry are dropped on load."""
        per_entry = MagicMock()
        per_entry.async_load = AsyncMock(return_value={"schedules": {
            "washer": {"status": STATE_SCHEDULED, "cycle_id": "eco_40"},
            "ghost":  {"status": STATE_SCHEDULED},
        }})
        per_entry.async_save = AsyncMock()
        mock_hass.async_create_task = MagicMock(side_effect=lambda c: c.close())

        with patch("custom_components.smartstart.scheduler.Store", return_value=per_entry):
            sched = SmartStartScheduler(mock_hass, [native_device], None, entry_id="entry_a")
            await sched.async_load()

        assert sched.get_schedule("washer")["status"] == STATE_SCHEDULED
        assert sched.get_schedule("ghost")["status"] == STATE_IDLE


# ── State transitions ─────────────────────────────────────────────────────────

class TestStateTransitions:
    async def test_set_running_updates_status(self, mock_hass, native_device):
        sched = _make_scheduler(mock_hass, [native_device])
        sched._schedules["washer"] = {"status": STATE_SCHEDULED, "cycle_id": "eco_40"}

        await sched.async_set_running("washer")
        assert sched.get_schedule("washer")["status"] == STATE_RUNNING

    async def test_set_idle_updates_status(self, mock_hass, native_device):
        sched = _make_scheduler(mock_hass, [native_device])
        sched._schedules["washer"] = {"status": STATE_RUNNING, "cycle_id": "eco_40"}

        await sched.async_set_idle("washer")
        assert sched.get_schedule("washer")["status"] == STATE_IDLE

    async def test_savings_pct_zero_when_now_rate_negative(self, mock_hass, native_device):
        """savings_pct should be 0 when now_rate is negative (can't compute percentage)."""
        sched = _make_scheduler(mock_hass, [native_device])
        window = _make_window(start_offset_minutes=0, rate=-0.01)

        await sched.async_schedule(
            device_slug="washer", cycle_id="eco_40", window=window,
            cycle_kwh=0.8, now_rate=-0.02,  # negative now_rate
            start_service="script.start_washer",
            service_payload={"delay_seconds": 0},
        )

        stored = sched.get_schedule("washer")
        assert stored["savings_pct"] == 0


class TestDelayHandling:
    """delay_handling: start_delay / end_time / managed branching."""

    async def test_start_delay_calls_script_immediately(self, mock_hass, native_device):
        sched = _make_scheduler(mock_hass, [native_device])
        window = _make_window(start_offset_minutes=30, stale_delay_minutes=30)
        await sched.async_schedule(
            device_slug="washer", cycle_id="eco_40", window=window,
            cycle_kwh=0.8, now_rate=0.20,
            start_service="script.start_washer",
            service_payload={"delay_seconds": 1800, "cycle_id": "eco_40"},
        )
        mock_hass.services.async_call.assert_called_once()

    async def test_managed_registers_timer_no_immediate_call(self, mock_hass, managed_device):
        sched = _make_scheduler(mock_hass, [managed_device])
        window = _make_window(start_offset_minutes=60, stale_delay_minutes=60)
        with patch.object(sched, "_register_managed_timer") as mock_timer:
            await sched.async_schedule(
                device_slug="dryer", cycle_id="normal", window=window,
                cycle_kwh=3.0, now_rate=0.20,
                start_service="script.start_dryer",
                service_payload={"delay_seconds": 3600, "cycle_id": "normal"},
            )
            mock_timer.assert_called_once_with("dryer", window.start)
        mock_hass.services.async_call.assert_not_called()

    async def test_end_time_calls_script_immediately(self, mock_hass, end_time_device):
        """end_time appliances handle their own delay (via reservation) — fire script now."""
        sched = _make_scheduler(mock_hass, [end_time_device])
        window = _make_window(start_offset_minutes=600, duration_minutes=150, stale_delay_minutes=600)
        await sched.async_schedule(
            device_slug="lg_dryer", cycle_id="eco", window=window,
            cycle_kwh=2.0, now_rate=0.20,
            start_service="script.start_lg_dryer",
            service_payload={"delay_seconds": 36000, "cycle_id": "eco"},
        )
        mock_hass.services.async_call.assert_called_once()

    async def test_stored_schedule_records_delay_handling(self, mock_hass, end_time_device):
        sched = _make_scheduler(mock_hass, [end_time_device])
        window = _make_window(start_offset_minutes=120, stale_delay_minutes=120)
        await sched.async_schedule(
            device_slug="lg_dryer", cycle_id="eco", window=window,
            cycle_kwh=2.0, now_rate=0.20,
            start_service="script.start_lg_dryer",
            service_payload={"delay_seconds": 7200, "cycle_id": "eco"},
        )
        stored = sched.get_schedule("lg_dryer")
        assert stored["delay_handling"] == DELAY_HANDLING_END_TIME

    async def test_reschedule_envelope_from_wait_limit(self, mock_hass, native_device):
        """No deadline → finish within max_wait_hours (default 36) of the press,
        independent of the appliance's 24h delay reach."""
        sched = _make_scheduler(mock_hass, [native_device])
        before = datetime.now(timezone.utc)
        await sched.async_schedule(
            device_slug="washer", cycle_id="eco_40",
            window=_make_window(start_offset_minutes=60),
            cycle_kwh=0.8, now_rate=0.20,
            start_service="script.start_washer",
            service_payload={"delay_seconds": 3600, "cycle_id": "eco_40"},
        )
        stored = sched.get_schedule("washer")
        assert stored["reschedule_cap_kind"] == "deadline"
        cap = datetime.fromisoformat(stored["reschedule_cap"])
        assert abs((cap - (before + timedelta(hours=36))).total_seconds()) < 5
        assert stored["reschedule_count"] == 0

    async def test_reschedule_envelope_legacy_max_delay(self, mock_hass, native_device):
        """max_wait_hours 0 → the old press + max_delay window."""
        device = {**native_device, "max_wait_hours": 0}
        sched = _make_scheduler(mock_hass, [device])
        before = datetime.now(timezone.utc)
        await sched.async_schedule(
            device_slug="washer", cycle_id="eco_40",
            window=_make_window(start_offset_minutes=60),
            cycle_kwh=0.8, now_rate=0.20,
            start_service="script.start_washer",
            service_payload={"delay_seconds": 3600, "cycle_id": "eco_40"},
        )
        stored = sched.get_schedule("washer")
        assert stored["reschedule_cap_kind"] == "window"
        cap = datetime.fromisoformat(stored["reschedule_cap"])
        expected = before + timedelta(minutes=native_device["max_delay_minutes"])
        assert abs((cap - expected).total_seconds()) < 5

    async def test_reschedule_envelope_from_deadline(self, mock_hass, native_device):
        """Explicit deadline wins over max_delay as the reschedule cap."""
        sched = _make_scheduler(mock_hass, [native_device])
        deadline = datetime.now(timezone.utc) + timedelta(hours=6)
        await sched.async_schedule(
            device_slug="washer", cycle_id="eco_40",
            window=_make_window(start_offset_minutes=60),
            cycle_kwh=0.8, now_rate=0.20,
            start_service="script.start_washer",
            service_payload={"delay_seconds": 3600, "cycle_id": "eco_40"},
            reschedule_deadline=deadline,
        )
        stored = sched.get_schedule("washer")
        assert stored["reschedule_cap_kind"] == "deadline"
        assert stored["reschedule_cap"] == deadline.isoformat()

    async def test_hold_until_stored_on_schedule(self, mock_hass, native_device):
        """Hold-for-rates placeholder marker survives into the schedule dict."""
        sched = _make_scheduler(mock_hass, [native_device])
        hold = datetime.now(timezone.utc) + timedelta(minutes=55)
        await sched.async_schedule(
            device_slug="washer", cycle_id="eco_40",
            window=_make_window(start_offset_minutes=120),
            cycle_kwh=0.8, now_rate=0.20,
            start_service="script.start_washer",
            service_payload={"delay_seconds": 7200},
            hold_until=hold,
        )
        assert sched.get_schedule("washer")["hold_until"] == hold.isoformat()

    async def test_rate_predicted_stored_and_cleared_on_reschedule(self, mock_hass, native_device):
        """Predicted-rate commits are flagged; any reschedule clears the flag
        (reschedule candidates always come from real rates)."""
        sched = _make_scheduler(mock_hass, [native_device])
        await sched.async_schedule(
            device_slug="washer", cycle_id="eco_40",
            window=_make_window(start_offset_minutes=120),
            cycle_kwh=0.8, now_rate=0.20,
            start_service="script.start_washer",
            service_payload={"delay_seconds": 7200},
            rate_predicted=True,
        )
        assert sched.get_schedule("washer")["rate_predicted"] is True

        await sched.async_reschedule("washer", _make_window(start_offset_minutes=300, rate=0.05))
        assert sched.get_schedule("washer")["rate_predicted"] is False


class TestReschedule:
    async def _schedule(self, sched, slug="washer", cycle="eco_40", rate=0.20):
        await sched.async_schedule(
            device_slug=slug, cycle_id=cycle,
            window=_make_window(start_offset_minutes=120, rate=rate),
            cycle_kwh=0.8, now_rate=0.25,
            start_service="script.start_washer",
            service_payload={"delay_seconds": 7200, "cycle_id": cycle, "reschedule": False},
        )

    async def test_reschedule_updates_schedule_and_recalls_service(self, mock_hass, native_device):
        sched = _make_scheduler(mock_hass, [native_device])
        await self._schedule(sched, rate=0.20)
        mock_hass.services.async_call.reset_mock()

        new_win = _make_window(start_offset_minutes=300, rate=0.10)
        await sched.async_reschedule("washer", new_win)

        stored = sched.get_schedule("washer")
        assert stored["status"] == STATE_SCHEDULED
        assert stored["scheduled_start"] == new_win.start.isoformat()
        assert stored["rate_cost_per_kwh"] == 0.10
        assert stored["reschedule_count"] == 1
        # cap survives the reschedule — anchored to the original decision
        assert stored["reschedule_cap_kind"] == "deadline"

        # Native devices get the start_service re-fired with reschedule=True
        mock_hass.services.async_call.assert_called_once()
        payload = mock_hass.services.async_call.call_args[0][2]
        assert payload["reschedule"] is True
        assert payload["delay_seconds"] == pytest.approx(300 * 60, abs=5)

    async def test_reschedule_fires_event_with_old_and_new(self, mock_hass, native_device):
        sched = _make_scheduler(mock_hass, [native_device])
        await self._schedule(sched, rate=0.20)
        old_start = sched.get_schedule("washer")["scheduled_start"]

        new_win = _make_window(start_offset_minutes=300, rate=0.10)
        await sched.async_reschedule("washer", new_win)

        events = [
            c for c in mock_hass.bus.async_fire.call_args_list
            if c[0][0] == "smartstart_cycle_rescheduled"
        ]
        assert len(events) == 1
        data = events[0][0][1]
        assert data["old_start"] == old_start
        assert data["new_start"] == new_win.start.isoformat()
        assert data["saving_per_kwh"] == pytest.approx(0.10)
        assert data["estimated_saving"] == pytest.approx(0.08)  # 0.10 × 0.8 kWh

    async def test_reschedule_managed_moves_timer_only(self, mock_hass, managed_device):
        sched = _make_scheduler(mock_hass, [managed_device])
        with patch.object(sched, "_register_managed_timer") as mock_timer:
            await sched.async_schedule(
                device_slug="dryer", cycle_id="normal",
                window=_make_window(start_offset_minutes=120),
                cycle_kwh=3.0, now_rate=0.20,
                start_service="script.start_dryer",
                service_payload={"delay_seconds": 7200},
            )
            new_win = _make_window(start_offset_minutes=300, rate=0.05)
            await sched.async_reschedule("dryer", new_win)
            mock_timer.assert_called_with("dryer", new_win.start)
        mock_hass.services.async_call.assert_not_called()

    async def test_reschedule_skipped_when_not_scheduled(self, mock_hass, native_device):
        sched = _make_scheduler(mock_hass, [native_device])
        sched._schedules["washer"] = {"status": STATE_RUNNING, "cycle_id": "eco_40"}

        await sched.async_reschedule("washer", _make_window(start_offset_minutes=60))

        assert sched.get_schedule("washer")["status"] == STATE_RUNNING
        mock_hass.services.async_call.assert_not_called()


class TestApplianceReach:
    """Targets past max_delay (only via an accepted offer) are sent at the
    appliance's reach, then re-sent once they fit — target unchanged."""

    async def _scheduled(self, sched):
        await sched.async_schedule(
            device_slug="washer", cycle_id="eco_40",
            window=_make_window(start_offset_minutes=120, rate=0.20),
            cycle_kwh=0.8, now_rate=0.25,
            start_service="script.start_washer",
            service_payload={"delay_seconds": 7200, "cycle_id": "eco_40", "reschedule": False},
        )
        sched._hass.services.async_call.reset_mock()

    async def test_offer_beyond_reach_sends_reach_and_arms_resend(self, mock_hass, native_device):
        sched = _make_scheduler(mock_hass, [native_device])
        await self._scheduled(sched)
        target = _make_window(start_offset_minutes=25 * 60, duration_minutes=60, rate=0.05)
        with patch("custom_components.smartstart.scheduler.async_track_point_in_time") as track:
            await sched.async_reschedule(
                "washer", target, reschedule_cap=target.end, rate_predicted=True, reason="offer",
            )
        payload = mock_hass.services.async_call.call_args[0][2]
        assert payload["delay_seconds"] == pytest.approx(24 * 3600, abs=5)
        # Interim payload describes what the appliance will actually do
        assert datetime.fromisoformat(payload["start_time"]) < target.start

        stored = sched.get_schedule("washer")
        assert stored["scheduled_start"] == target.start.isoformat()
        assert stored["reschedule_cap"] == target.end.isoformat()
        assert stored["reschedule_cap_kind"] == "deadline"
        assert stored["rate_predicted"] is True
        resend_at = datetime.fromisoformat(stored["recommand_at"])
        assert (resend_at - datetime.now(timezone.utc)).total_seconds() == pytest.approx(3660, abs=5)
        track.assert_called_once()

        events = [c for c in mock_hass.bus.async_fire.call_args_list
                  if c[0][0] == "smartstart_cycle_rescheduled"]
        assert events[-1][0][1]["reason"] == "offer"

    async def test_resend_carries_real_delay(self, mock_hass, native_device):
        sched = _make_scheduler(mock_hass, [native_device])
        await self._scheduled(sched)
        target = _make_window(start_offset_minutes=20 * 60, rate=0.05)
        sched._schedules["washer"].update({
            "scheduled_start":  target.start.isoformat(),
            "estimated_finish": target.end.isoformat(),
            "recommand_at":     datetime.now(timezone.utc).isoformat(),
        })
        await sched._recommand("washer")
        payload = mock_hass.services.async_call.call_args[0][2]
        assert payload["delay_seconds"] == pytest.approx(20 * 3600, abs=5)
        assert payload["recommand"] is True and payload["reschedule"] is True
        assert sched.get_schedule("washer")["recommand_at"] is None

    async def test_within_reach_no_resend(self, mock_hass, native_device):
        sched = _make_scheduler(mock_hass, [native_device])
        await self._scheduled(sched)
        await sched.async_reschedule("washer", _make_window(start_offset_minutes=600, rate=0.05))
        assert sched.get_schedule("washer")["recommand_at"] is None

    def test_clamped_delay_floors_to_step(self, native_device):
        device = {**native_device, "delay_step_minutes": 30}
        now = datetime.now(timezone.utc)
        win = _make_window(start_offset_minutes=25 * 60 + 10)
        delay, resend = SmartStartScheduler._reachable_delay(device, DELAY_HANDLING_START_DELAY, win, now)
        assert delay <= 24 * 3600 and delay % 1800 == 0
        assert resend is not None

    def test_end_time_reach_measured_on_finish(self, end_time_device):
        now = datetime.now(timezone.utc)
        # Starts 17h out but a 150-min cycle ends at 19h30 — past LG's 19h reach.
        win = _make_window(start_offset_minutes=17 * 60, duration_minutes=150)
        delay, resend = SmartStartScheduler._reachable_delay(end_time_device, DELAY_HANDLING_END_TIME, win, now)
        assert resend is not None
        assert delay == pytest.approx(17 * 3600 - 30 * 60, abs=5)

    def test_managed_never_clamped(self, managed_device):
        now = datetime.now(timezone.utc)
        win = _make_window(start_offset_minutes=30 * 60)
        delay, resend = SmartStartScheduler._reachable_delay(managed_device, DELAY_HANDLING_MANAGED, win, now)
        assert resend is None

    async def test_resend_timer_restored_on_load(self, mock_hass, native_device):
        sched = _make_scheduler(mock_hass, [native_device])
        start = datetime.now(timezone.utc) + timedelta(hours=25)
        sched._store.async_load = AsyncMock(return_value={"schedules": {"washer": {
            "status": STATE_SCHEDULED, "cycle_id": "eco_40",
            "scheduled_start": start.isoformat(),
            "estimated_finish": (start + timedelta(hours=1)).isoformat(),
            "delay_handling": DELAY_HANDLING_START_DELAY,
            "recommand_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }}})
        with patch.object(sched, "_register_running_watcher"), \
             patch.object(sched, "_reconcile_status", new=MagicMock()), \
             patch.object(sched, "_register_recommand_timer") as reg:
            await sched.async_load()
        reg.assert_called_once()
