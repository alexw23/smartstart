from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    EXTERNAL_CYCLE_KEY,
    EXTERNAL_CYCLE_NAME,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RUNNING,
)
from .coordinator import SmartStartCoordinator
from .curve_store import CurveStore
from .scheduler import SmartStartScheduler

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    if "laundry" in store:
        from .laundry_entities import build_sensors
        async_add_entities(build_sensors(store["laundry"]))
        return
    coordinator: SmartStartCoordinator = store["coordinator"]
    scheduler: SmartStartScheduler = store["scheduler"]
    config = entry.options if entry.options else entry.data
    currency_symbol = config.get("currency_symbol", "£")
    rate_unit = config.get("rate_unit", "p")

    curve_store: Optional[CurveStore] = store.get("curve_store")

    entities: List[SensorEntity] = []
    for device in config.get("devices", []):
        cycles     = device.get("cycles", [])
        multi_cycle = len(cycles) > 1
        entities += [
            StatusSensor(coordinator, scheduler, device, currency_symbol, rate_unit),
            CurrentRateSensor(coordinator, device, currency_symbol),
            # Planned X family — live recommendation while idle, frozen at
            # committed values when scheduled/running/paused. Cancel button
            # is the user's way to unfreeze.
            PlannedStartSensor(coordinator, scheduler, device, rate_unit, currency_symbol),
            PlannedFinishSensor(coordinator, scheduler, device),
            PlannedCostSensor(coordinator, scheduler, device, currency_symbol),
            PlannedRateSensor(coordinator, scheduler, device, rate_unit, currency_symbol),
            PlannedDecisionSensor(coordinator, scheduler, device, rate_unit, currency_symbol),
            BestWindowsSensor(coordinator, device),
            OfferSensor(coordinator, scheduler, device),
        ]
        if curve_store:
            entities.append(CurrentCycleSensor(coordinator, scheduler, device))
            # Per-cycle power curve confidence — diagnostic, off the main card
            if device.get("power_sensor"):
                for cycle in cycles:
                    entities.append(
                        PowerCurveConfidenceSensor(
                            coordinator, device, cycle, curve_store, multi_cycle
                        )
                    )
            # Live current-cycle metrics (unavailable between cycles → no
            # sawtooth in the recorder graph)
            entities += [
                CurrentCycleEnergySensor(coordinator, scheduler, device),
                CurrentCycleCostSensor(coordinator, scheduler, device, currency_symbol),
                CurrentCycleDurationSensor(coordinator, scheduler, device),
                # Last completed cycle — name is the headline
                LastCycleSensor(coordinator, scheduler, device, curve_store),
                LastCycleEnergySensor(coordinator, scheduler, device, curve_store),
                LastCycleCostSensor(coordinator, scheduler, device, curve_store, currency_symbol),
                LastCycleDurationSensor(coordinator, scheduler, device, curve_store),
                # Accumulated totals — for Energy Dashboard
                TotalEnergySensor(coordinator, scheduler, device, curve_store),
                TotalCostSensor(coordinator, scheduler, device, curve_store, currency_symbol),
                TotalDurationSensor(coordinator, scheduler, device, curve_store),
            ]

    async_add_entities(entities)


# ── Base classes ──────────────────────────────────────────────────────────────

class _CoordBase(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: SmartStartCoordinator, device: Dict) -> None:
        super().__init__(coordinator)
        self._device = device
        self._slug = device["device_slug"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._slug)},
            name=device["name"],
            manufacturer="SmartStart",
        )

    def _device_data(self) -> Optional[Dict]:
        return (self.coordinator.data or {}).get(self._slug)

    def _cycle_data(self, cycle_id: Optional[str] = None) -> Optional[Dict]:
        dd = self._device_data()
        if dd is None:
            return None
        cid = cycle_id or self.coordinator.get_selected_cycle_id(self._slug, self._device)
        if not cid:
            return None
        return dd.get("cycles", {}).get(cid)


class _ScheduleBase(_CoordBase):
    def __init__(
        self,
        coordinator: SmartStartCoordinator,
        scheduler: SmartStartScheduler,
        device: Dict,
    ) -> None:
        super().__init__(coordinator, device)
        self._scheduler = scheduler

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._scheduler.add_listener(self._on_schedule_change)

    @callback
    def _on_schedule_change(self, slug: str) -> None:
        if slug == self._slug:
            self.async_write_ha_state()

    def _sched(self) -> Dict:
        return self._scheduler.get_schedule(self._slug)


# ── Concrete sensors ──────────────────────────────────────────────────────────

