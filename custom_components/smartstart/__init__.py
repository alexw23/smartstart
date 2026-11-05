"""SmartStart — energy-aware appliance scheduler for Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)

from .const import (
    CONF_ENTRY_TYPE,
    CURRENCY_SYMBOLS,
    DOMAIN,
    ENTRY_TYPE_LAUNDRY,
    PROVIDER_OCTOPUS_ENTITY,
    RATE_UNITS_FOR_CURRENCY,
)
from .laundry import LaundryManager


def _derive_currency_symbol(hass: HomeAssistant) -> str:
    code = (hass.config.currency or "GBP").upper()
    return CURRENCY_SYMBOLS.get(code, code)


def _derive_rate_unit(hass: HomeAssistant) -> str:
    code = (hass.config.currency or "GBP").upper()
    return RATE_UNITS_FOR_CURRENCY.get(code, "")
from .coordinator import SmartStartCoordinator
from .curve_store import CurveStore
from .panel import async_setup_panel
from .run_history import RunHistoryStore
from .providers.manual import ManualProvider
from .providers.octopus_entity import OctopusEntityProvider
from .scheduler import SmartStartScheduler
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "button", "select"]


# ── Config entry setup ────────────────────────────────────────────────────────

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.warning("SmartStart: async_setup_entry running (entry=%s)", entry.entry_id)
    config = entry.options if entry.options else entry.data

    if config.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_LAUNDRY:
        return await _async_setup_laundry_entry(hass, entry, config)

    provider_type = config.get("provider", "manual")
    provider_cfg  = config.get("provider_config", {})

    if provider_type == PROVIDER_OCTOPUS_ENTITY:
        provider = OctopusEntityProvider(
            hass,
            current_day_entity=provider_cfg["current_day_entity"],
            next_day_entity=provider_cfg["next_day_entity"],
        )
    else:
        provider = ManualProvider()

    devices = config.get("devices", [])

    # Backfill cycle_key for cycles created before the cycle_key/cycle_id split.
    # cycle_key is the immutable internal identifier (curve storage, unique IDs).
    # cycle_id is now a user-editable payload string sent to the start script.
    for _device in devices:
        for _cycle in _device.get("cycles", []):
            if not _cycle.get("cycle_key"):
                _cycle["cycle_key"] = _cycle.get("cycle_id") or ""
        # Migrate legacy running_state (str|list) → running_states (list).
        # Drop stopped_state entirely — anything not running/paused is idle.
        if "running_states" not in _device:
            legacy = _device.get("running_state")
            if isinstance(legacy, list):
                _device["running_states"] = [s for s in legacy if s]
            elif legacy:
                _device["running_states"] = [legacy]
            else:
                _device["running_states"] = ["on"]
        _device.pop("running_state", None)
        _device.pop("stopped_state", None)
        # Migrate legacy delay_mode (native|managed) → delay_handling
        # (start_delay|end_time|managed). native→start_delay; managed stays.
        if "delay_handling" not in _device:
            legacy = _device.get("delay_mode")
            if legacy == "managed":
                _device["delay_handling"] = "managed"
            else:  # native or missing → start_delay
                _device["delay_handling"] = "start_delay"
        _device.pop("delay_mode", None)
        # Migrate legacy cancel_service → stop_service. Same semantics —
        # the user-supplied service fired when the user wants to abort.
        if "stop_service" not in _device and "cancel_service" in _device:
            _device["stop_service"] = _device.get("cancel_service", "")
        _device.pop("cancel_service", None)

        # Migrate legacy delay_granularity (minutes|whole_hours) → delay_step_minutes
        # (1 = minute precision, 60 = whole-hour reservation, etc.).
        if "delay_step_minutes" not in _device:
            legacy = _device.get("delay_granularity")
            if legacy == "whole_hours":
                _device["delay_step_minutes"] = 60
            else:  # "minutes" or missing
                _device["delay_step_minutes"] = 1
        _device.pop("delay_granularity", None)
        # Mode is runtime state (SelectEntity), not config. Only balanced_target_rate
        # lives in config — calibration value, rarely changed.
        _device.setdefault("balanced_target_rate", 0.05)
        # Auto-reschedule is opt-in — existing devices keep the notify-only
        # smartstart_better_window_found behaviour until enabled.
        _device.setdefault("auto_reschedule", False)
        _device.setdefault("hold_for_next_day_rates", False)
        _device.pop("mode", None)  # remove field from any old migration

    curve_store = CurveStore(hass, entry.entry_id)
    await curve_store.async_load()
    run_history = RunHistoryStore(hass, entry.entry_id)
    await run_history.async_load()
    await run_history.async_seed_from_curves(curve_store.raw_data)

    from .const import (
        CONF_CHEAP_WINDOW_THRESHOLD,
        CONF_HOLD_ENGAGE_LEAD,
        CONF_PREFER_SOONER_DIFF,
        CONF_RATES_EXPECTED_AT,
        CONF_RESCHEDULE_MIN_SAVING,
        DEFAULT_CHEAP_WINDOW_THRESHOLD,
        DEFAULT_HOLD_ENGAGE_LEAD,
        DEFAULT_RATES_EXPECTED_AT,
        DEFAULT_RESCHEDULE_MIN_SAVING,
        PREFER_SOONER_RATE_DIFF,
    )
    coordinator = SmartStartCoordinator(
        hass=hass,
        provider=provider,
        devices=devices,
        # Currency + rate-unit derive from HA's configured locale by default,
        # so most users never need to set them. Explicit stored values still
        # win — they exist for the rare case a user wants a non-standard
        # subunit ("c/kWh" rather than "¢/kWh", etc.).
        currency_symbol=config.get("currency_symbol") or _derive_currency_symbol(hass),
        rate_unit=config.get("rate_unit") or _derive_rate_unit(hass),
        curve_store=curve_store,
        prefer_sooner_diff=float(
            config.get(CONF_PREFER_SOONER_DIFF, PREFER_SOONER_RATE_DIFF)
        ),
        cheap_window_threshold=float(
            config.get(CONF_CHEAP_WINDOW_THRESHOLD, DEFAULT_CHEAP_WINDOW_THRESHOLD)
        ),
        reschedule_min_saving=float(
            config.get(CONF_RESCHEDULE_MIN_SAVING, DEFAULT_RESCHEDULE_MIN_SAVING)
        ),
        rates_expected_at=str(
            config.get(CONF_RATES_EXPECTED_AT, DEFAULT_RATES_EXPECTED_AT)
        ),
        hold_engage_lead=int(
            config.get(CONF_HOLD_ENGAGE_LEAD, DEFAULT_HOLD_ENGAGE_LEAD)
        ),
    )

    scheduler = SmartStartScheduler(hass, devices, curve_store, entry_id=entry.entry_id)
    scheduler.set_rate_lookup(coordinator.rate_at)
    scheduler.set_on_unscheduled(coordinator.clear_better_window_dedup)
    scheduler.set_rescore(coordinator.rescore_now)
    scheduler.set_run_history(run_history)
    coordinator.set_scheduler(scheduler)
    await scheduler.async_load()

    # Blackout windows (Octopus power-down etc.). Unset → auto-detect the
    # Octopus calendars; an explicit empty list turns the feature off.
    from .blackout import BlackoutSource, autodetect_calendars
    from .const import CONF_BLACKOUT_CALENDARS
    blackout_calendars = config.get(CONF_BLACKOUT_CALENDARS)
    if blackout_calendars is None:
        blackout_calendars = autodetect_calendars(hass)
    blackout_source = BlackoutSource(hass, blackout_calendars)
    await blackout_source.async_start()
    coordinator.set_blackout_source(blackout_source)

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "scheduler":   scheduler,
        "provider":    provider,
        "curve_store": curve_store,
        "run_history": run_history,
        "blackout":    blackout_source,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, "publish_rates"):
        await async_setup_services(hass)
    await async_setup_panel(hass)

    # ── Octopus rate-entity listeners ─────────────────────────────────────────
    # Re-fetch rates whenever the Octopus entities update so we don't wait up to
    # 30 minutes after HA starts for the entities to appear in the state machine.
    if isinstance(provider, OctopusEntityProvider):
        watch = [provider_cfg["current_day_entity"], provider_cfg["next_day_entity"]]

        # The two Octopus entities update milliseconds apart at publication.
        # Refreshing on the first event reads the second entity mid-update,
        # and HA's coordinator debouncer then drops the follow-up request as
        # "a call is already in progress" — leaving SmartStart on partial
        # (today-only) rates until the next 30-min tick. Coalesce instead:
        # (re)arm a short timer on every entity event and do ONE refresh
        # after the burst settles, bypassing the coordinator debouncer.
        pending_refresh: list = [None]

        @callback
        def _do_coalesced_refresh(_now) -> None:
            pending_refresh[0] = None
            hass.async_create_task(coordinator.async_refresh())

        @callback
        def _on_rate_entity_update(_event) -> None:
            if pending_refresh[0] is not None:
                pending_refresh[0]()
            pending_refresh[0] = async_call_later(hass, 2, _do_coalesced_refresh)

        @callback
        def _cancel_pending_refresh() -> None:
            if pending_refresh[0] is not None:
                pending_refresh[0]()
                pending_refresh[0] = None

        entry.async_on_unload(_cancel_pending_refresh)
        for eid in watch:
            entry.async_on_unload(
                async_track_state_change_event(hass, eid, _on_rate_entity_update)
            )
        _LOGGER.debug("SmartStart: listening for rate updates on %s", watch)

    # Re-score windows every minute so Optimal Savings Start / now-rate stay
    # current without re-fetching rates. Use track_time_change(second=0) so the
    # tick fires exactly on each wall-clock minute boundary — whole-hour devices
    # then produce clean "now + N hours" candidates (13:01 not 13:00:42).
    @callback
    def _tick(_now) -> None:
        coordinator.rescore_now()

    entry.async_on_unload(
        async_track_time_change(hass, _tick, second=0)
    )

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_setup_laundry_entry(
    hass: HomeAssistant, entry: ConfigEntry, config: dict
) -> bool:
    """A laundry entry pairs existing washer + dryer devices (usually owned
    by other entries) and schedules multi-step plans across them."""
    manager = LaundryManager(hass, entry.entry_id, config)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"laundry": manager}
    await manager.async_load()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, "publish_rates"):
        await async_setup_services(hass)

    # Minute tick keeps projections / Next Action fresh while a run is live,
    # and retries device subscriptions until the paired entries are loaded.
    entry.async_on_unload(
        async_track_time_change(hass, manager.minute_tick, second=0)
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.warning("SmartStart: async_unload_entry running (entry=%s)", entry.entry_id)
    store = hass.data[DOMAIN].get(entry.entry_id, {})
    laundry: LaundryManager | None = store.get("laundry")
    if laundry:
        await laundry.async_teardown()
    if blackout := store.get("blackout"):
        blackout.async_stop()
    scheduler: SmartStartScheduler | None = store.get("scheduler")
    if scheduler:
        await scheduler.async_teardown()

    try:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    except ValueError:
        # Platform was never fully loaded (e.g. partial setup failure) — safe to ignore
        unload_ok = True

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    _LOGGER.warning("SmartStart: update listener fired — reloading entry %s", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
    _LOGGER.warning("SmartStart: reload complete")
