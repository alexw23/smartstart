"""Tests for laundry.py — LaundryManager plan building, run lifecycle, reschedule."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.smartstart.const import (
    DOMAIN,
    LAUNDRY_STATE_AWAITING,
    LAUNDRY_STATE_IDLE,
    LAUNDRY_STATE_RUNNING,
    LAUNDRY_STATE_SCHEDULED,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_SCHEDULED,
)
from custom_components.smartstart.laundry import LaundryManager, find_claiming_manager
from custom_components.smartstart.laundry_plan import MACHINE_DRY, MACHINE_WASH
from .conftest import make_rates

NOW_FLOOR = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


WASHER_CFG = {
    "device_slug": "washer",
    "name": "Washing Machine",
    "start_service": "script.start_washer",
    "delay_handling": "start_delay",
    "delay_step_minutes": 1,
    "min_delay_minutes": 0,
    "max_delay_minutes": 1440,
    "balanced_target_rate": 0.05,
    "cycles": [
        {"cycle_key": "delicate", "cycle_id": "Delicate 800", "name": "Delicate 800",
         "duration_minutes": 90, "total_kwh": 0.7},
        {"cycle_key": "rinse_spin", "cycle_id": "Rinse+Spin", "name": "Rinse & Spin",
         "duration_minutes": 20, "total_kwh": 0.1},
        {"cycle_key": "cotton_40", "cycle_id": "Cotton 40", "name": "Cotton 40",
         "duration_minutes": 60, "total_kwh": 1.0},
    ],
}

DRYER_CFG = {
    "device_slug": "dryer",
    "name": "Dryer",
    "start_service": "script.start_dryer",
    "delay_handling": "start_delay",
    "delay_step_minutes": 1,
    "min_delay_minutes": 0,
    "max_delay_minutes": 1440,
    "cycles": [
        {"cycle_key": "iron_eco", "cycle_id": "Iron Eco", "name": "Iron Eco",
         "duration_minutes": 60, "total_kwh": 1.5},
        {"cycle_key": "cupboard_eco", "cycle_id": "Cupboard Eco", "name": "Cupboard Eco",
         "duration_minutes": 120, "total_kwh": 2.5},
    ],
}

LINEN_FLOW = {
    "slug": "linen_sheets",
    "name": "Linen Sheets",
    "steps": [
        {"machine": MACHINE_WASH, "cycle_key": "delicate", "confirm_before": True,
         "duration_minutes": 60},  # override: sheets run short
        {"machine": MACHINE_WASH, "cycle_key": "rinse_spin", "confirm_before": True},
        {"machine": MACHINE_DRY, "cycle_key": "iron_eco", "confirm_before": True},
        {"machine": MACHINE_WASH, "cycle_key": "rinse_spin", "confirm_before": True},
        {"machine": MACHINE_DRY, "cycle_key": "iron_eco", "confirm_before": True},
    ],
}

DOUBLE_DRY_FLOW = {
    "slug": "double_dry",
    "name": "2x Iron Eco",
    "steps": [
        {"machine": MACHINE_DRY, "cycle_key": "iron_eco", "confirm_before": True},
        {"machine": MACHINE_DRY, "cycle_key": "iron_eco", "confirm_before": False},
    ],
}

PAIR_CONFIG = {
    "entry_type": "laundry",
    "name": "Laundry",
    "washer_slug": "washer",
    "dryer_slug": "dryer",
    "gap_minutes": 15,
    "auto_reschedule": True,
    "hold_for_next_day_rates": False,
    # Sleep window off (bedtime == wake) and no wet cap, so wall-clock time
    # can't shift timelines in tests that aren't about it.
    "bedtime": "00:00",
    "wake_time": "00:00",
    "max_wet_wait_hours": 0,
    "flows": [LINEN_FLOW, DOUBLE_DRY_FLOW],
}


def _mock_entry(entry_id: str, data: dict):
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.options = {}
    entry.data = data
    return entry


def _mock_device_store(sched_status: dict):
    """Mock scheduler + coordinator store for one appliance entry."""
    scheduler = MagicMock()
    scheduler.async_schedule = AsyncMock()
    scheduler.async_reschedule = AsyncMock()
    scheduler.get_schedule = MagicMock(side_effect=lambda slug: dict(sched_status))
    scheduler.add_listener = MagicMock()
    return {"scheduler": scheduler, "coordinator": None, "curve_store": None}


@pytest.fixture
def laundry_env():
    """hass with washer + dryer entries loaded, cheap rates, and a manager."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.bus.async_fire = MagicMock()
    # Background tasks (e.g. the due-check's save) aren't run by the mock.
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())

    washer_status = {"status": STATE_IDLE}
    dryer_status = {"status": STATE_IDLE}

    washer_store = _mock_device_store(washer_status)
    dryer_store = _mock_device_store(dryer_status)

    coordinator = MagicMock()
    coordinator.rates = make_rates([0.20] * 48, start=NOW_FLOOR)
    coordinator.rate_at = MagicMock(return_value=0.20)
    coordinator.prefer_sooner_diff = 0.03
    coordinator.compute_hold_until = MagicMock(return_value=None)
    coordinator._next_day_rates_missing = MagicMock(return_value=False)
    coordinator.rates_with_predictions = MagicMock(side_effect=lambda now: coordinator.rates)
    coordinator.planner_rates = MagicMock(side_effect=lambda now: coordinator.rates)
    coordinator.currency_symbol = "£"
    coordinator.rate_unit = "p"
    coordinator.is_predicted_window = MagicMock(return_value=False)
    coordinator.best_windows_info = MagicMock(return_value=[])
    coordinator.async_add_listener = MagicMock(return_value=MagicMock())
    washer_store["coordinator"] = coordinator
    dryer_store["coordinator"] = coordinator

    hass.data = {DOMAIN: {"washer_entry": washer_store, "dryer_entry": dryer_store}}
    hass.config_entries.async_entries = MagicMock(return_value=[
        _mock_entry("washer_entry", {"devices": [WASHER_CFG]}),
        _mock_entry("dryer_entry", {"devices": [DRYER_CFG]}),
    ])

    with patch("custom_components.smartstart.laundry.Store") as MockStore:
        store = MagicMock()
        store.async_load = AsyncMock(return_value=None)
        store.async_save = AsyncMock()
        MockStore.return_value = store
        manager = LaundryManager(hass, "laundry_entry", PAIR_CONFIG)

    return {
        "hass": hass,
        "manager": manager,
        "washer_store": washer_store,
        "dryer_store": dryer_store,
        "washer_status": washer_status,
        "dryer_status": dryer_status,
        "coordinator": coordinator,
    }


def _events(hass, event_type):
    return [c.args[1] for c in hass.bus.async_fire.call_args_list if c.args[0] == event_type]


# ── Plan building ─────────────────────────────────────────────────────────────