class StatusSensor(_ScheduleBase):
    """Main status sensor — also carries device config in attributes for the card."""

    def __init__(self, coordinator, scheduler, device, currency_symbol, rate_unit):
        super().__init__(coordinator, scheduler, device)
        self._currency_symbol = currency_symbol
        self._rate_unit = rate_unit

    @property
    def unique_id(self): return f"smartstart_{self._slug}_status"
    @property
    def name(self): return f"{self._device['name']} Status"
    @property
    def icon(self): return "mdi:clock-outline"
    @property
    def native_value(self): return self._sched().get("status", STATE_IDLE)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Lean attributes — schedule values live on the Planned X sensors now.
        Only carry device config that dashboard cards need."""
        return {
            "device_slug": self._slug,
            "icon": self._device.get("icon", "mdi:lightning-bolt"),
            "currency_symbol": self._currency_symbol,
            "rate_unit": self._rate_unit,
            "delay_handling": self._device.get("delay_handling", "start_delay"),
            "cycles": self._device.get("cycles", []),
        }


class CurrentRateSensor(_CoordBase):
    def __init__(self, coordinator, device, currency_symbol="£"):
        super().__init__(coordinator, device)
        # Value is in major units (£/kWh); rate_unit ("p") is display-only.
        self._currency_symbol = currency_symbol

    @property
    def unique_id(self): return f"smartstart_{self._slug}_current_rate"
    @property
    def name(self): return f"{self._device['name']} Current Rate"
    @property
    def icon(self): return "mdi:lightning-bolt"
    @property
    def native_unit_of_measurement(self): return f"{self._currency_symbol}/kWh"
    @property
    def native_value(self):
        dd = self._device_data()
        if dd and dd.get("current_rate") is not None:
            return round(dd["current_rate"], 4)
        return None


class _PlannedBase(_ScheduleBase):
    """Base for Planned X sensors with freeze semantics.

    Two modes:
      * **Live**  — status == idle. Reads from coordinator.data's chosen_window
                    so the sensor reflects the current best recommendation under
                    the current Scheduling Mode + rates.
      * **Frozen** — status in {scheduled, running, paused}. Reads from the
                    scheduler's persisted schedule dict, so displayed values
                    never drift from what was actually committed.

    Subclasses implement `_live_value()` (returns native_value from
    chosen_window) and `_frozen_value()` (returns native_value from sched).
    """

    def __init__(self, coordinator, scheduler, device, rate_unit="p", currency_symbol="£"):
        super().__init__(coordinator, scheduler, device)
        self._rate_unit = rate_unit
        self._currency_symbol = currency_symbol

    def _is_frozen(self) -> bool:
        return self._sched().get("status", STATE_IDLE) != STATE_IDLE

    def _chosen_window(self):
        cd = self._cycle_data()
        return cd.get("chosen_window") if cd else None

    @property
    def native_value(self):
        return self._frozen_value() if self._is_frozen() else self._live_value()

    def _live_value(self):
        raise NotImplementedError

    def _frozen_value(self):
        raise NotImplementedError


class _WindowStartBase(_CoordBase):
    """[Deprecated — kept only for the attribute logic on PlannedStartSensor]
    Timestamp sensor for a scored window, with rate/savings details as attributes."""

    _window_key: str = ""  # "best_window" or "chosen_window" — set by subclass

    def __init__(self, coordinator, device, rate_unit, currency_symbol="£"):
        super().__init__(coordinator, device)
        self._rate_unit = rate_unit
        self._currency_symbol = currency_symbol

    def _fmt_rate(self, rate_per_kwh: float) -> str:
        """Render a rate in the user's configured display unit (e.g. p/kWh)."""
        if self._rate_unit == "p":
            return f"{rate_per_kwh * 100:.1f}p/kWh"
        return f"{rate_per_kwh:.4f} {self._rate_unit}/kWh"

    def _fmt_money(self, amount: float) -> str:
        """Render a money amount as e.g. £0.30. Negative shown as -£0.30."""
        sign = "-" if amount < 0 else ""
        return f"{sign}{self._currency_symbol}{abs(amount):.2f}"

    @staticmethod
    def _fmt_delay(mins: int) -> str:
        h, m = divmod(int(mins), 60)
        if h == 0:
            return f"{m}m"
        if m == 0:
            return f"{h}h"
        return f"{h}h {m}m"

    @property
    def device_class(self):
        return SensorDeviceClass.TIMESTAMP

    def _window(self):
        cd = self._cycle_data()
        return cd.get(self._window_key) if cd else None

    @property
    def native_value(self) -> Optional[datetime]:
        win = self._window()
        if not win:
            return None
        # If we're already inside the window (delay 0), show "now" rather than
        # the slot boundary — otherwise the timestamp drifts into the past.
        return max(win.start, datetime.now(timezone.utc))

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        cd = self._cycle_data()
        win = self._window()
        if not cd or not win:
            return {}

        cycle = cd.get("cycle", {}) or {}
        # Effective cycle duration — learned average if confident, else configured
        duration_min = cd.get("duration_minutes") or cycle.get("duration_minutes")
        cycle_kwh    = cycle.get("total_kwh")
        estimated_cost = (
            round(win.avg_rate_cost_per_kwh * cycle_kwh, 4)
            if cycle_kwh is not None else None
        )

        attrs: Dict[str, Any] = {
            "estimated_finish":       win.end,
            "estimated_cost":         estimated_cost,
            "expected_cycle_length":  duration_min,
            "delay_minutes":          win.delay_minutes,
            "avg_rate_cost_per_kwh":  round(win.avg_rate_cost_per_kwh, 4),
            "rate_unit":              f"{self._rate_unit}/kWh",
        }

        rates = list(self.coordinator.rates)
        slot_rates = [
            r.rate_cost_per_kwh for r in rates if r.start >= win.start and r.end <= win.end
        ]
        if slot_rates:
            attrs["min_rate"] = round(min(slot_rates), 4)
            attrs["max_rate"] = round(max(slot_rates), 4)

        now_win  = cd.get("now_window")
        best_win = cd.get("best_window")

        if now_win and now_win is not win:
            now_rate = now_win.avg_rate_cost_per_kwh
            saving_per_kwh = now_rate - win.avg_rate_cost_per_kwh
            # Always show an absolute saving — percentages explode when current
            # rate is at/below zero, so we keep savings_pct conditional but
            # provide savings_per_kwh as the reliable headline.
            attrs["savings_per_kwh"] = round(saving_per_kwh, 4)
            if cycle_kwh is not None:
                attrs["estimated_savings"] = round(saving_per_kwh * cycle_kwh, 4)
            if now_rate > 0:
                attrs["savings_pct"] = round(
                    (1 - win.avg_rate_cost_per_kwh / now_rate) * 100, 1
                )
            attrs["now_avg_rate_cost_per_kwh"] = round(now_rate, 4)

        # Reason: short headline that leads with the £ impact (the figure people
        # actually parse), then the avg rate as parenthetical context. Per-kWh
        # savings are technically accurate but cognitively expensive — "save
        # £0.30" is what registers, not "save 10p/kWh".
        threshold     = self.coordinator.prefer_sooner_diff
        win_rate      = win.avg_rate_cost_per_kwh
        deadline      = cd.get("deadline")
        deadline_used = cd.get("deadline_used")

        def _money(per_kwh: float) -> Optional[str]:
            """£ saving for this cycle, or None when cycle_kwh is unknown."""
            if cycle_kwh is None:
                return None
            return self._fmt_money(per_kwh * cycle_kwh)

        def _rate_paren(rate: float) -> str:
            """' (runs at 4.2p/kWh)' — the rate as background context."""
            return f" (runs at {self._fmt_rate(rate)})"

        # Hold-for-rates branch takes top priority: the recommendation is a
        # placeholder awaiting tomorrow's rates, and the honest explanation
        # includes what running now would have cost.
        hold_until = cd.get("hold_until")
        if cd.get("hold_active") and hold_until:
            attrs["hold_until"] = hold_until
            head = (
                f"Waiting for tomorrow's rates (expected by "
                f"{hold_until.astimezone().strftime('%H:%M')}) — placeholder "
                f"{win.start.astimezone().strftime('%H:%M')}{_rate_paren(win_rate)}"
            )
            if now_win and now_win is not win:
                head += f", forgoing {self._fmt_rate(now_win.avg_rate_cost_per_kwh)} now"
            if cd.get("chosen_is_predicted"):
                head += " — predicted rate, will confirm against actuals"
            attrs["reason"] = head + ". Will re-optimise when rates arrive."
            return attrs

        # Deadline-constrained branch takes priority: the user said "by X" so
        # the choice is "cheapest window that finishes in time" regardless of
        # the prefer_sooner threshold.
        if deadline_used and deadline:
            deadline_str = deadline.astimezone().strftime("%a %H:%M")
            attrs["deadline"] = deadline
            if best_win and win is not best_win:
                # We skipped a cheaper future window because it wouldn't finish in time
                saving = win_rate - best_win.avg_rate_cost_per_kwh
                missed_money = _money(saving)
                missed = missed_money or self._fmt_rate(saving)
                attrs["reason"] = (
                    f"Due by {deadline_str}{_rate_paren(win_rate)}. "
                    f"Waiting until {best_win.start.astimezone().strftime('%H:%M')} would have "
                    f"saved {missed} but misses the deadline."
                )
                attrs["cheaper_alternative_start"]            = best_win.start
                attrs["cheaper_alternative_avg_rate_per_kwh"] = round(best_win.avg_rate_cost_per_kwh, 4)
                attrs["cheaper_alternative_saving_per_kwh"]   = round(saving, 4)
            else:
                # Optional "saves £X vs running now" tail when we have a now_win comparison
                tail = ""
                if now_win:
                    s = now_win.avg_rate_cost_per_kwh - win_rate
                    s_money = _money(s)
                    if s_money and s > 0:
                        tail = f" — saves {s_money} vs running now"
                attrs["reason"] = (
                    f"Cheapest window finishing by {deadline_str}"
                    f"{_rate_paren(win_rate)}{tail}"
                )
            return attrs

        # ── Mode-aware reason text ───────────────────────────────────────────
        # Mode is runtime state on the coordinator (set via SchedulingModeSelectEntity).
        # cd's "mode" key reflects what was used at the time _build_data ran — same value
        # in practice, but coordinator is authoritative for the current setting.
        mode = self.coordinator.get_scheduling_mode(self._slug)
        balanced_target = cd.get("balanced_target_rate")
        mode_fallback   = cd.get("mode_fallback_used", False)
        attrs["mode"] = mode

        # Mode = Now: explicit immediate-start policy
        if mode == "now":
            attrs["reason"] = f"Mode = Now — running immediately{_rate_paren(win_rate)}"
            return attrs

        # Threshold caused us to start now over a cheaper future window
        if best_win and win is not best_win and now_win and win is now_win:
            saving = now_win.avg_rate_cost_per_kwh - best_win.avg_rate_cost_per_kwh
            saved_money = _money(saving)
            thresh_money = _money(threshold)
            saved = saved_money or self._fmt_rate(saving)
            thresh = thresh_money or self._fmt_rate(threshold)
            attrs["reason"] = (
                f"Starting now — cheapest window in {self._fmt_delay(best_win.delay_minutes)} "
                f"would only save {saved} (below {thresh} minimum)"
            )
            attrs["cheaper_alternative_start"]            = best_win.start
            attrs["cheaper_alternative_avg_rate_per_kwh"] = round(best_win.avg_rate_cost_per_kwh, 4)
            attrs["cheaper_alternative_saving_per_kwh"]   = round(saving, 4)
            return attrs

        # Balanced — found a window meeting the target
        if mode == "balanced" and not mode_fallback and now_win and win is not now_win:
            saving = now_win.avg_rate_cost_per_kwh - win_rate
            saved_money = _money(saving)
            head = (
                f"Save {saved_money} by waiting {self._fmt_delay(win.delay_minutes)}"
                if saved_money and saving > 0
                else f"Balanced — runs in {self._fmt_delay(win.delay_minutes)}"
            )
            attrs["reason"] = (
                f"{head}{_rate_paren(win_rate)} — meets your "
                f"{self._fmt_rate(balanced_target)} target"
            )
            return attrs

        # Balanced fell through to cheapest (no window met target)
        if mode == "balanced" and mode_fallback and now_win and win is not now_win:
            saving = now_win.avg_rate_cost_per_kwh - win_rate
            saved_money = _money(saving)
            tail = (
                f" — saves {saved_money}" if saved_money and saving > 0 else ""
            )
            attrs["reason"] = (
                f"Balanced — no window met your {self._fmt_rate(balanced_target)} target; "
                f"picking the cheapest in the next {self._fmt_delay(win.delay_minutes)}"
                f"{_rate_paren(win_rate)}{tail}"
            )
            return attrs

        # Already in best == now (Max Savings)
        if best_win and win is best_win and now_win and win is now_win:
            attrs["reason"] = f"Already at the cheapest available rate{_rate_paren(win_rate)}"
            return attrs

        # Delaying into a cheaper window (Max Savings)
        if best_win and win is best_win and now_win:
            saving = now_win.avg_rate_cost_per_kwh - win_rate
            saved_money = _money(saving)
            head = (
                f"Save {saved_money} by waiting {self._fmt_delay(win.delay_minutes)}"
                if saved_money
                else f"Save {self._fmt_rate(saving)} by waiting {self._fmt_delay(win.delay_minutes)}"
            )
            parts = [f"{head}{_rate_paren(win_rate)}"]
            if win_rate < 0:
                parts.append("— negative-rate window, you're paid to run this cycle")
            attrs["reason"] = " ".join(parts)
            return attrs

        if best_win and win is best_win:
            attrs["reason"] = (
                f"Cheapest start in the next {self._fmt_delay(win.delay_minutes)}"
                f"{_rate_paren(win_rate)}"
            )
            return attrs

        attrs["reason"] = "Only one window available"
        return attrs


