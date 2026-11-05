"""Start + Cancel buttons for SmartStart devices."""
from __future__ import annotations

from datetime import datetime, timezone as tz
from typing import Dict, List, Optional
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_DELAY_STEP_MINUTES, DOMAIN, STATE_IDLE
from .coordinator import SmartStartCoordinator
from .scheduler import SmartStartScheduler
from .window_scorer import ScoredWindow

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    if "laundry" in store:
        from .laundry_entities import build_buttons
        async_add_entities(build_buttons(store["laundry"]))
        return
    coordinator: SmartStartCoordinator = store["coordinator"]
    scheduler: SmartStartScheduler    = store["scheduler"]
    config = entry.options if entry.options else entry.data

    entities: List[ButtonEntity] = []
    for device in config.get("devices", []):
        cycles = device.get("cycles", [])
        multi  = len(cycles) > 1

        # Device-level Start uses whichever cycle is currently selected and
        # respects the device's Scheduling Mode. One button is enough now —
        # Mode owns the "delay vs. immediate" policy.
        entities += [
            DeviceStartButton(coordinator, scheduler, device),
            StopButton(coordinator, scheduler, device),
            AcceptOfferButton(coordinator, device),
        ]

        # Per-cycle Start (disabled by default — enable to target a specific cycle)
        if multi:
            for cycle in cycles:
                entities.append(CycleStartButton(coordinator, scheduler, device, cycle))

    async_add_entities(entities)


# ── Shared scheduling logic ───────────────────────────────────────────────────

class _StartBase(ButtonEntity):
    """Base for Start buttons. Subclasses resolve which cycle to schedule."""

    def __init__(
        self,
        coordinator: SmartStartCoordinator,
        scheduler: SmartStartScheduler,
        device: Dict,
    ) -> None:
        self._coordinator = coordinator
        self._scheduler   = scheduler
        self._device      = device
        self._slug        = device["device_slug"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._slug)},
            name=device["name"],
            manufacturer="SmartStart",
        )

    def _resolve_cycle(self) -> Optional[Dict]:
        """Return the cycle config dict to schedule. Override in subclasses."""
        raise NotImplementedError

    def _cycle_data_for(self, cycle_id: str) -> Dict:
        return (
            (self._coordinator.data or {})
            .get(self._slug, {})
            .get("cycles", {})
            .get(cycle_id, {})
        )

    def _resolve_window(self, cycle_id: str) -> Optional[ScoredWindow]:
        """Always use the mode-aware chosen window. Falls back to best/now
        if chosen wasn't populated (shouldn't happen in practice)."""
        cd = self._cycle_data_for(cycle_id)
        return cd.get("chosen_window") or cd.get("best_window") or cd.get("now_window")

    def _now_rate(self, cycle_id: str) -> Optional[float]:
        rate = (self._coordinator.data or {}).get(self._slug, {}).get("current_rate")
        if rate is None:
            now_win = self._cycle_data_for(cycle_id).get("now_window")
            rate = now_win.avg_rate_cost_per_kwh if now_win else None
        return rate

    async def _schedule(self) -> None:
        # A live laundry run owns this appliance — refuse rather than let a
        # manual start collide with a step the plan is about to fire.
        from .laundry import find_claiming_manager
        claimer = find_claiming_manager(self.hass, self._slug)
        if claimer is not None:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError(
                f"{self._device['name']} is in use by the '{claimer.name}' "
                f"laundry run — cancel that run first"
            )

        cycle = self._resolve_cycle()
        if cycle is None:
            _LOGGER.error("SmartStart: %s — no cycle configured", self._slug)
            return

        cycle_key = cycle["cycle_key"]
        # User's start_service receives the editable cycle_id, or the cycle
        # name as a friendly fallback when cycle_id is blank.
        payload_cycle_id = cycle.get("cycle_id") or cycle.get("name", "")

        # Capture status BEFORE any state changes. If a schedule was already
        # committed and the user is overriding it, we tag the payload as a
        # reschedule so the user's script can react accordingly (e.g. LG
        # dryer needs to update an existing reservation rather than start
        # a fresh one).
        prior_status = self._coordinator.status_for(self._slug)
        is_reschedule = prior_status != STATE_IDLE

        # Pressing any start button updates the cycle selector
        self._coordinator.set_selected_cycle(self._slug, cycle_key)

        # Always refresh before resolving the window — stale coordinator data
        # from a previous poll can pick the wrong window.
        await self._coordinator.async_refresh()

        window = self._resolve_window(cycle_key)
        if window is None:
            _LOGGER.error(
                "SmartStart: no valid window for %s/%s — check rates are loaded",
                self._slug, cycle_key,
            )
            return

        actual_delay_s = max(0, int((window.start - datetime.now(tz.utc)).total_seconds()))
        mode = self._coordinator.get_scheduling_mode(self._slug)
        _LOGGER.info(
            "SmartStart: %s/%s — Start pressed (mode=%s), window %s→%s "
            "(actual delay %ds / %.1fmin at press time)",
            self._slug, cycle_key, mode,
            window.start.strftime("%H:%M"),
            window.end.strftime("%H:%M"),
            actual_delay_s, actual_delay_s / 60,
        )

        cd = self._cycle_data_for(cycle_key)
        estimated_duration = cd.get("duration_minutes") or cycle.get("duration_minutes")
        # Round delay_seconds to the appliance's step. Round-to-nearest so a
        # 27-min planned delay snaps to 30 on a 30-min-step washer, not 0.
        step_s = max(1, int(self._device.get("delay_step_minutes", DEFAULT_DELAY_STEP_MINUTES))) * 60
        raw_delay_s = window.delay_minutes * 60
        delay_seconds = int(round(raw_delay_s / step_s)) * step_s if step_s > 1 else raw_delay_s
        service_payload = {
            "delay_seconds":              delay_seconds,
            "cycle_id":                   payload_cycle_id,
            "start_time":                 window.start.isoformat(),
            "cycle_duration":             cycle.get("duration_minutes"),
            "estimated_cycle_duration":   estimated_duration,
            "rate_cost_per_kwh":          window.avg_rate_cost_per_kwh,
            "estimated_finish":           window.end.isoformat(),
            "reschedule":                 is_reschedule,
        }

        await self._scheduler.async_schedule(
            device_slug=self._slug,
            cycle_id=cycle_key,
            window=window,
            cycle_kwh=cycle["total_kwh"],
            now_rate=self._now_rate(cycle_key),
            start_service=self._device["start_service"],
            service_payload=service_payload,
            # Cycle Due By (if set) also caps auto-reschedule for this run.
            # Read before the clear below.
            reschedule_deadline=self._coordinator.get_cycle_due_at(self._slug),
            # Hold-for-rates placeholder marker (None unless the hold engaged)
            hold_until=cd.get("hold_until"),
            rate_predicted=bool(cd.get("chosen_is_predicted")),
        )

        # Deadlines are per-run intent — clear after any start so they don't
        # silently apply to the next cycle.
        self._coordinator.set_cycle_due_at(self._slug, None)