class TestPlanBuilding:
    def test_manual_row_pair(self, laundry_env):
        m = laundry_env["manager"]
        m.set_row_selection(0, "Wash: Cotton 40")
        m.set_row_selection(1, "Dry: Cupboard Eco")
        steps = m.build_plan_from_rows()
        assert [(s.machine, s.cycle_key) for s in steps] == [
            (MACHINE_WASH, "cotton_40"), (MACHINE_DRY, "cupboard_eco"),
        ]

    def test_flow_row_expands_steps(self, laundry_env):
        m = laundry_env["manager"]
        m.set_row_selection(0, "Linen Sheets")
        steps = m.build_plan_from_rows()
        assert len(steps) == 5
        assert steps[0].duration_minutes == 60  # flow override, not the 90-min config
        assert steps[0].flow_slug == "linen_sheets"

    def test_dry_first_flow_composes_with_manual_wash(self, laundry_env):
        m = laundry_env["manager"]
        m.set_row_selection(0, "Wash: Delicate 800")
        m.set_row_selection(1, "2x Iron Eco")
        steps = m.build_plan_from_rows()
        assert [(s.machine, s.confirm_before) for s in steps] == [
            (MACHINE_WASH, True), (MACHINE_DRY, True), (MACHINE_DRY, False),
        ]

    def test_multi_row_concatenates(self, laundry_env):
        m = laundry_env["manager"]
        m.set_row_selection(0, "Linen Sheets")
        m.set_row_selection(1, "Wash: Cotton 40")
        m.set_row_selection(2, "Dry: Cupboard Eco")
        steps = m.build_plan_from_rows()
        assert len(steps) == 7

    def test_row_options_include_flows(self, laundry_env):
        m = laundry_env["manager"]
        opts = m.row_options()
        assert opts[0] == "None"
        assert "Linen Sheets" in opts                   # wash-first flow
        assert "2x Iron Eco" in opts                    # dry-first flow
        assert "Wash: Cotton 40" in opts
        assert "Dry: Cupboard Eco" in opts
        # flows listed before single cycles
        assert opts.index("Linen Sheets") < opts.index("Wash: Cotton 40")

    def test_legacy_flow_label_maps_to_flow(self, laundry_env):
        m = laundry_env["manager"]
        m.set_row_selection(0, "Flow: Linen Sheets")  # pre-merge restored label
        assert m.get_row_selection(0) == "Linen Sheets"
        assert len(m.build_plan_from_rows()) == 5


# ── Run lifecycle ─────────────────────────────────────────────────────────────

async def _start_simple(env):
    m = env["manager"]
    m.set_row_selection(0, "Wash: Cotton 40")
    m.set_row_selection(1, "Dry: Cupboard Eco")
    await m.async_start()
    env["washer_status"].update({"status": STATE_SCHEDULED})
    return m


