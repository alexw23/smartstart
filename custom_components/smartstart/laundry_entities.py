"""Entities for a laundry pair's virtual device.

Selects (plan rows / mode / due-by), buttons (Start / Continue / Cancel) and
sensors (status + planned figures). All state lives in the LaundryManager;
entities are thin views wired to its listener callback.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import List, Optional
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DEFAULT_MODE,
    DEFAULT_POWER_DOWN,
    POWER_DOWN_AVOID,
    POWER_DOWN_IGNORE,
    DOMAIN,
    LAUNDRY_ORDER_AS_LISTED,
    LAUNDRY_ORDER_OPTIMISED,
    LAUNDRY_ROWS,
    MODE_BALANCED,
    MODE_MAX_SAVINGS,
    MODE_NOW,
    ROW_NONE_LABEL,
)
from .laundry import LaundryManager

_LOGGER = logging.getLogger(__name__)

_MODE_LABELS = {
    MODE_MAX_SAVINGS: "Max Savings",
    MODE_BALANCED:    "Balanced",
    MODE_NOW:         "Now",
}
_LABEL_TO_MODE = {v: k for k, v in _MODE_LABELS.items()}

_NO_DEADLINE = "No deadline"
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class _LaundryBase(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: LaundryManager) -> None:
        self._manager = manager
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"laundry_{manager.slug}")},
            name=manager.name,
            manufacturer="SmartStart",
            model="Laundry pair",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._manager.add_listener(self._on_change))

    @callback
    def _on_change(self) -> None:
        self.async_write_ha_state()


# ── Selects ───────────────────────────────────────────────────────────────────

def legacy_row_unique_ids(manager: LaundryManager) -> List[str]:
    """Unique ids of the pre-merge per-row wash/dry selects (3 rows)."""
    return [
        f"smartstart_laundry_{manager.slug}_row{row + 1}_{col}"
        for row in range(3) for col in ("wash", "dry")
    ]


class RowLoadSelect(_LaundryBase, SelectEntity, RestoreEntity):
    """One "Load N" dropdown: saved flows plus single wash / dry cycles."""

    def __init__(self, manager: LaundryManager, row: int) -> None:
        super().__init__(manager)
        self._row = row
        self._attr_unique_id = f"smartstart_laundry_{manager.slug}_load{row + 1}"
        self._attr_name = f"Load {row + 1}"
        self._attr_icon = "mdi:washing-machine"

    @property
    def options(self) -> List[str]:
        opts = self._manager.row_options()
        current = self._manager.get_row_selection(self._row)
        if current not in opts:
            opts.append(current)  # keep a stale selection visible rather than erroring
        return opts

    @property
    def current_option(self) -> str:
        return self._manager.get_row_selection(self._row)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if (
            last
            and last.state
            and last.state not in ("unavailable", "unknown")
            and self._manager.get_row_selection(self._row) == ROW_NONE_LABEL
        ):
            self._manager.set_row_selection(self._row, last.state)

    async def async_select_option(self, option: str) -> None:
        self._manager.set_row_selection(self._row, option)
        self.async_write_ha_state()


class LaundryModeSelect(_LaundryBase, SelectEntity, RestoreEntity):
    _attr_options = list(_MODE_LABELS.values())

    def __init__(self, manager: LaundryManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"smartstart_laundry_{manager.slug}_scheduling_mode"
        self._attr_name = "Scheduling Mode"
        self._attr_icon = "mdi:tune-variant"

    @property
    def current_option(self) -> str:
        return _MODE_LABELS.get(self._manager.mode, _MODE_LABELS[DEFAULT_MODE])

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in self._attr_options:
            self._manager.set_mode(_LABEL_TO_MODE[last.state])

    async def async_select_option(self, option: str) -> None:
        if option in self._attr_options:
            self._manager.set_mode(_LABEL_TO_MODE[option])
            self.async_write_ha_state()


_POWER_DOWN_LABELS = {POWER_DOWN_AVOID: "Avoid", POWER_DOWN_IGNORE: "Ignore"}
_LABEL_TO_POWER_DOWN = {v: k for k, v in _POWER_DOWN_LABELS.items()}


class LaundryPowerDownSelect(_LaundryBase, SelectEntity, RestoreEntity):
    """Avoid: no wash or dry step runs through a power-down; a changeover can
    wait it out, so the plan splits around it. Ignore: plan as before.
    Same entity the panel drives."""

    _attr_options = list(_POWER_DOWN_LABELS.values())

    def __init__(self, manager: LaundryManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"smartstart_laundry_{manager.slug}_power_down"
        self._attr_name = "Power-down Windows"
        self._attr_icon = "mdi:flash-off"

    @property
    def current_option(self) -> str:
        return _POWER_DOWN_LABELS.get(self._manager.power_down, _POWER_DOWN_LABELS[DEFAULT_POWER_DOWN])

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in self._attr_options:
            self._manager.set_power_down(_LABEL_TO_POWER_DOWN[last.state])

    async def async_select_option(self, option: str) -> None:
        if option in self._attr_options:
            self._manager.set_power_down(_LABEL_TO_POWER_DOWN[option])
            self.async_write_ha_state()


class LaundryDueBySelect(_LaundryBase, SelectEntity):
    """Deadline for the PLAN END (the last dry). Hourly options across the
    loaded rate horizon; cleared automatically after each Start."""

    def __init__(self, manager: LaundryManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"smartstart_laundry_{manager.slug}_due_by"
        self._attr_name = "Due By"
        self._attr_icon = "mdi:calendar-clock"

    @staticmethod
    def _format_label(dt_local: datetime) -> str:
        today = datetime.now().astimezone().date()
        d = dt_local.date()
        if d == today:
            prefix = "Today"
        elif d == today + timedelta(days=1):
            prefix = "Tomorrow"
        else:
            prefix = _WEEKDAYS[dt_local.weekday()]
        return f"{prefix} {dt_local.strftime('%H:%M')}"

    @staticmethod
    def _parse_label(label: str) -> Optional[datetime]:
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
            days_ahead = (target - local_now.weekday()) % 7 or 7
            d = today + timedelta(days=days_ahead)
        else:
            return None
        return datetime.combine(d, time(hh, mm), tzinfo=local_now.tzinfo)

    def _build_options(self) -> List[str]:
        opts = [_NO_DEADLINE]
        now = datetime.now(timezone.utc)
        earliest = (now + timedelta(hours=1)).astimezone().replace(
            minute=0, second=0, microsecond=0
        ) + timedelta(hours=1)
        coordinator = self._manager.washer_coordinator()
        rates = coordinator.rates if coordinator else []
        latest = rates[-1].end.astimezone() if rates else (now + timedelta(hours=24)).astimezone()
        cursor = earliest
        while cursor <= latest:
            opts.append(self._format_label(cursor))
            cursor += timedelta(hours=1)
        return opts

    @property
    def options(self) -> List[str]:
        return self._build_options()

    @property
    def current_option(self) -> str:
        dt = self._manager.due_at
        if dt is None:
            return _NO_DEADLINE
        label = self._format_label(dt.astimezone().replace(minute=0, second=0, microsecond=0))
        return label if label in self._build_options() else _NO_DEADLINE

    async def async_select_option(self, option: str) -> None:
        if option == _NO_DEADLINE:
            self._manager.set_due_at(None)
        else:
            dt = self._parse_label(option)
            if dt is not None:
                self._manager.set_due_at(dt)
        self.async_write_ha_state()


class LaundryChangeoverSelect(_LaundryBase, SelectEntity):
    """"Changeover After": per-plan override of when you're next around to
    move laundry (lie-in, out until 9pm). Default = the nightly sleep window
    from the pair options. Cleared automatically after each Start."""

    _HOURS_AHEAD = 36

    def __init__(self, manager: LaundryManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"smartstart_laundry_{manager.slug}_changeover_after"
        self._attr_name = "Changeover After"
        self._attr_icon = "mdi:bed-clock"

    def _default_label(self) -> str:
        return f"Default (up {self._manager.wake_time.strftime('%H:%M')})"

    def _build_options(self) -> List[str]:
        opts = [self._default_label()]
        start = (datetime.now(timezone.utc) + timedelta(hours=1)).astimezone().replace(
            minute=0, second=0, microsecond=0
        )
        for h in range(self._HOURS_AHEAD):
            opts.append(LaundryDueBySelect._format_label(start + timedelta(hours=h)))
        return opts

    @property
    def options(self) -> List[str]:
        return self._build_options()

    @property
    def current_option(self) -> str:
        dt = self._manager.unavailable_until
        if dt is None:
            return self._default_label()
        label = LaundryDueBySelect._format_label(
            dt.astimezone().replace(minute=0, second=0, microsecond=0)
        )
        return label if label in self._build_options() else self._default_label()

    async def async_select_option(self, option: str) -> None:
        if option.startswith("Default"):
            self._manager.set_unavailable_until(None)
        else:
            dt = LaundryDueBySelect._parse_label(option)
            if dt is not None:
                self._manager.set_unavailable_until(dt)
        self.async_write_ha_state()


class LaundryOrderSelect(_LaundryBase, SelectEntity):
    """"Load Order": Optimised lets Start run the Loads in whichever order
    finishes best (cost + time blend); As listed keeps the row order."""

    _attr_options = [LAUNDRY_ORDER_OPTIMISED, LAUNDRY_ORDER_AS_LISTED]

    def __init__(self, manager: LaundryManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"smartstart_laundry_{manager.slug}_load_order"
        self._attr_name = "Load Order"
        self._attr_icon = "mdi:sort"

    @property
    def current_option(self) -> str:
        return self._manager.order_mode

    async def async_select_option(self, option: str) -> None:
        self._manager.set_order_mode(option)
        self.async_write_ha_state()


# ── Buttons ───────────────────────────────────────────────────────────────────

class LaundryStartButton(_LaundryBase, ButtonEntity):
    def __init__(self, manager: LaundryManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"smartstart_laundry_{manager.slug}_start"
        self._attr_name = "Start"
        self._attr_icon = "mdi:play-circle"

    async def async_press(self) -> None:
        await self._manager.async_start()


class LaundryContinueButton(_LaundryBase, ButtonEntity):
    """The changeover confirmation. Greyed out except when a trip is awaited."""

    def __init__(self, manager: LaundryManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"smartstart_laundry_{manager.slug}_continue"
        self._attr_name = "Continue"
        self._attr_icon = "mdi:swap-horizontal-circle"

    @property
    def available(self) -> bool:
        return self._manager.continue_available()

    async def async_press(self) -> None:
        await self._manager.async_continue()


class LaundryCancelButton(_LaundryBase, ButtonEntity):
    def __init__(self, manager: LaundryManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"smartstart_laundry_{manager.slug}_cancel"
        self._attr_name = "Cancel"
        self._attr_icon = "mdi:stop-circle"

    async def async_press(self) -> None:
        await self._manager.async_cancel()


class LaundryAcceptOfferButton(_LaundryBase, ButtonEntity):
    """Move the scheduled plan to its current offer. Available only while
    there is one."""

    def __init__(self, manager: LaundryManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"smartstart_laundry_{manager.slug}_accept_offer"
        self._attr_name = "Accept Offer"
        self._attr_icon = "mdi:cash-clock"

    @property
    def available(self) -> bool:
        return self._manager.offer is not None

    async def async_press(self) -> None:
        await self._manager.async_accept_offer()


# ── Sensors ───────────────────────────────────────────────────────────────────

class LaundryStatusSensor(_LaundryBase, SensorEntity):
    """Run status; attributes carry the full step plan for card rendering."""

    def __init__(self, manager: LaundryManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"smartstart_laundry_{manager.slug}_status"
        self._attr_name = "Status"
        self._attr_icon = "mdi:washing-machine"

    @property
    def native_value(self) -> str:
        return self._manager.status

    @property
    def extra_state_attributes(self) -> dict:
        return self._manager.snapshot()


class _SnapshotSensor(_LaundryBase, SensorEntity):
    """Base for sensors that surface one snapshot() field."""

    _field: str = ""

    def __init__(self, manager: LaundryManager) -> None:
        super().__init__(manager)
        self._attr_unique_id = f"smartstart_laundry_{manager.slug}_{self._field}"

    def _value(self):
        return self._manager.snapshot().get(self._field)


class LaundryPlannedStartSensor(_SnapshotSensor):
    _field = "planned_start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_name = "Planned Start"
    _attr_icon = "mdi:clock-start"

    @property
    def native_value(self) -> Optional[datetime]:
        raw = self._value()
        return datetime.fromisoformat(raw) if raw else None

    @property
    def extra_state_attributes(self) -> dict:
        """Same attribute names as the appliance Planned Start sensors, so the
        dashboard analysis cards work unchanged for the pair."""
        snap = self._manager.snapshot()
        rate = snap.get("plan_rate")
        return {
            "estimated_cost":        snap.get("plan_cost"),
            "estimated_savings":     snap.get("estimated_savings"),
            "avg_rate_cost_per_kwh": round(rate, 4) if rate is not None else None,
            "now_avg_rate_cost_per_kwh": (
                round(snap["now_rate"], 4) if snap.get("now_rate") is not None else None
            ),
        }


class LaundryPlannedFinishSensor(_SnapshotSensor):
    _field = "planned_finish"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_name = "Planned Finish"
    _attr_icon = "mdi:clock-end"

    @property
    def native_value(self) -> Optional[datetime]:
        raw = self._value()
        return datetime.fromisoformat(raw) if raw else None


class LaundryPlannedCostSensor(_SnapshotSensor):
    _field = "plan_cost"
    _attr_name = "Planned Cost"
    _attr_icon = "mdi:cash"

    @property
    def native_value(self) -> Optional[float]:
        return self._value()

    @property
    def native_unit_of_measurement(self) -> Optional[str]:
        coordinator = self._manager.washer_coordinator()
        return getattr(coordinator, "currency_symbol", None) or "£"


class LaundryPlannedRateSensor(_SnapshotSensor):
    _field = "plan_rate"
    _attr_name = "Planned Rate"
    _attr_icon = "mdi:chart-line"

    @property
    def native_value(self) -> Optional[float]:
        val = self._value()
        return round(val, 4) if val is not None else None

    @property
    def native_unit_of_measurement(self) -> str:
        coordinator = self._manager.washer_coordinator()
        # plan_rate is £/kWh (major units); rate_unit ("p") is display-only.
        unit = getattr(coordinator, "currency_symbol", None) or "£"
        return f"{unit}/kWh"


class LaundryBestWindowsSensor(_SnapshotSensor):
    """Cheapest distinct windows for the selected Load plan over real +
    forecast rates (planning view). State = the best start."""

    _field = "best_windows"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_name = "Best Windows"
    _attr_icon = "mdi:calendar-star"

    @property
    def native_value(self) -> Optional[datetime]:
        windows = self._value() or []
        return datetime.fromisoformat(windows[0]["start"]) if windows else None

    @property
    def extra_state_attributes(self) -> dict:
        return {"windows": self._value() or []}


class LaundryNextActionSensor(_SnapshotSensor):
    _field = "next_action"
    _attr_name = "Next Action"
    _attr_icon = "mdi:gesture-tap"

    @property
    def native_value(self) -> str:
        return self._value() or ""


class LaundryCurrentStepSensor(_SnapshotSensor):
    _field = "current_step"
    _attr_name = "Current Step"
    _attr_icon = "mdi:format-list-numbered"

    @property
    def native_value(self) -> str:
        return self._value() or "—"


# ── Builders (called from the platform setup functions) ──────────────────────

def build_selects(manager: LaundryManager) -> List[SelectEntity]:
    entities: List[SelectEntity] = []
    for row in range(LAUNDRY_ROWS):
        entities.append(RowLoadSelect(manager, row))
    entities.append(LaundryModeSelect(manager))
    entities.append(LaundryPowerDownSelect(manager))
    entities.append(LaundryDueBySelect(manager))
    entities.append(LaundryChangeoverSelect(manager))
    entities.append(LaundryOrderSelect(manager))
    return entities


def build_buttons(manager: LaundryManager) -> List[ButtonEntity]:
    return [
        LaundryStartButton(manager),
        LaundryContinueButton(manager),
        LaundryCancelButton(manager),
        LaundryAcceptOfferButton(manager),
    ]


def build_sensors(manager: LaundryManager) -> List[SensorEntity]:
    return [
        LaundryStatusSensor(manager),
        LaundryPlannedStartSensor(manager),
        LaundryPlannedFinishSensor(manager),
        LaundryPlannedCostSensor(manager),
        LaundryPlannedRateSensor(manager),
        LaundryNextActionSensor(manager),
        LaundryCurrentStepSensor(manager),
        LaundryBestWindowsSensor(manager),
    ]