# ── Device-level Start ────────────────────────────────────────────────────────

class DeviceStartButton(_StartBase):
    """Schedules the currently-selected cycle using the device's Scheduling Mode."""

    @property
    def unique_id(self) -> str:
        # Reuse the historical "start_anytime" unique_id so dashboards and
        # recorder history that referenced the old Anytime button continue to
        # resolve to this combined button. Users on the old Now button will
        # need to update their cards once.
        return f"smartstart_{self._slug}_start_anytime"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Start"

    @property
    def icon(self) -> str:
        return "mdi:play-circle"

    def _resolve_cycle(self) -> Optional[Dict]:
        cycle_key = self._coordinator.get_selected_cycle_id(self._slug, self._device)
        cycles    = self._device.get("cycles", [])
        return next((c for c in cycles if c["cycle_key"] == cycle_key), cycles[0] if cycles else None)

    async def async_press(self) -> None:
        await self._schedule()


# ── Per-cycle Start (disabled by default; enable to target a specific cycle) ──

class CycleStartButton(_StartBase):
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, scheduler, device, cycle):
        super().__init__(coordinator, scheduler, device)
        self._cycle = cycle

    @property
    def unique_id(self) -> str:
        return f"smartstart_{self._slug}_{self._cycle['cycle_key']}_start_anytime"

    @property
    def name(self) -> str:
        return f"{self._device['name']} {self._cycle['name']} Start"

    @property
    def icon(self) -> str:
        return "mdi:play-circle-outline"

    def _resolve_cycle(self) -> Dict:
        return self._cycle

    async def async_press(self) -> None:
        await self._schedule()


# ── Accept offer ──────────────────────────────────────────────────────────────

class AcceptOfferButton(CoordinatorEntity, ButtonEntity):
    """Moves the scheduled run to the device's current offer. Available only
    while there is one — the dashboard/panel/notification counterpart of the
    smartstart.accept_offer service."""

    def __init__(self, coordinator: SmartStartCoordinator, device: Dict) -> None:
        super().__init__(coordinator)
        self._device = device
        self._slug = device["device_slug"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._slug)},
            name=device["name"],
            manufacturer="SmartStart",
        )

    @property
    def unique_id(self) -> str:
        return f"smartstart_{self._slug}_accept_offer"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Accept Offer"

    @property
    def icon(self) -> str:
        return "mdi:piggy-bank-outline"

    @property
    def available(self) -> bool:
        return self.coordinator.get_offer(self._slug) is not None

    async def async_press(self) -> None:
        await self.coordinator.async_accept_offer(self._slug)


# ── Stop ──────────────────────────────────────────────────────────────────────

class StopButton(ButtonEntity):
    """Stops an active schedule. The user's way to "unfreeze" Planned X
    sensors and re-enable the Mode / Cycle / Cycle Due By selects. Named
    Stop so it sits next to Start in the device entity list."""

    def __init__(
        self,
        coordinator: SmartStartCoordinator,
        scheduler: SmartStartScheduler,
        device: Dict,
    ) -> None:
        self._coordinator = coordinator
        self._scheduler   = scheduler
        self._device      = device
        self._slug        = device["device_slug"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._slug)},
            name=device["name"],
            manufacturer="SmartStart",
        )

    @property
    def unique_id(self) -> str:
        # Keep historical "_cancel" id so existing automations / dashboards
        # that referenced the previous button name continue to resolve.
        return f"smartstart_{self._slug}_cancel"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Stop"

    @property
    def icon(self) -> str:
        return "mdi:stop-circle"

    async def async_press(self) -> None:
        status = self._coordinator.status_for(self._slug)
        if status == STATE_IDLE:
            _LOGGER.debug("SmartStart: %s — Stop pressed but already idle", self._slug)
            return
        _LOGGER.info("SmartStart: %s — Stop pressed (status was %s)", self._slug, status)
        # Route through the smartstart.stop HA service so user-supplied
        # stop_service (script / button entity) fires before SmartStart
        # clears its schedule. One code path for both UI + automation.
        await self.hass.services.async_call(
            DOMAIN, "stop", {"device_slug": self._slug}, blocking=True,
        )