class TestRunLifecycle:
    @pytest.mark.asyncio
    async def test_start_commits_washer_only(self, laundry_env):
        m = await _start_simple(laundry_env)
        assert m.status == LAUNDRY_STATE_SCHEDULED
        washer_sched = laundry_env["washer_store"]["scheduler"]
        dryer_sched = laundry_env["dryer_store"]["scheduler"]
        assert washer_sched.async_schedule.await_count == 1
        assert dryer_sched.async_schedule.await_count == 0
        kwargs = washer_sched.async_schedule.await_args.kwargs
        assert kwargs["laundry_run"] is True
        assert kwargs["cycle_id"] == "cotton_40"
        assert _events(laundry_env["hass"], "smartstart_laundry_scheduled")

    @pytest.mark.asyncio
    async def test_start_twice_rejected(self, laundry_env):
        from homeassistant.exceptions import HomeAssistantError
        m = await _start_simple(laundry_env)
        with pytest.raises(HomeAssistantError):
            await m.async_start()

    @pytest.mark.asyncio
    async def test_wash_done_awaits_changeover(self, laundry_env):
        m = await _start_simple(laundry_env)
        laundry_env["washer_status"].update({"status": STATE_RUNNING})
        await m._reconcile_device("washer")
        assert m.status == LAUNDRY_STATE_RUNNING
        laundry_env["washer_status"].update({"status": STATE_IDLE})
        await m._reconcile_device("washer")
        assert m.status == LAUNDRY_STATE_AWAITING
        assert m.continue_available()
        done_events = _events(laundry_env["hass"], "smartstart_laundry_step_done")
        assert done_events and done_events[0]["awaiting_changeover"] is True
        # The dryer is the machine we're now waiting on (wash → dry changeover).
        assert m.awaiting_machines() == ["dry"]
        assert m.snapshot()["awaiting_machines"] == ["dry"]

    @pytest.mark.asyncio
    async def test_awaiting_machines_empty_unless_awaiting(self, laundry_env):
        m = await _start_simple(laundry_env)
        assert m.awaiting_machines() == []          # scheduled/running
        assert m.snapshot()["awaiting_machines"] == []
        laundry_env["washer_status"].update({"status": STATE_RUNNING})
        await m._reconcile_device("washer")
        assert m.awaiting_machines() == []          # running, no trip due

    @pytest.mark.asyncio
    async def test_continue_fires_dryer(self, laundry_env):
        m = await _start_simple(laundry_env)
        laundry_env["washer_status"].update({"status": STATE_RUNNING})
        await m._reconcile_device("washer")
        laundry_env["washer_status"].update({"status": STATE_IDLE})
        await m._reconcile_device("washer")

        await m.async_continue()
        dryer_sched = laundry_env["dryer_store"]["scheduler"]
        assert dryer_sched.async_schedule.await_count == 1
        assert dryer_sched.async_schedule.await_args.kwargs["cycle_id"] == "cupboard_eco"
        assert m.status == LAUNDRY_STATE_RUNNING

    @pytest.mark.asyncio
    async def test_completion_clears_run_and_resets_rows(self, laundry_env):
        m = await _start_simple(laundry_env)
        m.set_row_selection(2, "Wash: Cotton 40")  # extra row that must reset
        laundry_env["washer_status"].update({"status": STATE_RUNNING})
        await m._reconcile_device("washer")
        laundry_env["washer_status"].update({"status": STATE_IDLE})
        await m._reconcile_device("washer")
        await m.async_continue()
        laundry_env["dryer_status"].update({"status": STATE_RUNNING})
        await m._reconcile_device("dryer")
        laundry_env["dryer_status"].update({"status": STATE_IDLE})
        await m._reconcile_device("dryer")

        assert m.status == LAUNDRY_STATE_IDLE
        assert _events(laundry_env["hass"], "smartstart_laundry_completed")
        assert m.get_row_selection(0) == "Wash: Cotton 40"  # Load 1 keeps its selection
        assert m.get_row_selection(1) == "None"             # later loads reset
        assert m.get_row_selection(2) == "None"

    @pytest.mark.asyncio
    async def test_auto_confirm_step_fires_without_continue(self, laundry_env):
        m = laundry_env["manager"]
        m.set_row_selection(0, "Wash: Delicate 800")
        m.set_row_selection(1, "2x Iron Eco")
        await m.async_start()
        # wash runs and finishes
        laundry_env["washer_status"].update({"status": STATE_RUNNING})
        await m._reconcile_device("washer")
        laundry_env["washer_status"].update({"status": STATE_IDLE})
        await m._reconcile_device("washer")
        # first dry needs Continue
        await m.async_continue()
        dryer_sched = laundry_env["dryer_store"]["scheduler"]
        assert dryer_sched.async_schedule.await_count == 1
        # first dry runs → done: second dry (confirm_before=False) auto-fires
        laundry_env["dryer_status"].update({"status": STATE_RUNNING})
        await m._reconcile_device("dryer")
        laundry_env["dryer_status"].update({"status": STATE_IDLE})
        await m._reconcile_device("dryer")
        assert dryer_sched.async_schedule.await_count == 2
        assert m.status == LAUNDRY_STATE_RUNNING  # not awaiting — no trip needed

    @pytest.mark.asyncio
    async def test_pipeline_continue_fires_both_machines(self, laundry_env):
        m = laundry_env["manager"]
        m.set_row_selection(0, "Wash: Cotton 40")
        m.set_row_selection(1, "Dry: Cupboard Eco")
        m.set_row_selection(2, "Wash: Cotton 40")
        m.set_row_selection(3, "Dry: Cupboard Eco")
        await m.async_start()
        laundry_env["washer_status"].update({"status": STATE_RUNNING})
        await m._reconcile_device("washer")
        laundry_env["washer_status"].update({"status": STATE_IDLE})
        await m._reconcile_device("washer")
        assert m.status == LAUNDRY_STATE_AWAITING

        # One trip: dry 1 into dryer, load 2 into washer.
        await m.async_continue()
        assert laundry_env["dryer_store"]["scheduler"].async_schedule.await_count == 1
        assert laundry_env["washer_store"]["scheduler"].async_schedule.await_count == 2

    @pytest.mark.asyncio
    async def test_free_washer_offered_while_dryer_runs(self, laundry_env):
        # Dry-first plan: the washer is free the moment the dryer starts, so
        # the wash trip must be offered then — not after the dryer finishes.
        m = laundry_env["manager"]
        m.set_order_mode("As listed")
        m.set_row_selection(0, "Dry: Cupboard Eco")
        m.set_row_selection(1, "Wash: Cotton 40")
        await m.async_start()
        assert m.status == LAUNDRY_STATE_SCHEDULED
        laundry_env["dryer_status"].update({"status": STATE_RUNNING})
        await m._reconcile_device("dryer")
        assert m.status == LAUNDRY_STATE_AWAITING
        assert m.awaiting_machines() == ["wash"]
        assert "still running" in m.next_action()
        assert _events(laundry_env["hass"], "smartstart_laundry_changeover_due")

        await m.async_continue(start_now=True)
        washer_sched = laundry_env["washer_store"]["scheduler"]
        assert washer_sched.async_schedule.await_args.kwargs["cycle_id"] == "cotton_40"
        assert m.status == LAUNDRY_STATE_RUNNING

    @pytest.mark.asyncio
    async def test_stuck_running_run_self_heals_on_tick(self, laundry_env):
        m = laundry_env["manager"]
        m.set_order_mode("As listed")
        m.set_row_selection(0, "Dry: Cupboard Eco")
        m.set_row_selection(1, "Wash: Cotton 40")
        await m.async_start()
        # A run saved before the fix: dryer running, wash never offered.
        m._run["states"][0] = "running"
        m._run["status"] = LAUNDRY_STATE_RUNNING
        laundry_env["dryer_status"].update({"status": STATE_RUNNING})
        hass = laundry_env["hass"]
        scheduled = []
        hass.async_create_task.side_effect = scheduled.append
        m.minute_tick()
        for coro in scheduled:
            await coro
        assert m.status == LAUNDRY_STATE_AWAITING

    @pytest.mark.asyncio
    async def test_cancel_clears_scheduled_step(self, laundry_env):
        m = await _start_simple(laundry_env)
        await m.async_cancel()
        assert m.status == LAUNDRY_STATE_IDLE
        # Routed through smartstart.stop so the user's stop_service fires too
        laundry_env["hass"].services.async_call.assert_awaited_with(
            DOMAIN, "stop", {"device_slug": "washer"}, blocking=True
        )
        assert _events(laundry_env["hass"], "smartstart_laundry_cancelled")

    @pytest.mark.asyncio
    async def test_claiming(self, laundry_env):
        m = await _start_simple(laundry_env)
        assert m.claims("washer") and m.claims("dryer")
        assert not m.claims("dishwasher")
        assert find_claiming_manager(laundry_env["hass"], "washer") is None  # not registered in hass.data
        laundry_env["hass"].data[DOMAIN]["laundry_entry"] = {"laundry": m}
        assert find_claiming_manager(laundry_env["hass"], "washer") is m


# ── Snapshot / projections ────────────────────────────────────────────────────

class TestSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_carries_step_plan(self, laundry_env):
        m = await _start_simple(laundry_env)
        snap = m.snapshot()
        assert snap["status"] == LAUNDRY_STATE_SCHEDULED
        assert len(snap["steps"]) == 2
        assert snap["steps"][0]["machine"] == MACHINE_WASH
        assert snap["planned_start"] is not None
        assert snap["planned_finish"] > snap["planned_start"]
        assert snap["current_step"] == "1/2"

    def test_idle_snapshot(self, laundry_env):
        snap = laundry_env["manager"].snapshot()
        assert snap["status"] == LAUNDRY_STATE_IDLE
        assert snap["steps"] == []


# ── Live preview (score on selection, not on Start) ───────────────────────────