class PlannedStartSensor(_PlannedBase):
    """When the cycle will start.

    Live (status==idle): the recommended start time under current Scheduling
    Mode + rates, re-scored every minute by the coordinator.
    Frozen (status in scheduled/running/paused): the time committed when
    Start was pressed. Stays stable even if mode/rates change — press Cancel
    to unfreeze.
    """

    @property
    def unique_id(self):
        # Keep historical unique_id (formerly "Optimal Savings Start") so
        # recorder history and dashboard cards continue to resolve.
        return f"smartstart_{self._slug}_best_window_start"

    @property
    def name(self):
        return f"{self._device['name']} Planned Start"

    @property
    def icon(self):
        return "mdi:star-outline"

    @property
    def device_class(self):
        return SensorDeviceClass.TIMESTAMP

    def _live_value(self) -> Optional[datetime]:
        win = self._chosen_window()
        if not win:
            return None
        # Inside the window already → show "now" rather than a past timestamp
        return max(win.start, datetime.now(timezone.utc))

    def _frozen_value(self) -> Optional[datetime]:
        raw = self._sched().get("scheduled_start")
        try:
            return datetime.fromisoformat(raw) if raw else None
        except ValueError:
            return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        # Reuse the rich live-attribute logic from the deprecated base. We
        # build a transient _WindowStartBase to compute reason/savings/etc.
        # without duplicating ~150 lines of formatting logic. When frozen,
        # attributes are minimal — the committed values speak for themselves.
        if self._is_frozen():
            sched = self._sched()
            return {
                "mode_used": sched.get("mode"),
                "scheduled_at": sched.get("scheduled_at"),
                "cycle_id": sched.get("cycle_id"),
                "delay_seconds": sched.get("delay_seconds"),
                "frozen": True,
            }
        # Live path — delegate to the legacy attribute builder
        proxy = _WindowStartBase(self.coordinator, self._device, self._rate_unit, self._currency_symbol)
        proxy._window_key = "chosen_window"
        proxy.hass = self.hass
        attrs = proxy.extra_state_attributes
        attrs["mode"] = self.coordinator.get_scheduling_mode(self._slug)
        attrs["frozen"] = False
        return attrs


