"""Active cycle selector + Cycle Due By deadline selector."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import logging
from typing import Dict, List, Optional

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEFAULT_MAX_DELAY_MINUTES,
    DEFAULT_MODE,
    DEFAULT_POWER_DOWN,
    DOMAIN,
    MODE_BALANCED,
    MODE_MAX_SAVINGS,
    MODE_NOW,
    POWER_DOWN_AVOID,
    POWER_DOWN_IGNORE,
    STATE_IDLE,
)
from .coordinator import SmartStartCoordinator


def _reject_if_committed(coordinator, slug: str, what: str) -> None:
    """Raise if the device's schedule is not idle. Used by all three selects
    to keep displayed config aligned with the committed schedule. The user's
    escape hatch is the Stop button — which clears the schedule and returns
    status to idle, at which point the select becomes editable again."""
    status = coordinator.status_for(slug)
    if status != STATE_IDLE:
        raise ServiceValidationError(
            f"{what} cannot be changed while status is {status}. "
            "Press Stop first to clear the schedule."
        )

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    if "laundry" in store:
        from homeassistant.helpers import entity_registry as er
        from .laundry_entities import build_selects, legacy_row_unique_ids
        # Rows merged from wash+dry select pairs into one "Load N" select —
        # drop the orphaned pre-merge entities.
        registry = er.async_get(hass)
        for uid in legacy_row_unique_ids(store["laundry"]):
            if entity_id := registry.async_get_entity_id("select", DOMAIN, uid):
                registry.async_remove(entity_id)
        async_add_entities(build_selects(store["laundry"]))
        return
    coordinator: SmartStartCoordinator = store["coordinator"]
    config = entry.options if entry.options else entry.data

    entities: List[SelectEntity] = []
    for device in config.get("devices", []):
        if len(device.get("cycles", [])) >= 2:
            entities.append(CycleSelectEntity(coordinator, device))
        entities.append(CycleDueAtSelectEntity(coordinator, device))
        entities.append(SchedulingModeSelectEntity(coordinator, device))
        entities.append(PowerDownSelectEntity(coordinator, device))
    async_add_entities(entities)


class CycleSelectEntity(SelectEntity, RestoreEntity):
    """Selects which cycle's data the device-level sensors show."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SmartStartCoordinator, device: Dict) -> None:
        self._coordinator = coordinator
        self._device      = device
        self._slug        = device["device_slug"]
        self._cycles      = device.get("cycles", [])

        self._attr_unique_id      = f"smartstart_{self._slug}_cycle_select"
        self._attr_name           = "Cycle"
        self._attr_icon           = "mdi:tune"
        self._attr_options        = [c["name"] for c in self._cycles]
        self._attr_current_option = self._cycles[0]["name"] if self._cycles else None
        self._attr_device_info    = DeviceInfo(
            identifiers={(DOMAIN, self._slug)},
            name=device["name"],
            manufacturer="SmartStart",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in self._attr_options:
            self._attr_current_option = last.state
            _LOGGER.debug("SmartStart: %s restored cycle → %s", self._slug, last.state)
        self._push_selection()

    async def async_select_option(self, option: str) -> None:
        _reject_if_committed(self._coordinator, self._slug, "Cycle")
        self._attr_current_option = option
        self._push_selection()
        self.async_write_ha_state()
        _LOGGER.info("SmartStart: %s active cycle → %s", self._slug, option)
        # Refresh coordinator so all device sensors immediately reflect the new cycle
        await self._coordinator.async_refresh()

    def _push_selection(self) -> None:
        selected_id = next(
            (c["cycle_key"] for c in self._cycles if c["name"] == self._attr_current_option),
            self._cycles[0]["cycle_key"] if self._cycles else None,
        )
        if selected_id:
            self._coordinator.set_selected_cycle(self._slug, selected_id)


_NO_DEADLINE = "No deadline"
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class CycleDueAtSelectEntity(CoordinatorEntity, SelectEntity):
    """Optional deadline picker: 'Cycle Due By {label}'.

    Options auto-generated on each render from current time, configured
    max_delay, currently-selected cycle's duration, and loaded rate horizon.
    Hourly granularity. Cleared after any Start button press.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: SmartStartCoordinator, device: Dict) -> None:
        super().__init__(coordinator)
        self._device = device
        self._slug   = device["device_slug"]
        self._attr_unique_id   = f"smartstart_{self._slug}_cycle_due_at"
        self._attr_name        = "Cycle Due By"
        self._attr_icon        = "mdi:calendar-clock"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._slug)},
            name=device["name"],
            manufacturer="SmartStart",
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _max_delay_minutes(self) -> int:
        return int(self._device.get("max_delay_minutes", DEFAULT_MAX_DELAY_MINUTES))

    def _cycle_duration_minutes(self) -> int:
        """Use the currently-selected cycle's duration (learned avg if available),
        falling back to the device's longest configured cycle."""
        cd = self.coordinator.data or {}
        device_data = cd.get(self._slug, {})
        selected_cid = self.coordinator.get_selected_cycle_id(self._slug, self._device)
        cycles_data = device_data.get("cycles", {})
        if selected_cid and selected_cid in cycles_data:
            d = cycles_data[selected_cid].get("duration_minutes")
            if d:
                return int(round(d))
        configured = [int(c.get("duration_minutes", 60)) for c in self._device.get("cycles", [])]
        return max(configured) if configured else 60

    def _format_label(self, dt_local: datetime) -> str:
        today = datetime.now().astimezone().date()
        d = dt_local.date()
        if d == today:
            prefix = "Today"
        elif d == today + timedelta(days=1):
            prefix = "Tomorrow"
        else:
            prefix = _WEEKDAYS[dt_local.weekday()]
        return f"{prefix} {dt_local.strftime('%H:%M')}"

    def _parse_label(self, label: str) -> Optional[datetime]:
        """Inverse of _format_label — resolves to a tz-aware datetime."""
        if not label or " " not in label:
            return None
        prefix, time_str = label.rsplit(" ", 1)
        try:
            hh, mm = (int(x) for x in time_str.split(":", 1))
        except ValueError:
            return None
        local_now = datetime.now().astimezone()
        today = local_now.date()
        if prefix == "Today":
            d = today
        elif prefix == "Tomorrow":
            d = today + timedelta(days=1)
        elif prefix in _WEEKDAYS:
            target = _WEEKDAYS.index(prefix)
            days_ahead = (target - local_now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            d = today + timedelta(days=days_ahead)
        else:
            return None
        return datetime.combine(d, time(hh, mm), tzinfo=local_now.tzinfo)

    def _build_options(self) -> List[str]:
        opts = [_NO_DEADLINE]
        now = datetime.now(timezone.utc)
        duration = self._cycle_duration_minutes()
        earliest = (now + timedelta(minutes=duration)).astimezone()
        # Round earliest up to next whole local hour
        earliest = earliest.replace(minute=0, second=0, microsecond=0)
        if earliest <= (now + timedelta(minutes=duration)).astimezone():
            earliest += timedelta(hours=1)

        latest_by_delay = (now + timedelta(minutes=self._max_delay_minutes())).astimezone()
        rates = self.coordinator.rates
        latest_by_rates = rates[-1].end.astimezone() if rates else latest_by_delay
        latest = min(latest_by_delay, latest_by_rates)

        cursor = earliest
        while cursor <= latest:
            opts.append(self._format_label(cursor))
            cursor += timedelta(hours=1)
        return opts

    # ── SelectEntity ─────────────────────────────────────────────────────────

    @property
    def options(self) -> List[str]:
        return self._build_options()

    @property
    def current_option(self) -> str:
        dt = self.coordinator.get_cycle_due_at(self._slug)
        if dt is None:
            return _NO_DEADLINE
        local = dt.astimezone().replace(minute=0, second=0, microsecond=0)
        label = self._format_label(local)
        # Only return label if it's still in the current options list; otherwise
        # the deadline is stale (past the horizon, or unreachable). Show "No deadline".
        return label if label in self._build_options() else _NO_DEADLINE

    async def async_select_option(self, option: str) -> None:
        _reject_if_committed(self.coordinator, self._slug, "Cycle Due By")
        if option == _NO_DEADLINE:
            self.coordinator.set_cycle_due_at(self._slug, None)
        else:
            dt = self._parse_label(option)
            if dt is not None:
                self.coordinator.set_cycle_due_at(self._slug, dt)
        await self.coordinator.async_refresh()


_MODE_LABELS = {
    MODE_MAX_SAVINGS: "Max Savings",
    MODE_BALANCED:    "Balanced",
    MODE_NOW:         "Now",
}
_LABEL_TO_MODE = {v: k for k, v in _MODE_LABELS.items()}


class SchedulingModeSelectEntity(CoordinatorEntity, SelectEntity, RestoreEntity):
    """Per-device scheduling mode: Max Savings / Balanced / Now.

    Restored from HA state on startup so user's choice persists across restarts.
    Pushes the value into the coordinator so _build_data can read it on each tick.
    """

    _attr_has_entity_name = True
    _attr_options = list(_MODE_LABELS.values())

    def __init__(self, coordinator: SmartStartCoordinator, device: Dict) -> None:
        super().__init__(coordinator)
        self._device = device
        self._slug   = device["device_slug"]
        self._attr_unique_id      = f"smartstart_{self._slug}_scheduling_mode"
        self._attr_name           = "Scheduling Mode"
        self._attr_icon           = "mdi:tune-variant"
        self._attr_current_option = _MODE_LABELS[DEFAULT_MODE]
        self._attr_device_info    = DeviceInfo(
            identifiers={(DOMAIN, self._slug)},
            name=device["name"],
            manufacturer="SmartStart",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in self._attr_options:
            self._attr_current_option = last.state
        # Push to coordinator so build_data sees it on first refresh
        self.coordinator.set_scheduling_mode(
            self._slug, _LABEL_TO_MODE.get(self._attr_current_option, DEFAULT_MODE),
        )

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            return
        _reject_if_committed(self.coordinator, self._slug, "Scheduling Mode")
        self._attr_current_option = option
        self.coordinator.set_scheduling_mode(
            self._slug, _LABEL_TO_MODE[option],
        )
        self.async_write_ha_state()
        await self.coordinator.async_refresh()


_POWER_DOWN_LABELS = {
    POWER_DOWN_AVOID:  "Avoid",
    POWER_DOWN_IGNORE: "Ignore",
}
_LABEL_TO_POWER_DOWN = {v: k for k, v in _POWER_DOWN_LABELS.items()}


class PowerDownSelectEntity(CoordinatorEntity, SelectEntity, RestoreEntity):
    """Per-device handling of blackout windows (Octopus power-down / saving
    sessions): Avoid never schedules a cycle over one; Ignore does as before.

    The panel drives this same entity, so the HA dropdown and the panel
    control are one source of state. Restored on startup; read by the
    coordinator through `blackouts_for`.
    """

    _attr_has_entity_name = True
    _attr_options = list(_POWER_DOWN_LABELS.values())

    def __init__(self, coordinator: SmartStartCoordinator, device: Dict) -> None:
        super().__init__(coordinator)
        self._device = device
        self._slug   = device["device_slug"]
        self._attr_unique_id      = f"smartstart_{self._slug}_power_down"
        self._attr_name           = "Power-down Windows"
        self._attr_icon           = "mdi:flash-off"
        self._attr_current_option = _POWER_DOWN_LABELS[DEFAULT_POWER_DOWN]
        self._attr_device_info    = DeviceInfo(
            identifiers={(DOMAIN, self._slug)},
            name=device["name"],
            manufacturer="SmartStart",
        )

    @property
    def extra_state_attributes(self) -> Dict:
        return {"calendars": self.coordinator.blackout_calendars}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in self._attr_options:
            self._attr_current_option = last.state
        self.coordinator.set_power_down_handling(
            self._slug,
            _LABEL_TO_POWER_DOWN.get(self._attr_current_option, DEFAULT_POWER_DOWN),
        )

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            return
        _reject_if_committed(self.coordinator, self._slug, "Power-down Windows")
        self._attr_current_option = option
        self.coordinator.set_power_down_handling(self._slug, _LABEL_TO_POWER_DOWN[option])
        self.async_write_ha_state()
        await self.coordinator.async_refresh()