class TestPreview:
    def test_selection_populates_planned_figures(self, laundry_env):
        m = laundry_env["manager"]
        m.set_row_selection(0, "Wash: Cotton 40")
        m.set_row_selection(1, "Dry: Cupboard Eco")
        snap = m.snapshot()
        assert snap["status"] == LAUNDRY_STATE_IDLE  # still idle — nothing committed
        assert snap["preview"] is True
        assert snap["planned_start"] is not None
        assert snap["planned_finish"] > snap["planned_start"]
        assert snap["plan_rate"] == pytest.approx(0.20, abs=0.01)
        assert snap["total_kwh"] == pytest.approx(3.5)
        assert [s["state"] for s in snap["steps"]] == ["planned", "planned"]
        assert "would begin at" in snap["next_action"]

    def test_clearing_selection_clears_preview(self, laundry_env):
        m = laundry_env["manager"]
        m.set_row_selection(0, "Wash: Cotton 40")
        m.set_row_selection(0, "None")
        snap = m.snapshot()
        assert snap["preview"] is False
        assert snap["planned_start"] is None
        assert snap["steps"] == []

    def test_preview_finds_cheap_window(self, laundry_env):
        # Expensive for 6h, cheap block after — preview must aim at the cheap block.
        rates = [0.30] * 12 + [0.05] * 8 + [0.30] * 28
        laundry_env["coordinator"].rates = make_rates(rates, start=NOW_FLOOR)
        m = laundry_env["manager"]
        m.set_row_selection(0, "Wash: Cotton 40")
        m.set_row_selection(1, "Dry: Cupboard Eco")
        assert m.snapshot()["plan_rate"] < 0.10

    def test_preview_tracks_mode_change(self, laundry_env):
        from custom_components.smartstart.const import MODE_MAX_SAVINGS, MODE_NOW
        rates = [0.30] * 12 + [0.05] * 8 + [0.30] * 28
        laundry_env["coordinator"].rates = make_rates(rates, start=NOW_FLOOR)
        m = laundry_env["manager"]
        m.set_row_selection(0, "Wash: Cotton 40")
        m.set_row_selection(1, "Dry: Cupboard Eco")
        m.set_mode(MODE_NOW)
        now_rate = m.snapshot()["plan_rate"]
        m.set_mode(MODE_MAX_SAVINGS)
        savings_rate = m.snapshot()["plan_rate"]
        assert savings_rate < now_rate

    def test_rates_update_rescores_preview(self, laundry_env):
        m = laundry_env["manager"]
        m.set_row_selection(0, "Wash: Cotton 40")
        m.set_row_selection(1, "Dry: Cupboard Eco")
        rate_before = m.snapshot()["plan_rate"]
        laundry_env["coordinator"].rates = make_rates(
            [0.30] * 4 + [0.02] * 8 + [0.30] * 36, start=NOW_FLOOR
        )
        m._on_rates_update()
        assert m.snapshot()["plan_rate"] < rate_before

    def test_preview_reports_savings_vs_now(self, laundry_env):
        rates = [0.30] * 12 + [0.05] * 8 + [0.30] * 28
        laundry_env["coordinator"].rates = make_rates(rates, start=NOW_FLOOR)
        m = laundry_env["manager"]
        m.set_row_selection(0, "Wash: Cotton 40")
        m.set_row_selection(1, "Dry: Cupboard Eco")
        snap = m.snapshot()
        assert snap["now_rate"] > snap["plan_rate"]
        assert snap["estimated_savings"] == pytest.approx(
            (snap["now_rate"] - snap["plan_rate"]) * snap["total_kwh"], abs=1e-3
        )

    def test_no_savings_when_plan_starts_now(self, laundry_env):
        from custom_components.smartstart.const import MODE_NOW
        m = laundry_env["manager"]
        m.set_mode(MODE_NOW)
        m.set_row_selection(0, "Wash: Cotton 40")
        assert m.snapshot()["estimated_savings"] is None

    def test_unknown_label_gives_blank_preview_not_error(self, laundry_env):
        m = laundry_env["manager"]
        m.set_row_selection(0, "Ghost Cycle")  # e.g. stale restored state
        snap = m.snapshot()
        assert snap["planned_start"] is None
        assert snap["preview"] is False

    @pytest.mark.asyncio
    async def test_run_figures_take_over_from_preview(self, laundry_env):
        m = await _start_simple(laundry_env)
        snap = m.snapshot()
        assert snap["status"] == LAUNDRY_STATE_SCHEDULED
        assert snap["preview"] is False

    @pytest.mark.asyncio
    async def test_cancel_restores_preview(self, laundry_env):
        m = await _start_simple(laundry_env)
        laundry_env["washer_status"].update({"status": STATE_IDLE})
        await m.async_cancel()
        snap = m.snapshot()
        assert snap["status"] == LAUNDRY_STATE_IDLE
        assert snap["preview"] is True  # selections kept, so the preview returns
        assert snap["planned_start"] is not None


# ── Pair-level auto-reschedule ────────────────────────────────────────────────

class TestReschedule:
    @pytest.mark.asyncio
    async def test_cheaper_rates_move_the_plan(self, laundry_env):
        # Commit to a delayed window first (expensive now, cheaper block 2h out)
        # — a plan already due to start can't be moved.
        initial = [0.30] * 4 + [0.10] * 8 + [0.30] * 36
        laundry_env["coordinator"].rates = make_rates(initial, start=NOW_FLOOR)
        m = await _start_simple(laundry_env)
        t0_before = m.snapshot()["planned_start"]
        rate_before = m.snapshot()["plan_rate"]
        # New rates reveal a much cheaper block 8h out, still inside the cap.
        cheap = [0.30] * 4 + [0.10] * 8 + [0.30] * 4 + [0.02] * 8 + [0.30] * 24
        laundry_env["coordinator"].rates = make_rates(cheap, start=NOW_FLOOR)
        await m._maybe_reschedule()
        washer_sched = laundry_env["washer_store"]["scheduler"]
        assert washer_sched.async_reschedule.await_count == 1
        assert m.snapshot()["plan_rate"] < rate_before
        assert m.snapshot()["planned_start"] != t0_before

    @pytest.mark.asyncio
    async def test_no_move_once_running(self, laundry_env):
        m = await _start_simple(laundry_env)
        laundry_env["washer_status"].update({"status": STATE_RUNNING})
        await m._reconcile_device("washer")
        laundry_env["coordinator"].rates = make_rates([0.001] * 48, start=NOW_FLOOR)
        await m._maybe_reschedule()
        assert laundry_env["washer_store"]["scheduler"].async_reschedule.await_count == 0


# ── Predictive hold (forecast slots beyond the real horizon) ──────────────────

def _arm_predictive_hold(env, real, predicted):
    """Hold active, real rates for `real`, forecast slots right after them."""
    c = env["coordinator"]
    m = env["manager"]
    m._hold_for_rates = True
    c.rates = make_rates(real, start=NOW_FLOOR)
    horizon = c.rates[-1].end
    pred = make_rates(predicted, start=horizon)
    c.rates_with_predictions = MagicMock(side_effect=lambda now: c.rates + pred)
    c.is_predicted_window = MagicMock(side_effect=lambda w: w.end > c.rates[-1].end)
    c.compute_hold_until = MagicMock(
        return_value=datetime.now(timezone.utc) + timedelta(minutes=15)
    )
    c._next_day_rates_missing = MagicMock(return_value=True)
    return m