class PlannedFinishSensor(_PlannedBase):
    """When the cycle will finish. Live recommendation vs. committed finish,
    same freeze semantics as Planned Start."""

    @property
    def unique_id(self):
        return f"smartstart_{self._slug}_planned_finish"

    @property
    def name(self):
        return f"{self._device['name']} Planned Finish"

    @property
    def icon(self):
        return "mdi:flag-checkered"

    @property
    def device_class(self):
        return SensorDeviceClass.TIMESTAMP

    def _live_value(self) -> Optional[datetime]:
        win = self._chosen_window()
        return win.end if win else None

    def _frozen_value(self) -> Optional[datetime]:
        raw = self._sched().get("estimated_finish")
        try:
            return datetime.fromisoformat(raw) if raw else None
        except ValueError:
            return None


class PlannedCostSensor(_PlannedBase):
    """Total expected cost for the planned cycle."""

    def __init__(self, coordinator, scheduler, device, currency_symbol):
        super().__init__(coordinator, scheduler, device, currency_symbol=currency_symbol)

    @property
    def unique_id(self):
        return f"smartstart_{self._slug}_planned_cost"

    @property
    def name(self):
        return f"{self._device['name']} Planned Cost"

    @property
    def icon(self):
        return "mdi:cash"

    @property
    def native_unit_of_measurement(self):
        return self._currency_symbol

    @property
    def device_class(self):
        return SensorDeviceClass.MONETARY

    def _live_value(self) -> Optional[float]:
        cd  = self._cycle_data()
        win = self._chosen_window()
        if not cd or not win:
            return None
        cycle = cd.get("cycle", {}) or {}
        kwh = cycle.get("total_kwh")
        if kwh is None:
            return None
        return round(win.avg_rate_cost_per_kwh * kwh, 4)

    def _frozen_value(self) -> Optional[float]:
        v = self._sched().get("estimated_cost")
        return round(v, 4) if isinstance(v, (int, float)) else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        if self._is_frozen():
            return {"frozen": True}
        cd  = self._cycle_data()
        win = self._chosen_window()
        if not cd or not win:
            return {}
        cycle = cd.get("cycle", {}) or {}
        kwh = cycle.get("total_kwh")
        now_win = cd.get("now_window")
        attrs: Dict[str, Any] = {"frozen": False}
        if now_win and now_win is not win and kwh is not None:
            saving_per_kwh = now_win.avg_rate_cost_per_kwh - win.avg_rate_cost_per_kwh
            attrs["estimated_savings"] = round(saving_per_kwh * kwh, 4)
            attrs["now_cost"] = round(now_win.avg_rate_cost_per_kwh * kwh, 4)
            if now_win.avg_rate_cost_per_kwh > 0:
                attrs["savings_pct"] = round(
                    (1 - win.avg_rate_cost_per_kwh / now_win.avg_rate_cost_per_kwh) * 100, 1
                )
        return attrs