class TestPredictiveHold:
    def test_held_preview_uses_forecast(self, laundry_env):
        # Real rates 8h at 30p; forecast says 5p right after — a held plan
        # should be pre-positioned on the forecast block.
        m = _arm_predictive_hold(laundry_env, [0.30] * 16, [0.05] * 10)
        m.set_row_selection(0, "Wash: Cotton 40")
        m.set_row_selection(1, "Dry: Cupboard Eco")
        assert m.snapshot()["plan_rate"] < 0.10

    def test_no_forecast_without_hold(self, laundry_env):
        m = _arm_predictive_hold(laundry_env, [0.30] * 16, [0.05] * 10)
        m._hold_for_rates = False
        m.set_row_selection(0, "Wash: Cotton 40")
        m.set_row_selection(1, "Dry: Cupboard Eco")
        assert m.snapshot()["plan_rate"] == pytest.approx(0.30, abs=0.01)

    @pytest.mark.asyncio
    async def test_predicted_commit_rebaselines_against_actuals(self, laundry_env):
        m = _arm_predictive_hold(laundry_env, [0.30] * 16, [0.05] * 10)
        m = await _start_simple(laundry_env)
        assert m._run["rate_predicted"] is True
        assert m._run["plan_rate"] < 0.10
        # Real rates land: the forecast block was optimistic (25p), but a real
        # 15p block exists. The 5p prediction must not block the move.
        c = laundry_env["coordinator"]
        c._next_day_rates_missing = MagicMock(return_value=False)
        c.rates = make_rates(
            [0.30] * 16 + [0.25] * 10 + [0.15] * 10 + [0.30] * 12, start=NOW_FLOOR
        )
        await m._maybe_reschedule()
        assert laundry_env["washer_store"]["scheduler"].async_reschedule.await_count == 1
        assert m._run["rate_predicted"] is False
        assert m._run["plan_rate"] == pytest.approx(0.15, abs=0.01)


# ── Repeat-every-N-days flows ─────────────────────────────────────────────────

def _with_repeat(env, days=7, last_done_days_ago=6.5):
    m = env["manager"]
    m._flows = [dict(LINEN_FLOW, repeat_days=days), DOUBLE_DRY_FLOW]
    now = datetime.now(timezone.utc)
    m._last_done = {"linen_sheets": (now - timedelta(days=last_done_days_ago)).isoformat()}
    return m, now


class TestRepeatFlows:
    def test_due_flow_fills_empty_load_with_deadline(self, laundry_env):
        m, now = _with_repeat(laundry_env)
        m._check_due(now)
        assert m.get_row_selection(0) == "Linen Sheets"
        due = datetime.fromisoformat(m._last_done["linen_sheets"]) + timedelta(days=7)
        assert m.due_at == due
        assert _events(laundry_env["hass"], "smartstart_laundry_load_due")[0]["load"] == 1

    def test_not_due_yet_leaves_rows(self, laundry_env):
        m, now = _with_repeat(laundry_env, last_done_days_ago=2)  # due in 5 days
        m._check_due(now)
        assert m.get_row_selection(0) == "None"
        assert m.due_at is None

    def test_cleared_row_not_refilled_for_same_due_date(self, laundry_env):
        m, now = _with_repeat(laundry_env)
        m._check_due(now)
        m.set_row_selection(0, "None")
        m._check_due(now)
        assert m.get_row_selection(0) == "None"

    def test_fills_next_empty_load(self, laundry_env):
        m, now = _with_repeat(laundry_env)
        m.set_row_selection(0, "Wash: Cotton 40")
        m._check_due(now)
        assert m.get_row_selection(1) == "Linen Sheets"

    def test_user_deadline_not_overridden_by_later_due(self, laundry_env):
        m, now = _with_repeat(laundry_env)
        mine = now + timedelta(hours=6)
        m.set_due_at(mine)
        m._check_due(now)
        assert m.due_at == mine

    def test_new_interval_starts_counting_not_due(self, laundry_env):
        m = laundry_env["manager"]
        m._flows = [dict(LINEN_FLOW, repeat_days=7)]
        m._check_due(datetime.now(timezone.utc))  # no history → seeded, not due
        assert m.get_row_selection(0) == "None"
        assert "linen_sheets" in m._last_done

    @pytest.mark.asyncio
    async def test_completion_records_last_done_and_clears_repeat_row(self, laundry_env):
        m, now = _with_repeat(laundry_env)
        m._check_due(now)
        await m.async_start()
        # run all five Linen steps to completion
        for _ in range(10):
            if m._run is None:
                break
            steps = m._run_steps()
            for i, st in enumerate(m._run["states"]):
                if st == "fired":
                    status = laundry_env["washer_status" if steps[i].machine == MACHINE_WASH else "dryer_status"]
                    slug = "washer" if steps[i].machine == MACHINE_WASH else "dryer"
                    status.update({"status": STATE_RUNNING})
                    await m._reconcile_device(slug)
                    status.update({"status": STATE_IDLE})
                    await m._reconcile_device(slug)
                    break
            else:
                await m.async_continue()
        assert m._run is None
        assert datetime.fromisoformat(m._last_done["linen_sheets"]) > now - timedelta(minutes=1)
        assert m.get_row_selection(0) == "None"  # repeating flow doesn't linger in Load 1


# ── Sleep window / changeover availability ───────────────────────────────────

from custom_components.smartstart.laundry_plan import PlanStep, max_wet_wait
from homeassistant.util import dt as dt_util


def _sleepy(env, bed="23:00", wake="07:00", cap=0.0):
    m = env["manager"]
    from custom_components.smartstart.laundry import _parse_hhmm
    m._bedtime = _parse_hhmm(bed, "00:00")
    m._wake = _parse_hhmm(wake, "00:00")
    m._wet_cap_minutes = cap * 60
    return m


def _local(hh, mm=0, day_offset=0):
    base = dt_util.as_local(datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)).date()
    d = base + timedelta(days=day_offset)
    return dt_util.as_utc(datetime.combine(d, datetime.min.time().replace(hour=hh, minute=mm),
                                           tzinfo=dt_util.get_default_time_zone()))


def _wash_dry():
    return [
        PlanStep(MACHINE_WASH, "cotton_40", "Cotton 40", 60, 1.0),
        PlanStep(MACHINE_DRY, "cupboard_eco", "Cupboard Eco", 120, 2.5),
    ]


class TestSleepWindow:
    def test_available_from_inside_and_outside_window(self, laundry_env):
        m = _sleepy(laundry_env)
        assert m._available_from(_local(21), None) == _local(21)          # awake
        assert m._available_from(_local(23, 30), None) == _local(7, 0, 1)  # after bed
        assert m._available_from(_local(3), None) == _local(7)             # small hours

    def test_override_pushes_past_then_rechecks_window(self, laundry_env):
        m = _sleepy(laundry_env)
        # out until 22:30 → 22:30 is fine; out until 23:30 → falls into the night
        assert m._available_from(_local(21), _local(22, 30)) == _local(22, 30)
        assert m._available_from(_local(21), _local(23, 30)) == _local(7, 0, 1)

    def test_night_start_waits_for_wake_changeover(self, laundry_env):
        m = _sleepy(laundry_env)
        offsets, _, total, _ = m._plan_shape(_wash_dry(), _local(2, 30, 1), None)
        dryer_at = _local(2, 30, 1) + timedelta(minutes=offsets[1])
        assert dryer_at == _local(7, 0, 1)
        assert total == pytest.approx(offsets[1] + 120)

    def test_evening_start_gets_changeover_before_bed(self, laundry_env):
        m = _sleepy(laundry_env)
        offsets, *_ = m._plan_shape(_wash_dry(), _local(21), None)
        assert offsets[1] == pytest.approx(60 + 15)  # 22:15, normal gap

    def test_wet_cap_rejects_long_waits(self, laundry_env):
        m = _sleepy(laundry_env, cap=4)
        # wash 02:30–03:30, dryer 07:00 → 3.5h wet: ok
        assert m._plan_shape(_wash_dry(), _local(2, 30, 1), None, enforce_cap=True)
        # wash 22:00–23:00, dryer 07:00 → 8h wet: rejected
        assert m._plan_shape(_wash_dry(), _local(22), None, enforce_cap=True) is None

    def test_scorer_avoids_cheap_wash_with_expensive_morning_dry(self, laundry_env):
        """Cheap 02:00-04:00 but pricey from 07:00: with a 07:00 changeover the
        dryer would land in the expensive morning, so the plan should rather
        run in the evening window where wash AND dry both fit before bed."""
        m = _sleepy(laundry_env)
        c = laundry_env["coordinator"]
        start = _local(18)
        # 18:00-23:00 at 15p, 23:00-02:00 30p, 02:00-04:00 5p, 04:00-07:00 30p, 07:00+ 60p
        rates = [0.15] * 10 + [0.30] * 6 + [0.05] * 4 + [0.30] * 6 + [0.60] * 20
        c.rates = make_rates(rates, start=start)
        steps = _wash_dry()
        with patch("custom_components.smartstart.laundry._utcnow", return_value=start):
            win = m.score_plan(steps, start)
        dryer_at = win.start + timedelta(minutes=m._plan_shape(steps, win.start, None)[0][1])
        assert dt_util.as_local(dryer_at).hour < 23  # whole plan done before bed
        assert win.avg_rate_cost_per_kwh < 0.20

    def test_max_wet_wait(self):
        steps = _wash_dry()
        assert max_wet_wait(steps, [0, 75]) == 15
        assert max_wet_wait(steps, [0, 300]) == 240



class TestLoadHistory:
    @pytest.mark.asyncio
    async def test_steps_carry_load_id_and_completion_records_load(self, laundry_env):
        m = await _start_simple(laundry_env)
        run_id = m._run["id"]
        wkw = laundry_env["washer_store"]["scheduler"].async_schedule.await_args.kwargs
        assert wkw["laundry"] == {"run_id": run_id, "pair": m.slug, "step": 0, "flow": None}

        laundry_env["washer_status"].update({"status": STATE_RUNNING})
        await m._reconcile_device("washer")
        laundry_env["washer_status"].update({"status": STATE_IDLE})
        await m._reconcile_device("washer")
        await m.async_continue()
        dkw = laundry_env["dryer_store"]["scheduler"].async_schedule.await_args.kwargs
        assert dkw["laundry"]["run_id"] == run_id and dkw["laundry"]["step"] == 1
        laundry_env["dryer_status"].update({"status": STATE_RUNNING})
        await m._reconcile_device("dryer")
        laundry_env["dryer_status"].update({"status": STATE_IDLE})
        await m._reconcile_device("dryer")

        assert len(m.loads) == 1
        load = m.loads[0]
        assert load["id"] == run_id
        assert [s["machine"] for s in load["steps"]] == ["wash", "dry"]
        assert all(s["start"] and s["end"] for s in load["steps"])
        assert load["plan_cost"] is not None

    @pytest.mark.asyncio
    async def test_cancelled_run_records_no_load(self, laundry_env):
        m = await _start_simple(laundry_env)
        await m.async_cancel()
        assert m.loads == []


# ── Load grouping / order optimiser ───────────────────────────────────────────

BIG_FLOW = {"slug": "big", "name": "Big", "steps": [
    {"machine": MACHINE_WASH, "cycle_key": "delicate"},       # 90 min, 0.7 kWh
    {"machine": MACHINE_DRY, "cycle_key": "iron_eco"},        # 60 min, 1.5 kWh
]}
QUICK_FLOW = {"slug": "quick", "name": "Quick", "steps": [
    {"machine": MACHINE_WASH, "cycle_key": "rinse_spin"},     # 20 min, 0.1 kWh
    {"machine": MACHINE_DRY, "cycle_key": "cupboard_eco"},    # 120 min, 2.5 kWh
]}


def _two_loads(env, mode="now"):
    m = env["manager"]
    m._flows = [BIG_FLOW, QUICK_FLOW]
    m.set_mode(mode)
    m.set_row_selection(0, "Big")
    m.set_row_selection(1, "Quick")
    return m


class TestLoadOrder:
    def test_wash_and_dry_rows_form_one_load(self, laundry_env):
        m = laundry_env["manager"]
        m.set_row_selection(0, "Wash: Cotton 40")
        m.set_row_selection(1, "Dry: Cupboard Eco")
        m.set_row_selection(2, "Linen Sheets")
        loads = m._row_loads()
        assert [(rows, label) for rows, label, _ in loads] == [
            ([0, 1], "Cotton 40 → Cupboard Eco"), ([2], "Linen Sheets"),
        ]
        assert {s.load for s in loads[0][2]} == {"Cotton 40 → Cupboard Eco"}

    def test_optimiser_runs_short_wash_first(self, laundry_env):
        """Flat rates, so time decides: Quick→Big is done at +230 min (the long
        wash runs during the long dry), Big→Quick only at +300."""
        m = _two_loads(laundry_env)
        snap = m.snapshot()
        assert snap["order"]["order"] == ["Quick", "Big"]
        assert snap["order"]["reordered"] is True
        assert snap["order"]["minutes_sooner"] == 70
        assert [s["cycle_key"] for s in snap["steps"]][:2] == ["rinse_spin", "cupboard_eco"]
        assert snap["steps"][0]["load"] == "Quick"

    def test_as_listed_keeps_rows_but_reports_better_order(self, laundry_env):
        m = _two_loads(laundry_env)
        m.set_order_mode("As listed")
        snap = m.snapshot()
        assert snap["order"]["order"] == ["Big", "Quick"]
        assert snap["order"]["improves"] is True
        assert snap["order"]["optimised"] == ["Quick", "Big"]

    @pytest.mark.asyncio
    async def test_start_commits_optimised_order(self, laundry_env):
        m = _two_loads(laundry_env)
        await m.async_start()
        kwargs = laundry_env["washer_store"]["scheduler"].async_schedule.await_args.kwargs
        assert kwargs["cycle_id"] == "rinse_spin"
        assert [s["load"] for s in m._run["steps"]] == ["Quick", "Quick", "Big", "Big"]

    def test_search_keeps_listed_order_on_ties(self):
        assert LaundryManager._search_order(3, lambda p: 1.0) == (0, 1, 2)

    def test_local_search_beyond_exhaustive_limit(self):
        target = (5, 4, 3, 2, 1, 0)
        score = lambda p: sum(abs(a - b) for a, b in zip(p, target))  # noqa: E731
        assert LaundryManager._search_order(6, score) == target