class BestWindowsSensor(_CoordBase):
    """Cheapest distinct windows for the selected cycle over real + forecast
    rates (planning view — nothing is booked). State = the best start."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def unique_id(self):
        return f"smartstart_{self._slug}_best_windows"

    @property
    def name(self):
        return f"{self._device['name']} Best Windows"

    @property
    def icon(self):
        return "mdi:calendar-star"

    def _windows(self) -> List[Dict]:
        cd = self._cycle_data()
        return (cd or {}).get("best_windows") or []

    @property
    def native_value(self) -> Optional[datetime]:
        windows = self._windows()
        return datetime.fromisoformat(windows[0]["start"]) if windows else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        cd = self._cycle_data() or {}
        return {
            "cycle": (cd.get("cycle") or {}).get("name"),
            "windows": self._windows(),
        }


class OfferSensor(_ScheduleBase):
    """A cheaper window the scheduled run can't reach on its own (forecast
    slots, or past its envelope) — accept via the Accept Offer button or
    smartstart.accept_offer. State = the offered start; unknown when there's
    no offer. `message` is ready-made notification text."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def unique_id(self):
        return f"smartstart_{self._slug}_offer"

    @property
    def name(self):
        return f"{self._device['name']} Offer"

    @property
    def icon(self):
        return "mdi:piggy-bank-outline"

    @property
    def native_value(self) -> Optional[datetime]:
        offer = self.coordinator.get_offer(self._slug)
        return datetime.fromisoformat(offer["start"]) if offer else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        return dict(self.coordinator.get_offer(self._slug) or {})