class TestDropSuggestion:
    def test_expensive_load_suggested_for_another_day(self, laundry_env):
        """Cheap for 3 h, dear for 15 h, cheap again: Big's dry lands in the
        dear stretch, while on its own later it'd be cheap."""
        m = _two_loads(laundry_env)
        laundry_env["coordinator"].rates = make_rates(
            [0.05] * 6 + [0.50] * 30 + [0.05] * 12, start=NOW_FLOOR,
        )
        with patch("custom_components.smartstart.laundry._utcnow", return_value=NOW_FLOOR):
            m._choice_key = None
            m._refresh_preview()
            snap = m.snapshot()
        [tip] = snap["suggestions"]
        assert tip["kind"] == "expensive"
        assert tip["load"] == "Big"
        assert tip["rows"] == [0]
        assert tip["alone_cost"] < tip["extra_cost"]
        assert "another day" in tip["message"]

    def test_no_suggestion_when_rates_flat(self, laundry_env):
        m = _two_loads(laundry_env)
        assert m.snapshot()["suggestions"] == []


# ── Away periods ──────────────────────────────────────────────────────────────

class TestAway:
    def test_away_pushes_changeover_to_return(self, laundry_env):
        m = laundry_env["manager"]
        m._set_away([{"start": _local(13).isoformat(), "end": _local(22).isoformat()}])
        offsets, *_ = m._plan_shape(_wash_dry(), _local(12), None)
        # wash 12:00–13:00; changeover due 13:15 is inside the away period
        assert _local(12) + timedelta(minutes=offsets[1]) == _local(22)

    def test_changeover_before_leaving_is_kept(self, laundry_env):
        m = laundry_env["manager"]
        m._set_away([{"start": _local(13).isoformat(), "end": _local(22).isoformat()}])
        offsets, *_ = m._plan_shape(_wash_dry(), _local(11), None)
        assert offsets[1] == pytest.approx(75)  # 12:15, before lunch

    def test_open_ended_away_lasts_until_wake(self, laundry_env):
        m = _sleepy(laundry_env)
        m._set_away([{"start": _local(13).isoformat(), "end": None}])
        assert m.away[0]["until"] == _local(7, 0, 1).isoformat()
        assert m._available_from(_local(14), None) == _local(7, 0, 1)

    def test_away_then_sleep_chain(self, laundry_env):
        m = _sleepy(laundry_env)
        m._set_away([{"start": _local(20).isoformat(), "end": _local(23, 30).isoformat()}])
        assert m._available_from(_local(21), None) == _local(7, 0, 1)

    @pytest.mark.asyncio
    async def test_add_remove_and_prune(self, laundry_env):
        from homeassistant.exceptions import HomeAssistantError
        m = laundry_env["manager"]
        now = datetime.now(timezone.utc)
        await m.async_add_away(now + timedelta(hours=2), now + timedelta(hours=4))
        await m.async_add_away(now + timedelta(hours=1), None)
        assert len(m.away) == 2 and m.away[0]["end"] is None  # sorted by start
        await m.async_remove_away(0)
        assert len(m.away) == 1
        with pytest.raises(HomeAssistantError):
            await m.async_add_away(now + timedelta(hours=3), now + timedelta(hours=2))
        assert m._prune_away(now + timedelta(hours=5)) is True
        assert m.away == []


# ── Reschedule parity: wait limit, picked windows, offers ────────────────────

class TestRescheduleParity:
    @pytest.mark.asyncio
    async def test_envelope_is_wait_limit_from_press(self, laundry_env):
        m = await _start_simple(laundry_env)
        assert m._run["cap_kind"] == "wait"
        cap = datetime.fromisoformat(m._run["cap"])
        pressed = datetime.fromisoformat(m._run["committed_at"])
        assert cap - pressed == timedelta(hours=36)

    @pytest.mark.asyncio
    async def test_schedule_at_pins_then_moves_and_widens(self, laundry_env):
        m = laundry_env["manager"]
        m.set_row_selection(0, "Wash: Cotton 40")
        m.set_row_selection(1, "Dry: Cupboard Eco")
        at = NOW_FLOOR + timedelta(hours=5)
        await m.async_schedule_at(at)
        assert m._run["t0"] == at.isoformat()
        assert m._run["hold_until"] is None
        laundry_env["washer_status"].update({"status": STATE_SCHEDULED})
        m._run["cap"] = (NOW_FLOOR + timedelta(hours=6)).isoformat()  # tight envelope
        later = NOW_FLOOR + timedelta(hours=20)
        await m.async_schedule_at(later)
        assert m._run["t0"] == later.isoformat()
        laundry_env["washer_store"]["scheduler"].async_reschedule.assert_awaited()
        assert datetime.fromisoformat(m._run["cap"]) >= later + timedelta(minutes=195)

    @pytest.mark.asyncio
    async def test_schedule_at_refuses_running_plan(self, laundry_env):
        from homeassistant.exceptions import HomeAssistantError
        m = await _start_simple(laundry_env)
        m._run["status"] = LAUNDRY_STATE_RUNNING
        with pytest.raises(HomeAssistantError):
            await m.async_schedule_at(NOW_FLOOR + timedelta(hours=3))

    @pytest.mark.asyncio
    async def test_forecast_offer_found_and_accepted(self, laundry_env):
        c = laundry_env["coordinator"]
        real = make_rates([0.50] * 4 + [0.20] * 44, start=NOW_FLOOR)  # wait past the dear first hours
        forecast = make_rates([0.02] * 24, start=NOW_FLOOR + timedelta(hours=24))
        c.rates = real
        c.planner_rates = MagicMock(side_effect=lambda now: real + forecast)
        c.is_predicted_window = MagicMock(side_effect=lambda w: w.end > real[-1].end)
        m = laundry_env["manager"]
        m.set_mode("max_savings")
        m.set_row_selection(0, "Wash: Cotton 40")
        m.set_row_selection(1, "Dry: Cupboard Eco")
        await m.async_start()
        laundry_env["washer_status"].update({"status": STATE_SCHEDULED})

        m._update_offer(datetime.now(timezone.utc))
        offer = m.offer
        assert offer is not None and offer["predicted"] is True
        assert offer["saving"] >= 0.10
        assert datetime.fromisoformat(offer["start"]) >= real[-1].end - timedelta(hours=4)
        [ev] = _events(laundry_env["hass"], "smartstart_laundry_offer_found")
        assert ev["kind"] == "forecast"
        # Same offer again → not re-announced.
        m._update_offer(datetime.now(timezone.utc))
        assert len(_events(laundry_env["hass"], "smartstart_laundry_offer_found")) == 1

        await m.async_accept_offer()
        assert m._run["t0"] == offer["start"]
        assert datetime.fromisoformat(m._run["cap"]) >= datetime.fromisoformat(offer["end"])
        assert m.offer is None
        laundry_env["washer_store"]["scheduler"].async_reschedule.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_offer_when_auto_reschedule_would_take_it(self, laundry_env):
        c = laundry_env["coordinator"]
        m = await _start_simple(laundry_env)
        m._run["t0"] = (NOW_FLOOR + timedelta(hours=2)).isoformat()
        cheap = make_rates([0.20] * 10 + [0.02] * 38, start=NOW_FLOOR)
        c.rates = cheap
        c.planner_rates = MagicMock(side_effect=lambda now: cheap)
        m._update_offer(datetime.now(timezone.utc))
        assert m.offer is None  # real + inside the wait limit → auto-reschedule's job