class PlannedRateSensor(_PlannedBase):
    """Average rate the planned cycle will pay across its run."""

    def __init__(self, coordinator, scheduler, device, rate_unit, currency_symbol="£"):
        super().__init__(
            coordinator, scheduler, device,
            rate_unit=rate_unit, currency_symbol=currency_symbol,
        )

    @property
    def unique_id(self):
        return f"smartstart_{self._slug}_planned_rate"

    @property
    def name(self):
        return f"{self._device['name']} Planned Rate"

    @property
    def icon(self):
        return "mdi:lightning-bolt-outline"

    @property
    def native_unit_of_measurement(self):
        # Value is £/kWh (major units); rate_unit is only for text rendering.
        return f"{self._currency_symbol}/kWh"

    def _live_value(self) -> Optional[float]:
        win = self._chosen_window()
        return round(win.avg_rate_cost_per_kwh, 4) if win else None

    def _frozen_value(self) -> Optional[float]:
        v = self._sched().get("rate_cost_per_kwh")
        return round(v, 4) if isinstance(v, (int, float)) else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        if self._is_frozen():
            return {"frozen": True}
        cd  = self._cycle_data()
        win = self._chosen_window()
        if not cd or not win:
            return {}
        rates = list(self.coordinator.rates)
        slot_rates = [
            r.rate_cost_per_kwh for r in rates if r.start >= win.start and r.end <= win.end
        ]
        attrs: Dict[str, Any] = {"frozen": False}
        if slot_rates:
            attrs["min_rate"] = round(min(slot_rates), 4)
            attrs["max_rate"] = round(max(slot_rates), 4)
        now_win = cd.get("now_window")
        if now_win is not None:
            attrs["now_rate"] = round(now_win.avg_rate_cost_per_kwh, 4)
        return attrs


class PlannedDecisionSensor(_PlannedBase):
    """One-line explanation of why SmartStart picked this window.

    Diagnostic so it shows on the device page without cluttering the main
    card. State is the same string previously exposed as the `reason`
    attribute on Planned Start — kept consistent so dashboards can be
    pointed at this sensor directly. Frozen-mode shows a "scheduled under X
    mode — press Cancel to re-evaluate" line; live-mode delegates to the
    rich attribute builder for the full impact / threshold / fallback text.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        return f"smartstart_{self._slug}_planned_decision"

    @property
    def name(self):
        return f"{self._device['name']} Decision"

    @property
    def icon(self):
        return "mdi:lightbulb-on-outline"

    def _live_value(self) -> Optional[str]:
        # Reuse the reason-builder from the legacy attribute helper.
        if self._chosen_window() is None:
            return None
        proxy = _WindowStartBase(self.coordinator, self._device, self._rate_unit, self._currency_symbol)
        proxy._window_key = "chosen_window"
        attrs = proxy.extra_state_attributes
        return attrs.get("reason")

    def _frozen_value(self) -> Optional[str]:
        sched = self._sched()
        mode  = sched.get("mode") or self.coordinator.get_scheduling_mode(self._slug)
        return (
            f"Scheduled under {mode} mode — press Stop to re-evaluate"
        )


class CurrentCycleSensor(_ScheduleBase):
    """Name of the cycle currently running. Unavailable between cycles —
    so the recorder doesn't store a string-valued sawtooth."""

    @property
    def unique_id(self) -> str:
        return f"smartstart_{self._slug}_current_cycle"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Current Cycle"

    @property
    def icon(self) -> str:
        return "mdi:rotate-right"

    @property
    def available(self) -> bool:
        return self._sched().get("status") in (STATE_RUNNING, STATE_PAUSED)

    @property
    def native_value(self) -> Optional[str]:
        sched = self._sched()
        if sched.get("status") not in (STATE_RUNNING, STATE_PAUSED):
            return None
        cid = sched.get("cycle_id")
        if cid == EXTERNAL_CYCLE_KEY:
            return EXTERNAL_CYCLE_NAME
        for c in self._device.get("cycles", []):
            if c.get("cycle_key") == cid:
                return c.get("name", cid)
        return cid