class TestWetCapNowExemption:
    def test_only_mode_now_exempts_an_immediate_start(self, laundry_env):
        """22:00 wash → 07:00 dry is 8 h wet. Mode Now may start anyway;
        Balanced must not pick 'now' just because it's immediate."""
        m = _sleepy(laundry_env, cap=4)
        now = _local(22)
        m.set_mode("balanced")
        assert m._shape_fn(_wash_dry(), now, None)(now) is None
        m.set_mode("now")
        assert m._shape_fn(_wash_dry(), now, None)(now) is not None


# ── Smarter Continue / stalled steps / leaving heads-up ──────────────────────

async def _awaiting_dryer(env):
    m = await _start_simple(env)
    env["washer_status"].update({"status": STATE_RUNNING})
    await m._reconcile_device("washer")
    env["washer_status"].update({"status": STATE_IDLE})
    await m._reconcile_device("washer")
    assert m.status == LAUNDRY_STATE_AWAITING
    return m


class TestSmarterContinue:
    @pytest.mark.asyncio
    async def test_continue_delays_dry_to_cheap_slot_then_start_now(self, laundry_env):
        m = await _awaiting_dryer(laundry_env)
        laundry_env["coordinator"].rates = make_rates([0.50] * 6 + [0.05] * 42, start=NOW_FLOOR)
        opt = m._continue_option(datetime.now(timezone.utc))
        assert opt is not None and opt["saving"] >= 0.10
        start_at = datetime.fromisoformat(opt["start_at"])
        assert start_at >= NOW_FLOOR + timedelta(hours=3) - timedelta(minutes=30)

        await m.async_continue()
        kw = laundry_env["dryer_store"]["scheduler"].async_schedule.await_args.kwargs
        assert abs((kw["window"].start - start_at).total_seconds()) < 5
        snap = m.snapshot()
        assert snap["continue_delay"] is not None
        assert snap["next_action"].startswith("Loaded")

        await m.async_start_now()
        laundry_env["dryer_store"]["scheduler"].async_reschedule.assert_awaited()
        assert m.snapshot()["continue_delay"] is None

    @pytest.mark.asyncio
    async def test_continue_start_now_skips_delay(self, laundry_env):
        m = await _awaiting_dryer(laundry_env)
        laundry_env["coordinator"].rates = make_rates([0.50] * 6 + [0.05] * 42, start=NOW_FLOOR)
        await m.async_continue(start_now=True)
        kw = laundry_env["dryer_store"]["scheduler"].async_schedule.await_args.kwargs
        assert abs((kw["window"].start - datetime.now(timezone.utc)).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_no_delay_when_rates_flat_or_wet_cap_tight(self, laundry_env):
        m = await _awaiting_dryer(laundry_env)
        assert m._continue_option(datetime.now(timezone.utc)) is None  # flat 20p
        laundry_env["coordinator"].rates = make_rates([0.50] * 6 + [0.05] * 42, start=NOW_FLOOR)
        m._wet_cap_minutes = 10  # cap + gap < the 30-min grid: washing can't wait
        assert m._continue_option(datetime.now(timezone.utc)) is None


class TestStalledAndLeaving:
    @pytest.mark.asyncio
    async def test_stalled_step_announced_once(self, laundry_env):
        m = await _start_simple(laundry_env)
        later = datetime.now(timezone.utc) + timedelta(minutes=40)
        assert m._stalled(later)
        m._check_stalled(later)
        m._check_stalled(later + timedelta(minutes=1))
        [ev] = _events(laundry_env["hass"], "smartstart_laundry_step_stalled")
        assert ev["machine"] == MACHINE_WASH and "isn't running" in ev["message"]

    @pytest.mark.asyncio
    async def test_leaving_heads_up(self, laundry_env):
        m = await _start_simple(laundry_env)
        laundry_env["washer_status"].update({"status": STATE_RUNNING})
        await m._reconcile_device("washer")
        now = datetime.now(timezone.utc)
        m._set_away([{"start": (now + timedelta(minutes=30)).isoformat(),
                      "end": (now + timedelta(hours=5)).isoformat()}])
        m._check_leaving(now)
        m._check_leaving(now + timedelta(minutes=1))
        [ev] = _events(laundry_env["hass"], "smartstart_laundry_leaving")
        assert "No changeover due before you go" in ev["message"]
        assert "Next once you're back" in ev["message"]

    def test_changeover_after_is_an_away_period(self, laundry_env):
        m = laundry_env["manager"]
        until = datetime.now(timezone.utc) + timedelta(hours=3)
        m.set_unavailable_until(until)
        assert m.unavailable_until == until.replace(microsecond=until.microsecond)
        assert m.away[0]["kind"] == "until"
        m.set_unavailable_until(until + timedelta(hours=1))
        assert len(m.away) == 1  # replaced, not stacked
        m.set_unavailable_until(None)
        assert m.away == [] and m.unavailable_until is None


class TestInUseOvernight:
    def _linen_overnight(self, env):
        m = _sleepy(env)
        m._flows = [{**LINEN_FLOW, "overnight": True}, BIG_FLOW]
        return m

    def test_flow_marks_first_step_attended(self, laundry_env):
        m = self._linen_overnight(laundry_env)
        steps = m._expand_flow(m._flows[0])
        assert steps[0].attended and not any(s.attended for s in steps[1:])

    def test_cannot_start_in_the_night_but_can_by_day(self, laundry_env):
        m = self._linen_overnight(laundry_env)
        steps = m._expand_flow(m._flows[0])
        fn = m._shape_fn(steps, _local(12), None)
        assert fn(_local(3, 0, 1)) is None        # 03:00 — you're asleep in them
        assert fn(_local(7, 30, 1)) is not None   # after wake
        assert fn(_local(15)) is not None         # daytime

    def test_optimiser_preloads_the_other_load_first(self, laundry_env):
        """Evening Start, cheap night: Big can be loaded now for a night
        start; the overnight linen must wait for the morning changeover."""
        m = self._linen_overnight(laundry_env)
        start = _local(22)
        laundry_env["coordinator"].rates = make_rates([0.30] * 4 + [0.05] * 16 + [0.30] * 30, start=start)
        m.set_mode("max_savings")
        m.set_row_selection(0, "Linen Sheets")
        m.set_row_selection(1, "Big")
        with patch("custom_components.smartstart.laundry._utcnow", return_value=start):
            m._choice_key = None
            m._refresh_preview()
            snap = m.snapshot()
        assert snap["order"]["order"][0] == "Big"
        linen = next(s for s in snap["steps"] if s["load"] == "Linen Sheets")
        assert datetime.fromisoformat(linen["planned_start"]) >= _local(7, 0, 1)