class PowerCurveConfidenceSensor(_CoordBase):
    """Shows how confident SmartStart is in its learned power curve for a cycle.
    Diagnostic — moved off the main card to reduce clutter, still accessible
    via the device's diagnostic panel."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SmartStartCoordinator,
        device: Dict,
        cycle: Dict,
        curve_store: CurveStore,
        multi_cycle: bool,
    ) -> None:
        super().__init__(coordinator, device)
        self._cycle      = cycle
        # Internal storage key — stable, matches curve_store and unique IDs
        self._cycle_id   = cycle["cycle_key"]
        self._curve_store = curve_store
        cycle_label = f" {cycle['name']}" if multi_cycle else ""
        self._sensor_name = f"{device['name']}{cycle_label} Power Curve Confidence"

    @property
    def unique_id(self) -> str:
        return f"smartstart_{self._slug}_{self._cycle_id}_curve_confidence"

    @property
    def name(self) -> str:
        return self._sensor_name

    @property
    def icon(self) -> str:
        info = self._curve_store.get_confidence_info(self._slug, self._cycle_id)
        if info["warning"]:
            return "mdi:chart-bell-curve-cumulative"
        return "mdi:chart-bell-curve"

    @property
    def native_unit_of_measurement(self) -> str:
        return "%"

    @property
    def native_value(self) -> Optional[float]:
        info = self._curve_store.get_confidence_info(self._slug, self._cycle_id)
        return round(info["confidence"] * 100, 1)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        info = self._curve_store.get_confidence_info(self._slug, self._cycle_id)
        cd   = (
            (self.coordinator.data or {})
            .get(self._slug, {})
            .get("cycles", {})
            .get(self._cycle_id, {})
        )
        return {
            "n_curves":             info["n_curves"],
            "status":               info["status"],
            "last_captured_at":     info["last_captured_at"],
            "warning":              info["warning"],
            "profile_type":         info["profile_type"],
            "avg_duration_minutes": info["avg_duration_minutes"],
            "duration_std_minutes": info["duration_std_minutes"],
            "profile_active":       cd.get("profile_active", False),
            "duration_source":      cd.get("duration_source", "configured"),
            "duration_minutes":     cd.get("duration_minutes", self._cycle.get("duration_minutes")),
            # Normalised energy fractions per bucket — for ApexCharts / dashboard rendering
            "profile":              self._curve_store.get_profile(self._slug, self._cycle_id) or [],
        }


# ── Live current-cycle sensors (update every minute while running) ────────────

class _CurrentCycleBase(_ScheduleBase):
    """Reads from scheduler.get_current_stats(); marks the entity unavailable
    when no cycle is in progress so the recorder graph shows gaps between
    cycles, not a sawtooth of resetting-to-zero values."""

    def _live(self) -> dict:
        return self._scheduler.get_current_stats(self._slug)

    @property
    def available(self) -> bool:
        return self._sched().get("status") in (STATE_RUNNING, STATE_PAUSED)


class CurrentCycleEnergySensor(_CurrentCycleBase):
    @property
    def unique_id(self) -> str:
        return f"smartstart_{self._slug}_current_cycle_energy"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Current Cycle Energy"

    @property
    def icon(self) -> str:
        return "mdi:lightning-bolt"

    @property
    def native_unit_of_measurement(self) -> str:
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    def device_class(self):
        return SensorDeviceClass.ENERGY

    @property
    def state_class(self):
        return SensorStateClass.TOTAL

    @property
    def native_value(self) -> Optional[float]:
        return self._live().get("current_kwh")


class CurrentCycleCostSensor(_CurrentCycleBase):
    def __init__(self, coordinator, scheduler, device, currency_symbol):
        super().__init__(coordinator, scheduler, device)
        self._currency = currency_symbol

    @property
    def unique_id(self) -> str:
        return f"smartstart_{self._slug}_current_cycle_cost"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Current Cycle Cost"

    @property
    def icon(self) -> str:
        return "mdi:cash-fast"

    @property
    def native_unit_of_measurement(self) -> str:
        return self._currency

    @property
    def device_class(self):
        return SensorDeviceClass.MONETARY

    @property
    def native_value(self) -> Optional[float]:
        return self._live().get("current_cost")


class CurrentCycleDurationSensor(_CurrentCycleBase):
    @property
    def unique_id(self) -> str:
        return f"smartstart_{self._slug}_current_cycle_duration"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Current Cycle Duration"

    @property
    def icon(self) -> str:
        return "mdi:timer"

    @property
    def native_unit_of_measurement(self) -> str:
        return "min"

    @property
    def device_class(self):
        return SensorDeviceClass.DURATION

    @property
    def state_class(self):
        return SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        return self._live().get("elapsed_minutes")


# ── Last-run sensors (device level, one set per device) ──────────────────────

class _LastRunBase(_ScheduleBase):
    """Reads device-level last-run data from CurveStore; updates on cycle completion."""

    def __init__(
        self,
        coordinator: SmartStartCoordinator,
        scheduler: SmartStartScheduler,
        device: Dict,
        curve_store: CurveStore,
    ) -> None:
        super().__init__(coordinator, scheduler, device)
        self._curve_store = curve_store

    def _last_run(self) -> dict:
        return self._curve_store.get_last_run(self._slug)


class LastCycleSensor(_LastRunBase):
    """Name of the most recently completed cycle. Cost/Energy/Duration
    siblings carry the other stats."""

    @property
    def unique_id(self) -> str:
        # Reuse the historical "_last_cycle_type" id so recorder history persists
        return f"smartstart_{self._slug}_last_cycle_type"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Last Cycle"

    @property
    def icon(self) -> str:
        return "mdi:washing-machine"

    @property
    def native_value(self) -> Optional[str]:
        return self._last_run().get("cycle_name")


class LastCycleEnergySensor(_LastRunBase):
    @property
    def unique_id(self) -> str:
        return f"smartstart_{self._slug}_last_cycle_energy"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Last Cycle Energy"

    @property
    def icon(self) -> str:
        return "mdi:lightning-bolt-outline"

    @property
    def native_unit_of_measurement(self) -> str:
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    def device_class(self):
        return SensorDeviceClass.ENERGY

    @property
    def state_class(self):
        # ENERGY device class only accepts TOTAL or TOTAL_INCREASING.
        # TOTAL fits: value changes at each cycle completion, can go up or down.
        return SensorStateClass.TOTAL

    @property
    def native_value(self) -> Optional[float]:
        return self._last_run().get("kwh")


class LastCycleCostSensor(_LastRunBase):
    def __init__(self, coordinator, scheduler, device, curve_store, currency_symbol):
        super().__init__(coordinator, scheduler, device, curve_store)
        self._currency = currency_symbol

    @property
    def unique_id(self) -> str:
        return f"smartstart_{self._slug}_last_cycle_cost"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Last Cycle Cost"

    @property
    def icon(self) -> str:
        return "mdi:cash-clock"

    @property
    def native_unit_of_measurement(self) -> str:
        return self._currency

    @property
    def device_class(self):
        return SensorDeviceClass.MONETARY

    @property
    def native_value(self) -> Optional[float]:
        return self._last_run().get("cost")


class LastCycleDurationSensor(_LastRunBase):
    @property
    def unique_id(self) -> str:
        return f"smartstart_{self._slug}_last_cycle_duration"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Last Cycle Duration"

    @property
    def icon(self) -> str:
        return "mdi:timer-outline"

    @property
    def native_unit_of_measurement(self) -> str:
        return "min"

    @property
    def device_class(self):
        return SensorDeviceClass.DURATION

    @property
    def state_class(self):
        return SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        return self._last_run().get("duration_minutes")


# ── Accumulated totals (device level, grow forever) ───────────────────────────

class TotalEnergySensor(_LastRunBase):
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        return f"smartstart_{self._slug}_total_energy"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Total Energy"

    @property
    def icon(self) -> str:
        return "mdi:sigma"

    @property
    def native_unit_of_measurement(self) -> str:
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    def device_class(self):
        return SensorDeviceClass.ENERGY

    @property
    def state_class(self):
        return SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> Optional[float]:
        return self._curve_store.get_totals(self._slug)["total_kwh"]


class TotalCostSensor(_LastRunBase):
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, scheduler, device, curve_store, currency_symbol):
        super().__init__(coordinator, scheduler, device, curve_store)
        self._currency = currency_symbol

    @property
    def unique_id(self) -> str:
        return f"smartstart_{self._slug}_total_cost"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Total Cost"

    @property
    def icon(self) -> str:
        return "mdi:cash-multiple"

    @property
    def native_unit_of_measurement(self) -> str:
        return self._currency

    @property
    def device_class(self):
        return SensorDeviceClass.MONETARY

    @property
    def state_class(self):
        return SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> Optional[float]:
        return self._curve_store.get_totals(self._slug)["total_cost"]


class TotalDurationSensor(_LastRunBase):
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        return f"smartstart_{self._slug}_total_duration"

    @property
    def name(self) -> str:
        return f"{self._device['name']} Total Run Time"

    @property
    def icon(self) -> str:
        return "mdi:timer-sand"

    @property
    def native_unit_of_measurement(self) -> str:
        return "min"

    @property
    def device_class(self):
        return SensorDeviceClass.DURATION

    @property
    def state_class(self):
        return SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> Optional[float]:
        return self._curve_store.get_totals(self._slug)["total_minutes"]
