"""Scheduling state, managed timers, and running-entity watchers."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_MAX_WAIT_HOURS,
    CURVE_MIN_SAMPLES,
    DEFAULT_DELAY_HANDLING,
    DEFAULT_MAX_DELAY_MINUTES,
    DEFAULT_RUNNING_STATES,
    DELAY_HANDLING_END_TIME,
    DELAY_HANDLING_MANAGED,
    DELAY_HANDLING_START_DELAY,
    EXTERNAL_CYCLE_KEY,
    EXTERNAL_CYCLE_NAME,
    MIN_CYCLE_DURATION_S,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RUNNING,
    STATE_SCHEDULED,
    STORAGE_KEY,
    STORAGE_VERSION,
    UNAVAILABLE_GRACE_SECONDS,
)
from .curve_store import CurveStore
from .power_recorder import PowerRecorder
from .window_scorer import ScoredWindow

_LOGGER = logging.getLogger(__name__)


class SmartStartScheduler:
    """Manages per-device scheduling state, timers, and running-entity watchers."""

    def __init__(
        self,
        hass: HomeAssistant,
        devices: List[Dict],
        curve_store: Optional[CurveStore] = None,
        entry_id: str = "",
    ) -> None:
        self._hass        = hass
        self._devices     = {d["device_slug"]: d for d in devices}
        self._entry_id    = entry_id
        # Per-entry storage. All entries used to share one smartstart_schedules
        # file — each scheduler loaded every entry's schedules and wrote them
        # all back on save, so entries clobbered each other's state
        # (last-writer-wins). Legacy data is migrated in async_load.
        storage_key = f"{STORAGE_KEY}_{entry_id}" if entry_id else STORAGE_KEY
        self._store       = Store(hass, STORAGE_VERSION, storage_key)
        self._curve_store = curve_store
        # Callable[[datetime], Optional[float]] — set by __init__.py once the
        # coordinator exists. Used to compute per-slot live and final cycle cost.
        self._rate_at: Optional[Callable] = None
        # Callable[[str], None] — fired whenever a device leaves SCHEDULED state
        # so the coordinator can reset its "better window already fired" dedup.
        self._on_unscheduled: Optional[Callable[[str], None]] = None
        self._rescore: Optional[Callable] = None
        # RunHistoryStore — set by __init__.py; every recorded run lands here
        # for the sidebar panel (list, curve, tagging, rhythm).
        self._run_history = None
        self._schedules: Dict[str, Dict] = {}
        self._listeners: List[Callable[[str], None]] = []
        # slug → cancel handle for managed-mode timers
        self._timers: Dict[str, Callable] = {}
        # slug → cancel handle for state-change watchers
        self._watchers: Dict[str, Callable] = {}
        # slug → active PowerRecorder
        self._recorders: Dict[str, PowerRecorder] = {}
        # slug → cancel handle for per-minute live-update ticks while device is running
        self._live_timers: Dict[str, Callable] = {}
        # slug → cancel handle for "running entity went unavailable" grace timers.
        # Armed when we'd otherwise transition a RUNNING/PAUSED cycle to IDLE
        # purely because the sensor went unavailable. If the sensor recovers
        # before the timer fires, we cancel and stay in the same state.
        self._idle_grace_timers: Dict[str, Callable] = {}

    def set_rate_lookup(self, rate_at: Callable) -> None:
        """Wire the coordinator's rate_at(datetime) → £/kWh into the scheduler."""
        self._rate_at = rate_at

    def set_on_unscheduled(self, cb: Callable[[str], None]) -> None:
        """Register a callback fired when a device leaves SCHEDULED state."""
        self._on_unscheduled = cb

    def set_run_history(self, run_history) -> None:
        self._run_history = run_history

    def set_rescore(self, cb: Callable) -> None:
        """Register a callback (coordinator.rescore_now) to push state changes to HA entities."""
        self._rescore = cb

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def async_load(self) -> None:
        """Load persisted state and re-register watchers / managed timers."""
        raw = await self._store.async_load()
        migrated = False
        if raw is None and self._entry_id:
            # One-time migration from the legacy shared store: take only the
            # schedules for devices this entry owns.
            legacy = await Store(self._hass, STORAGE_VERSION, STORAGE_KEY).async_load() or {}
            raw = {
                "schedules": {
                    slug: sched
                    for slug, sched in legacy.get("schedules", {}).items()
                    if slug in self._devices
                }
            }
            migrated = bool(raw["schedules"])
            if migrated:
                _LOGGER.info(
                    "SmartStart: migrated %d schedule(s) from legacy shared store "
                    "to per-entry store (%s)",
                    len(raw["schedules"]), self._entry_id,
                )
        # Filter to devices this entry owns — drops other entries' devices
        # (legacy shared data) and devices since removed from the config.
        self._schedules = {
            slug: sched
            for slug, sched in (raw or {}).get("schedules", {}).items()
            if slug in self._devices
        }
        if migrated:
            await self._save()

        for slug, device in self._devices.items():
            self._register_running_watcher(slug, device)
            # Reconcile current state immediately — catches devices that are
            # already running/paused at the moment we (re)load (after a config
            # update or HA restart). State-change watchers only fire on
            # transitions, so without this we'd miss the existing state.
            self._hass.async_create_task(self._reconcile_status(slug))

        # Re-register managed timers that survived an HA restart
        for slug, sched in self._schedules.items():
            if sched.get("status") != STATE_SCHEDULED:
                continue
            if sched.get("recommand_at"):
                try:
                    at = datetime.fromisoformat(sched["recommand_at"])
                except ValueError:
                    at = None
                if at is not None:
                    # Overdue → re-send now (the appliance still holds the
                    # interim delay, which is the safe fallback meanwhile).
                    self._register_recommand_timer(
                        slug, max(at, datetime.now(timezone.utc) + timedelta(seconds=5))
                    )
            if self._devices.get(slug, {}).get("delay_handling") != DELAY_HANDLING_MANAGED:
                continue
            scheduled_start_raw = sched.get("scheduled_start")
            if not scheduled_start_raw:
                continue
            try:
                scheduled_start = datetime.fromisoformat(scheduled_start_raw)
            except ValueError:
                continue

            now = datetime.now(timezone.utc)
            if scheduled_start <= now:
                # Missed while HA was down — fire immediately
                _LOGGER.warning(
                    "SmartStart: managed schedule for %s was missed during restart, firing now",
                    slug,
                )
                self._hass.async_create_task(self._fire_managed_start(slug))
            else:
                self._register_managed_timer(slug, scheduled_start)

        # Resume any cycles that were running or paused when HA went down
        for slug, sched in self._schedules.items():
            status = sched.get("status")
            if status not in (STATE_RUNNING, STATE_PAUSED):
                continue
            device = self._devices.get(slug, {})
            power_entity = device.get("power_sensor", "")
            if power_entity and sched.get("cycle_id") and self._curve_store:
                recorder = PowerRecorder(self._hass, power_entity)
                recorder.start()
                # Pre-reboot samples are lost; running_since survives so duration
                # spans the full cycle, kWh covers post-reboot only.
                if status == STATE_PAUSED:
                    recorder.pause()
                self._recorders[slug] = recorder
            if status == STATE_RUNNING:
                self._start_live_timer(slug)
            _LOGGER.info(
                "SmartStart: %s resumed in %s after restart (running_since=%s)",
                slug, status, sched.get("running_since"),
            )

    async def async_teardown(self) -> None:
        for cancel in list(self._timers.values()):
            cancel()
        for cancel in list(self._watchers.values()):
            cancel()
        for cancel in list(self._live_timers.values()):
            cancel()
        for cancel in list(self._idle_grace_timers.values()):
            cancel()
        for recorder in list(self._recorders.values()):
            recorder.stop()
        self._timers.clear()
        self._watchers.clear()
        self._live_timers.clear()
        self._idle_grace_timers.clear()
        self._recorders.clear()

    # ── Scheduling ───────────────────────────────────────────────────────────

    async def async_schedule(
        self,
        device_slug: str,
        cycle_id: str,
        window: ScoredWindow,
        cycle_kwh: float,
        now_rate: Optional[float],
        start_service: str,
        service_payload: Dict,
        reschedule_deadline: Optional[datetime] = None,
        hold_until: Optional[datetime] = None,
        rate_predicted: bool = False,
        laundry_run: bool = False,
        laundry: Optional[Dict] = None,
    ) -> None:
        # Recompute delay from window.start at the moment the action is triggered.
        # The window was scored by the coordinator which may have run up to 30 min ago;
        # using its stale delay_minutes would pass delay_seconds=0 for windows whose
        # start has already passed (or is imminent).
        now_ts       = datetime.now(timezone.utc)
        device       = self._devices.get(device_slug, {})
        # A picked window (start_at) can lie past the appliance's reach — send
        # the reach now and the real delay later, as for an accepted offer.
        delay_seconds, recommand_at = self._reachable_delay(
            device, device.get("delay_handling", DEFAULT_DELAY_HANDLING), window, now_ts
        )

        # Refresh the payload with the corrected delay
        service_payload = {**service_payload, "delay_seconds": delay_seconds}
        if recommand_at is not None:
            shift = timedelta(seconds=int((window.start - now_ts).total_seconds()) - delay_seconds)
            service_payload["start_time"] = (window.start - shift).isoformat()
            service_payload["estimated_finish"] = (window.end - shift).isoformat()

        savings_pct = (
            round((1 - window.avg_rate_cost_per_kwh / now_rate) * 100)
            if now_rate and now_rate > 0
            else 0
        )
        device         = self._devices.get(device_slug, {})
        delay_handling = device.get("delay_handling", DEFAULT_DELAY_HANDLING)

        # Buttons can't receive delay_seconds — auto-upgrade to managed for this run
        start_domain = start_service.split(".", 1)[0] if "." in start_service else ""
        if start_domain == "button" and delay_seconds > 0 and delay_handling != DELAY_HANDLING_MANAGED:
            _LOGGER.warning(
                "SmartStart: %s — start_service is a button but delay_handling is %r "
                "and delay is %ds; managing the timer for this run",
                device_slug, delay_handling, delay_seconds,
            )
            delay_handling = DELAY_HANDLING_MANAGED
            recommand_at = None  # our own timer has unlimited reach

        # Reschedule envelope — committed once, now, so auto-reschedule can
        # never creep later than what this scheduling decision allowed. A
        # finish-by deadline (explicit or Cycle Due By) is the cap directly;
        # otherwise the device's max_delay measured from this moment. For
        # end_time devices the cap bounds the cycle END (same semantics as
        # max_delay itself); managed devices with unbounded max_delay get no
        # cap (full rate horizon).
        max_wait_h = int(device.get("max_wait_hours", DEFAULT_MAX_WAIT_HOURS) or 0)
        if reschedule_deadline is not None:
            resched_cap: Optional[datetime] = reschedule_deadline
            resched_cap_kind: Optional[str] = "deadline"
        elif max_wait_h > 0:
            # Wait limit: finish within N hours of this press. Independent of
            # the appliance's delay reach — targets past it are re-sent.
            resched_cap = max(now_ts + timedelta(hours=max_wait_h), window.end)
            resched_cap_kind = "deadline"
        else:
            max_delay = int(device.get("max_delay_minutes") or 0)
            if delay_handling == DELAY_HANDLING_MANAGED and max_delay <= 0:
                resched_cap, resched_cap_kind = None, None
            else:
                if max_delay <= 0:
                    max_delay = DEFAULT_MAX_DELAY_MINUTES
                resched_cap      = now_ts + timedelta(minutes=max_delay)
                resched_cap_kind = "window"

        _LOGGER.info(
            "SmartStart: scheduling %s/%s — window %s→%s, delay=%ds (%.1fmin), "
            "rate=%.5f, savings=%d%%, handling=%s, service=%s",
            device_slug, cycle_id,
            window.start.strftime("%H:%M"),
            window.end.strftime("%H:%M"),
            delay_seconds, delay_seconds / 60,
            window.avg_rate_cost_per_kwh,
            savings_pct,
            delay_handling,
            start_service,
        )

        self._schedules[device_slug] = {
            "status":           STATE_SCHEDULED,
            "scheduled_at":     now_ts.isoformat(),
            # Use now for immediate starts so the sensor doesn't show a past time
            "scheduled_start":  (now_ts if delay_seconds == 0 else window.start).isoformat(),
            "estimated_finish": window.end.isoformat(),
            "cycle_id":         cycle_id,
            "delay_seconds":    delay_seconds,
            "delay_handling":   delay_handling,
            "rate_cost_per_kwh": window.avg_rate_cost_per_kwh,
            # Rate had it run immediately — the run-history baseline for
            # "what did waiting save".
            "now_rate_cost_per_kwh": now_rate,
            "savings_pct":      savings_pct,
            "estimated_cost":   round(window.avg_rate_cost_per_kwh * cycle_kwh, 4),
            "start_service":    start_service,
            "service_payload":  service_payload,
            "reschedule_cap":      resched_cap.isoformat() if resched_cap else None,
            "reschedule_cap_kind": resched_cap_kind,
            "reschedule_count":    0,
            # Set when this commit is a hold-for-rates placeholder: the
            # auto-reschedule search must not move the start before this
            # time while tomorrow's rates are still pending.
            "hold_until":          hold_until.isoformat() if hold_until else None,
            # True when the committed rate came from user-published forecast
            # slots — the reschedule-on-release compares against real rates.
            "rate_predicted":      bool(rate_predicted),
            # True when a LaundryManager owns this run: the device coordinator
            # must not auto-reschedule or better-window it — the manager
            # re-optimises the composite plan itself.
            "laundry_run":         bool(laundry_run),
            # {run_id, pair, step, flow} — groups a laundry load's wash + dry
            # runs in run history.
            "laundry":             laundry,
            "recommand_at":        recommand_at.isoformat() if recommand_at else None,
        }
        await self._save()
        # New schedule = clean slate for better-window/offer dedup. Before
        # _notify: its rescore may already announce this run's first offer.
        if self._on_unscheduled:
            self._on_unscheduled(device_slug)
        self._notify(device_slug)

        if delay_handling != DELAY_HANDLING_MANAGED:
            self._cancel_timer(device_slug)
            await self._call_service(start_service, service_payload, device_slug)
            if recommand_at is not None:
                self._register_recommand_timer(device_slug, recommand_at)
        else:
            self._register_managed_timer(device_slug, window.start)

    async def async_reschedule(
        self,
        device_slug: str,
        window: ScoredWindow,
        *,
        reschedule_cap: Optional[datetime] = None,
        rate_predicted: bool = False,
        reason: str = "auto",
    ) -> None:
        """Move an existing SCHEDULED cycle to a new (cheaper) window.

        Called by the coordinator's auto-reschedule path when new rates reveal
        a better window inside the envelope committed at schedule time. Keeps
        scheduled_at / reschedule_cap intact so repeated reschedules stay
        anchored to the original decision; re-dispatches the start_service
        with reschedule=true for native-delay appliances, or just moves the
        timer for managed ones. Fires smartstart_cycle_rescheduled.

        reason="offer" is the user accepting an offer: reschedule_cap replaces
        the envelope (a deadline at the offer's finish) — the only path that
        ever widens it — and rate_predicted keeps the forecast flag so the
        reschedule-on-release re-baselines against real rates.
        """
        sched = self._schedules.get(device_slug, {})
        if sched.get("status") != STATE_SCHEDULED:
            _LOGGER.debug(
                "SmartStart: reschedule for %s skipped (status=%s)",
                device_slug, sched.get("status"),
            )
            return

        device = self._devices.get(device_slug, {})
        now_ts = datetime.now(timezone.utc)
        delay_handling = sched.get("delay_handling", DEFAULT_DELAY_HANDLING)
        delay_seconds, recommand_at = self._reachable_delay(
            device, delay_handling, window, now_ts
        )

        old_start = sched.get("scheduled_start")
        old_rate  = sched.get("rate_cost_per_kwh")
        cycle_key = sched.get("cycle_id")
        cycle_cfg = next(
            (c for c in device.get("cycles", []) if c.get("cycle_key") == cycle_key), {}
        )
        cycle_kwh = float(cycle_cfg.get("total_kwh") or 0.0)

        service_payload = {
            **sched.get("service_payload", {}),
            "delay_seconds":     delay_seconds,
            "start_time":        window.start.isoformat(),
            "rate_cost_per_kwh": window.avg_rate_cost_per_kwh,
            "estimated_finish":  window.end.isoformat(),
            "reschedule":        True,
        }
        if recommand_at is not None:
            # Interim command at the appliance's furthest reach — what the
            # appliance will actually do if HA never re-sends.
            shift = timedelta(seconds=int((window.start - now_ts).total_seconds()) - delay_seconds)
            service_payload["start_time"] = (window.start - shift).isoformat()
            service_payload["estimated_finish"] = (window.end - shift).isoformat()

        now_rate = self._rate_at(now_ts) if self._rate_at else None
        savings_pct = (
            round((1 - window.avg_rate_cost_per_kwh / now_rate) * 100)
            if now_rate and now_rate > 0
            else 0
        )
        reschedule_count = int(sched.get("reschedule_count") or 0) + 1

        _LOGGER.info(
            "SmartStart: auto-rescheduling %s/%s — %s → %s (rate %.5f → %.5f, "
            "delay=%ds, handling=%s, reschedule #%d)",
            device_slug, cycle_key,
            old_start, window.start.isoformat(),
            old_rate if old_rate is not None else float("nan"),
            window.avg_rate_cost_per_kwh,
            delay_seconds, delay_handling, reschedule_count,
        )

        self._schedules[device_slug] = {
            **sched,
            "scheduled_start":   (now_ts if delay_seconds == 0 else window.start).isoformat(),
            "estimated_finish":  window.end.isoformat(),
            "delay_seconds":     delay_seconds,
            "rate_cost_per_kwh": window.avg_rate_cost_per_kwh,
            "savings_pct":       savings_pct,
            "estimated_cost":    round(window.avg_rate_cost_per_kwh * cycle_kwh, 4),
            "service_payload":   service_payload,
            "rescheduled_at":    now_ts.isoformat(),
            "reschedule_count":  reschedule_count,
            # Auto-reschedule candidates are always drawn from real rates; an
            # accepted offer may sit on forecast slots.
            "rate_predicted":    bool(rate_predicted),
            "recommand_at":      recommand_at.isoformat() if recommand_at else None,
        }
        if reschedule_cap is not None:
            self._schedules[device_slug].update({
                "reschedule_cap":      reschedule_cap.isoformat(),
                "reschedule_cap_kind": "deadline",
                # The user picked this slot — a pending hold no longer applies.
                "hold_until":          None,
                "offer_accepted_at":   now_ts.isoformat(),
            })
        await self._save()
        self._notify(device_slug)

        if delay_handling != DELAY_HANDLING_MANAGED:
            self._cancel_timer(device_slug)
            await self._call_service(sched.get("start_service", ""), service_payload, device_slug)
            if recommand_at is not None:
                self._register_recommand_timer(device_slug, recommand_at)
        else:
            self._register_managed_timer(device_slug, window.start)

        saving_per_kwh = (
            round(old_rate - window.avg_rate_cost_per_kwh, 4)
            if old_rate is not None else None
        )
        self._fire_event("smartstart_cycle_rescheduled", device_slug, extra={
            "old_start":                 old_start,
            "new_start":                 window.start.isoformat(),
            "new_end":                   window.end.isoformat(),
            "old_avg_rate_cost_per_kwh": round(old_rate, 4) if old_rate is not None else None,
            "new_avg_rate_cost_per_kwh": round(window.avg_rate_cost_per_kwh, 4),
            "saving_per_kwh":            saving_per_kwh,
            "estimated_saving":          (
                round(saving_per_kwh * cycle_kwh, 4)
                if saving_per_kwh is not None else None
            ),
            "reschedule_count":          reschedule_count,
            "reason":                    reason,
        })

    async def async_cancel(self, device_slug: str) -> None:
        _LOGGER.info("SmartStart: async_cancel — clearing schedule for %s", device_slug)
        self._cancel_timer(device_slug)
        self._cancel_live_timer(device_slug)
        recorder = self._recorders.pop(device_slug, None)
        if recorder:
            recorder.stop()  # discard — incomplete cycle
        self._schedules[device_slug] = {"status": STATE_IDLE}
        await self._save()
        self._notify(device_slug)
        _LOGGER.info("SmartStart: async_cancel — %s is now idle", device_slug)
        if self._on_unscheduled:
            self._on_unscheduled(device_slug)

    async def async_set_running(self, device_slug: str) -> None:
        sched = self._schedules.get(device_slug, {})
        prior_status = sched.get("status", STATE_IDLE)

        # If managed timer was still pending, cancel it — device started externally
        self._cancel_timer(device_slug)

        # Resuming from pause — keep existing recorder + running_since
        if prior_status == STATE_PAUSED:
            recorder = self._recorders.get(device_slug)
            if recorder:
                recorder.resume()
            self._schedules[device_slug] = {**sched, "status": STATE_RUNNING}
            self._start_live_timer(device_slug)
            await self._save()
            self._notify(device_slug)
            self._fire_event("smartstart_cycle_resumed", device_slug)
            return

        # Fresh start — was this scheduled by SmartStart, or did the appliance
        # start on its own? External starts get stamped with the Unknown bucket
        # so kWh/cost still track, but learned curves stay clean.
        triggered_by = "smartstart" if sched.get("cycle_id") else "external"
        if triggered_by == "external":
            sched = {
                **sched,
                "cycle_id":          EXTERNAL_CYCLE_KEY,
                "rate_cost_per_kwh": self._rate_at(datetime.now(timezone.utc))
                                     if self._rate_at else None,
            }

        if self._curve_store:
            device      = self._devices.get(device_slug, {})
            power_entity = device.get("power_sensor", "")
            if power_entity:
                recorder = PowerRecorder(self._hass, power_entity)
                recorder.start()
                self._recorders[device_slug] = recorder

        running_since = datetime.now(timezone.utc).isoformat()
        self._schedules[device_slug] = {
            **sched,
            "status": STATE_RUNNING,
            "running_since": running_since,
            "triggered_by":  triggered_by,
        }
        self._start_live_timer(device_slug)
        await self._save()
        self._notify(device_slug)
        if self._on_unscheduled:
            self._on_unscheduled(device_slug)
        self._fire_event("smartstart_cycle_started", device_slug, extra={
            "running_since": running_since,
            "estimated_finish": sched.get("estimated_finish"),
            "rate_cost_per_kwh": sched.get("rate_cost_per_kwh"),
            "triggered_by": triggered_by,
        })

    async def async_set_paused(self, device_slug: str) -> None:
        sched = self._schedules.get(device_slug, {})
        recorder = self._recorders.get(device_slug)
        if recorder:
            recorder.pause()
        self._cancel_live_timer(device_slug)
        self._schedules[device_slug] = {**sched, "status": STATE_PAUSED}
        await self._save()
        self._notify(device_slug)
        self._fire_event("smartstart_cycle_paused", device_slug)

    async def async_set_idle(self, device_slug: str) -> None:
        from .curve_store import _samples_to_kwh, _samples_to_cost

        sched    = self._schedules.get(device_slug, {})
        recorder = self._recorders.pop(device_slug, None)
        cycle_id = sched.get("cycle_id")
        scheduled_rate = float(sched.get("rate_cost_per_kwh") or 0.0)

        samples = recorder.stop() if recorder else []

        # Wall-clock duration from running_since → now, minus any paused time
        running_since_iso = sched.get("running_since")
        duration_s = 0.0
        if running_since_iso:
            try:
                started = datetime.fromisoformat(running_since_iso)
                duration_s = (datetime.now(timezone.utc) - started).total_seconds()
            except ValueError:
                pass
        if recorder:
            duration_s = max(0.0, duration_s - recorder.paused_total_seconds)

        too_short = duration_s < MIN_CYCLE_DURATION_S

        if cycle_id and self._curve_store and not too_short:
            device    = self._devices.get(device_slug, {})
            is_external = cycle_id == EXTERNAL_CYCLE_KEY
            # Scheduler's internal cycle_id is the cycle_key (immutable slug)
            cycle_cfg = next(
                (c for c in device.get("cycles", []) if c["cycle_key"] == cycle_id), {}
            )

            # Curve learning only for SmartStart-scheduled runs — we can't trust
            # which cycle an external start was. Stats still get recorded below.
            ended_at = datetime.now(timezone.utc).isoformat()
            learned = False
            if not is_external and len(samples) >= CURVE_MIN_SAMPLES:
                learned = await self._curve_store.async_add_curve(
                    device_slug=device_slug,
                    cycle_id=cycle_id,
                    samples=samples,
                    captured_at=ended_at,
                )

            actual_kwh        = _samples_to_kwh(samples)
            actual_duration_m = duration_s / 60.0
            # Per-slot cost — falls back to scheduled rate if no wall-ts samples
            actual_cost = (
                _samples_to_cost(samples, self._rate_at) if self._rate_at else None
            )
            if actual_cost is None:
                actual_cost = round(actual_kwh * scheduled_rate, 6)
            await self._curve_store.async_record_last_run(
                device_slug=device_slug,
                cycle_id=cycle_id,
                cycle_name=(
                    EXTERNAL_CYCLE_NAME if is_external
                    else cycle_cfg.get("name", cycle_id)
                ),
                kwh=actual_kwh,
                cost=actual_cost,
                duration_minutes=actual_duration_m,
            )
            if self._run_history is not None:
                await self._record_run(
                    device_slug, sched, cycle_id, samples,
                    ended_at=ended_at,
                    duration_m=actual_duration_m,
                    kwh=actual_kwh,
                    cost=actual_cost,
                    learned=learned,
                    has_power=bool(device.get("power_sensor")),
                )
            cycle_ended_extra = {
                "kwh":              round(actual_kwh, 4),
                "cost":             actual_cost,
                "duration_minutes": round(actual_duration_m, 1),
            }
        elif too_short:
            _LOGGER.info(
                "SmartStart: %s — discarding run, only %.0fs elapsed (< %ds)",
                device_slug, duration_s, MIN_CYCLE_DURATION_S,
            )
            cycle_ended_extra = None
        else:
            cycle_ended_extra = None

        self._cancel_live_timer(device_slug)
        self._schedules[device_slug] = {"status": STATE_IDLE}
        await self._save()
        self._notify(device_slug)
        # Fire only for recorded cycles — too-short or unscheduled stops stay silent
        if cycle_ended_extra is not None:
            self._fire_event(
                "smartstart_cycle_ended",
                device_slug,
                cycle_key_override=cycle_id,
                extra=cycle_ended_extra,
            )

    async def _record_run(
        self,
        device_slug: str,
        sched: Dict,
        cycle_id: str,
        samples: list,
        *,
        ended_at: str,
        duration_m: float,
        kwh: float,
        cost: float,
        learned: bool,
        has_power: bool,
    ) -> None:
        from .run_history import compact_samples, rate_segments

        now_rate = sched.get("now_rate_cost_per_kwh")
        started_at = sched.get("running_since") or ended_at
        delay_m = None
        if sched.get("scheduled_at") and sched.get("triggered_by") == "smartstart":
            try:
                delay_m = round((
                    datetime.fromisoformat(started_at)
                    - datetime.fromisoformat(sched["scheduled_at"])
                ).total_seconds() / 60, 1)
            except ValueError:
                pass
        try:
            await self._run_history.async_add_run({
                "device_slug":      device_slug,
                "cycle_key":        cycle_id,
                "started_at":       started_at,
                "ended_at":         ended_at,
                "duration_minutes": round(duration_m, 1),
                # Without a power sensor kWh is 0 by construction, not measured
                "kwh":              round(kwh, 4) if has_power else None,
                "cost":             round(cost, 4) if has_power else None,
                "triggered_by":     sched.get("triggered_by", "smartstart"),
                "scheduled_at":     sched.get("scheduled_at"),
                "delay_minutes":    delay_m,
                "planned_rate":     sched.get("rate_cost_per_kwh"),
                "now_rate":         now_rate,
                # Cost had it started when it was scheduled, at that slot's
                # rate (flat — no profile). Savings = this − actual cost.
                "baseline_cost":    (
                    round(kwh * float(now_rate), 4)
                    if has_power and now_rate is not None else None
                ),
                "reschedule_count": sched.get("reschedule_count", 0),
                "laundry_run":      bool(sched.get("laundry_run")),
                "laundry":          sched.get("laundry"),
                "learned":          learned,
                "samples":          compact_samples(samples),
                "rates":            rate_segments(samples, self._rate_at),
            })
        except Exception:  # never let history break the idle transition
            _LOGGER.exception("SmartStart: failed to record run history for %s", device_slug)

    # ── Appliance reach ──────────────────────────────────────────────────────

    @staticmethod
    def _reachable_delay(
        device: Dict, delay_handling: str, window: ScoredWindow, now: datetime
    ) -> tuple[int, Optional[datetime]]:
        """(delay_seconds to send now, when to re-send) for a target window.

        Native-delay appliances can't take a delay beyond max_delay from the
        moment of the command (Home Connect StartInRelative tops out at 24h).
        A target further out — only reachable through an accepted offer —
        is sent at the appliance's furthest reach, then re-sent with the real
        delay once it fits. The target never changes in between, so the
        re-send is not a reschedule. For end_time appliances max_delay bounds
        the finish, so the overflow is measured on the window end.
        """
        raw_delay = max(0.0, (window.start - now).total_seconds())
        recommand_at: Optional[datetime] = None
        reach_s = int(device.get("max_delay_minutes") or 0) * 60
        if delay_handling != DELAY_HANDLING_MANAGED and reach_s > 0:
            edge = window.end if delay_handling == DELAY_HANDLING_END_TIME else window.start
            overflow = (edge - now).total_seconds() - reach_s
            if overflow > 0:
                raw_delay = max(0.0, raw_delay - overflow)
                # A minute's margin so the re-send lands inside the reach.
                recommand_at = now + timedelta(seconds=overflow + 60)
        step_s = int(device.get("delay_step_minutes", 1)) * 60
        if step_s > 60:
            # Round to the step, but never past the reach when clamped.
            steps = raw_delay // step_s if recommand_at else round(raw_delay / step_s)
            delay_seconds = int(steps * step_s)
        else:
            delay_seconds = int(raw_delay)
        return delay_seconds, recommand_at

    def _register_recommand_timer(self, slug: str, fire_at: datetime) -> None:
        # Shares the managed-timer slot: a device is either managed or native.
        self._cancel_timer(slug)

        @callback
        def _fire(_now):
            self._timers.pop(slug, None)
            self._hass.async_create_task(self._recommand(slug))

        self._timers[slug] = async_track_point_in_time(self._hass, _fire, fire_at)
        _LOGGER.debug("SmartStart: re-send for %s registered at %s", slug, fire_at)

    async def _recommand(self, slug: str) -> None:
        """Re-send the committed target once it's within the appliance's reach."""
        sched = self._schedules.get(slug, {})
        if sched.get("status") != STATE_SCHEDULED or not sched.get("recommand_at"):
            return
        try:
            start = datetime.fromisoformat(sched["scheduled_start"])
            end = datetime.fromisoformat(sched["estimated_finish"])
        except (KeyError, TypeError, ValueError):
            return
        now_ts = datetime.now(timezone.utc)
        target = ScoredWindow(
            start=start, end=end,
            avg_rate_cost_per_kwh=sched.get("rate_cost_per_kwh") or 0.0,
            delay_minutes=max(0, int((start - now_ts).total_seconds() // 60)),
            n_slots=0,
        )
        device = self._devices.get(slug, {})
        delay_seconds, recommand_at = self._reachable_delay(
            device, sched.get("delay_handling", DEFAULT_DELAY_HANDLING), target, now_ts
        )
        payload = {
            **sched.get("service_payload", {}),
            "delay_seconds":    delay_seconds,
            "start_time":       start.isoformat(),
            "estimated_finish": end.isoformat(),
            "reschedule":       True,
            # Same target, now in reach — scripts can skip "rescheduled" alerts.
            "recommand":        True,
        }
        sched["service_payload"] = payload
        sched["delay_seconds"] = delay_seconds
        sched["recommand_at"] = recommand_at.isoformat() if recommand_at else None
        await self._save()
        _LOGGER.info(
            "SmartStart: %s target %s now in reach — re-sending (delay=%ds)",
            slug, start.isoformat(), delay_seconds,
        )
        await self._call_service(sched.get("start_service", ""), payload, slug)
        if recommand_at is not None:  # pragma: no cover — reach shrank meanwhile
            self._register_recommand_timer(slug, recommand_at)

    def mark_offer_announced(self, slug: str, start: datetime, predicted: bool) -> None:
        """Persist the last announced offer so a restart doesn't re-announce it."""
        sched = self._schedules.get(slug)
        if not sched or sched.get("status") != STATE_SCHEDULED:
            return
        sched["offer_announced"] = {"start": start.isoformat(), "predicted": predicted}
        self._hass.async_create_task(self._save())

    def get_schedule(self, device_slug: str) -> Dict:
        return self._schedules.get(device_slug, {"status": STATE_IDLE})

    # ── Managed-mode timer ───────────────────────────────────────────────────

    def _register_managed_timer(self, slug: str, fire_at: datetime) -> None:
        self._cancel_timer(slug)

        @callback
        def _fire(_now):
            self._hass.async_create_task(self._fire_managed_start(slug))

        self._timers[slug] = async_track_point_in_time(self._hass, _fire, fire_at)
        _LOGGER.debug("SmartStart: managed timer registered for %s at %s", slug, fire_at)

    async def _fire_managed_start(self, slug: str) -> None:
        sched = self._schedules.get(slug, {})
        if sched.get("status") != STATE_SCHEDULED:
            _LOGGER.debug("SmartStart: managed timer for %s skipped (status=%s)", slug, sched.get("status"))
            return

        start_service = sched.get("start_service", "")
        payload = dict(sched.get("service_payload", {}))
        payload["delay_seconds"] = 0

        _LOGGER.info(
            "SmartStart: managed timer fired for %s/%s — calling %s now",
            slug, sched.get("cycle_id", "?"), start_service,
        )
        await self._call_service(start_service, payload, slug)
        self._timers.pop(slug, None)

    def _cancel_timer(self, slug: str) -> None:
        cancel = self._timers.pop(slug, None)
        if cancel:
            cancel()

    # ── Live stats (current cycle in progress) ───────────────────────────────

    def _start_live_timer(self, slug: str) -> None:
        """Notify listeners every minute while the device is running."""
        self._cancel_live_timer(slug)

        @callback
        def _tick(_now: datetime) -> None:
            self._notify(slug)

        self._live_timers[slug] = async_track_time_interval(
            self._hass, _tick, timedelta(minutes=1)
        )

    def _cancel_live_timer(self, slug: str) -> None:
        cancel = self._live_timers.pop(slug, None)
        if cancel:
            cancel()

    def get_current_stats(self, device_slug: str) -> dict:
        """Return live energy/cost/duration for a cycle currently in progress, or {}."""
        from .curve_store import _samples_to_kwh, _samples_to_cost

        sched = self._schedules.get(device_slug, {})
        if sched.get("status") != STATE_RUNNING:
            return {}

        scheduled_rate = float(sched.get("rate_cost_per_kwh") or 0.0)

        # Elapsed time from power recorder samples, or from running_since timestamp
        recorder = self._recorders.get(device_slug)
        if recorder:
            samples = recorder.current_samples
            if samples:
                elapsed_minutes = round(float(samples[-1]["t_s"]) / 60.0, 1)
                current_kwh     = _samples_to_kwh(samples)
                # Per-slot cost — uses the rate in effect at each sample's
                # wall-clock time, so negative-rate slots reduce the running cost.
                current_cost    = None
                if self._rate_at:
                    current_cost = _samples_to_cost(samples, self._rate_at)
                if current_cost is None:
                    # Fallback for legacy samples missing wall_ts
                    current_cost = round(current_kwh * scheduled_rate, 6)
                return {
                    "elapsed_minutes": elapsed_minutes,
                    "current_kwh":     current_kwh,
                    "current_cost":    current_cost,
                    "source":          "measured",
                }

        # No power sensor — compute elapsed from running_since, estimate energy linearly
        running_since_str = sched.get("running_since")
        if running_since_str:
            try:
                running_since   = datetime.fromisoformat(running_since_str)
                elapsed_minutes = round(
                    (datetime.now(timezone.utc) - running_since).total_seconds() / 60, 1
                )
                # Estimate energy proportional to elapsed fraction of configured duration
                cycle_id    = sched.get("cycle_id", "")
                device_cfg  = self._devices.get(device_slug, {})
                cycle_cfg   = next(
                    (c for c in device_cfg.get("cycles", []) if c["cycle_key"] == cycle_id), {}
                )
                config_dur  = float(cycle_cfg.get("duration_minutes", 0) or 0)
                config_kwh  = float(cycle_cfg.get("total_kwh", 0) or 0)
                if config_dur > 0:
                    frac        = min(elapsed_minutes / config_dur, 1.0)
                    current_kwh = round(frac * config_kwh, 4)
                else:
                    current_kwh = None
                # No power samples → best we can do is current rate × estimated kWh
                rate_now = self._rate_at(datetime.now(timezone.utc)) if self._rate_at else None
                if rate_now is None:
                    rate_now = scheduled_rate
                current_cost = round(current_kwh * rate_now, 6) if current_kwh else None
                return {
                    "elapsed_minutes": elapsed_minutes,
                    "current_kwh":     current_kwh,
                    "current_cost":    current_cost,
                    "source":          "estimated",
                }
            except (ValueError, TypeError):
                pass

        return {}

    # ── Running-entity watcher ───────────────────────────────────────────────

    def _state_matches(self, entity_id: str, candidates: List[str]) -> bool:
        """True if the entity's raw or translated state matches any candidate
        (case-insensitive). Lets users enter user-facing labels like 'Washing'
        without caring whether HA's raw state is 'washing' or 'wash_program'."""
        if not entity_id or not candidates:
            return False
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return False

        norm = {c.strip().casefold() for c in candidates if c and c.strip()}
        if state.state.casefold() in norm:
            return True

        # Try the translated state (e.g. enum sensors with user-facing labels)
        try:
            from homeassistant.helpers.template import Template
            tpl = Template(f"{{{{ state_translated('{entity_id}') }}}}", self._hass)
            translated = tpl.async_render()
            if isinstance(translated, str) and translated.casefold() in norm:
                return True
        except Exception:  # noqa: BLE001 — translation may be unavailable
            pass
        return False

    def _register_running_watcher(self, slug: str, device: Dict) -> None:
        running_entity = device.get("running_entity")
        if not running_entity:
            return

        running_states = list(device.get("running_states") or DEFAULT_RUNNING_STATES)
        paused_states  = list(device.get("paused_states") or [])
        paused_entity  = device.get("paused_entity") or running_entity

        watched = {running_entity}
        if paused_states:
            watched.add(paused_entity)

        @callback
        def _on_state_change(_event):
            self._hass.async_create_task(self._reconcile_status(slug))

        cancels = [
            async_track_state_change_event(self._hass, eid, _on_state_change)
            for eid in watched
        ]

        @callback
        def _cancel_all():
            for c in cancels:
                c()

        if slug in self._watchers:
            self._watchers[slug]()
        self._watchers[slug] = _cancel_all

    def _is_unavailable(self, entity_id: str) -> bool:
        """True if HA has the entity but its state is unavailable/unknown, or
        the entity isn't in the state machine at all."""
        if not entity_id:
            return False
        state = self._hass.states.get(entity_id)
        return state is None or state.state in ("unavailable", "unknown")

    def _arm_idle_grace(self, slug: str) -> None:
        """Start (or leave armed) the grace timer that ends a cycle if the
        running entity stays unavailable past UNAVAILABLE_GRACE_SECONDS."""
        if slug in self._idle_grace_timers:
            return  # already armed
        fire_at = datetime.now(timezone.utc) + timedelta(seconds=UNAVAILABLE_GRACE_SECONDS)
        _LOGGER.info(
            "SmartStart: %s running_entity unavailable while cycle active — "
            "holding state until %s before falling to idle",
            slug, fire_at.strftime("%H:%M:%S"),
        )

        @callback
        def _expire(_now):
            self._idle_grace_timers.pop(slug, None)
            self._hass.async_create_task(self._idle_grace_expired(slug))

        self._idle_grace_timers[slug] = async_track_point_in_time(
            self._hass, _expire, fire_at,
        )

    def _cancel_idle_grace(self, slug: str) -> None:
        cancel = self._idle_grace_timers.pop(slug, None)
        if cancel:
            cancel()
            _LOGGER.debug("SmartStart: %s running_entity recovered — grace cancelled", slug)

    async def _idle_grace_expired(self, slug: str) -> None:
        """Grace window elapsed. End the cycle if the sensor is still unavailable;
        if it recovered between scheduling and firing, re-reconcile normally."""
        device = self._devices.get(slug, {})
        if not self._is_unavailable(device.get("running_entity")):
            await self._reconcile_status(slug)
            return
        current_status = self._schedules.get(slug, {}).get("status", STATE_IDLE)
        if current_status in (STATE_RUNNING, STATE_PAUSED):
            _LOGGER.warning(
                "SmartStart: %s running_entity still unavailable after %ds grace — ending cycle",
                slug, UNAVAILABLE_GRACE_SECONDS,
            )
            await self.async_set_idle(slug)

    async def _reconcile_status(self, slug: str) -> None:
        """Evaluate the device's running/paused state and drive transitions.

        Special case: if the running entity is unavailable/unknown while the
        cycle is RUNNING or PAUSED, hold the current state and arm a grace
        timer — see _arm_idle_grace. This survives HA restarts and short
        sensor blips without prematurely ending the cycle.
        """
        device = self._devices.get(slug, {})
        running_entity = device.get("running_entity")
        paused_entity  = device.get("paused_entity") or running_entity
        running_states = list(device.get("running_states") or DEFAULT_RUNNING_STATES)
        paused_states  = list(device.get("paused_states") or [])

        current_status = self._schedules.get(slug, {}).get("status", STATE_IDLE)

        # Sensor is unavailable AND we were mid-cycle — don't end the cycle.
        # Arm the grace timer (no-op if already armed) and bail.
        if (
            current_status in (STATE_RUNNING, STATE_PAUSED)
            and self._is_unavailable(running_entity)
        ):
            self._arm_idle_grace(slug)
            return

        # Sensor is either available again or we're not mid-cycle — cancel any
        # pending grace fallout and run the normal transition logic.
        self._cancel_idle_grace(slug)

        is_paused  = self._state_matches(paused_entity, paused_states)
        is_running = self._state_matches(running_entity, running_states) and not is_paused

        if is_paused and current_status != STATE_PAUSED:
            _LOGGER.info("SmartStart: %s entered paused state — status → paused", slug)
            await self.async_set_paused(slug)
        elif is_running and current_status != STATE_RUNNING:
            _LOGGER.info("SmartStart: %s entered running state — status → running", slug)
            await self.async_set_running(slug)
        elif not is_running and not is_paused and current_status in (STATE_RUNNING, STATE_PAUSED):
            _LOGGER.info("SmartStart: %s left running/paused — status → idle", slug)
            await self.async_set_idle(slug)

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _call_service(self, service_str: str, payload: Dict, device_slug: str = "") -> None:
        """Dispatch the user's start_service. Catches each failure mode that
        an integration-level service call can hit and fires a
        smartstart_action_failed event so users can wire notifications
        (e.g. "send me a push when scheduling fails")."""
        from homeassistant.exceptions import (
            HomeAssistantError,
            ServiceNotFound,
            ServiceValidationError,
        )

        def _fail(reason: str, detail: str) -> None:
            _LOGGER.error(
                "SmartStart: %s start_service %r failed (%s): %s",
                device_slug or "?", service_str, reason, detail,
            )
            self._hass.bus.async_fire(
                "smartstart_action_failed",
                {
                    "device_slug": device_slug,
                    "action": "start",
                    "service": service_str,
                    "reason": reason,
                    "detail": detail,
                },
            )

        if not service_str or "." not in service_str:
            _fail("invalid_service", f"not in domain.service form: {service_str!r}")
            return
        domain, name = service_str.split(".", 1)

        # Pre-check button entities — blocking=False would swallow the
        # "entity not found" condition silently. Catching it upfront lets us
        # report a useful reason.
        if domain == "button":
            entity_state = self._hass.states.get(service_str)
            if entity_state is None:
                _fail("entity_missing", f"button entity {service_str} not in state machine")
                return
            if entity_state.state in ("unavailable", "unknown"):
                _fail("entity_unavailable", f"button {service_str} state={entity_state.state}")
                return

        try:
            if domain == "button":
                _LOGGER.info("SmartStart: pressing button %s (no payload)", service_str)
                await self._hass.services.async_call(
                    "button", "press", {"entity_id": service_str}, blocking=False
                )
            else:
                _LOGGER.info(
                    "SmartStart: calling %s.%s (delay_seconds=%s, reschedule=%s)",
                    domain, name,
                    payload.get("delay_seconds", "—"),
                    payload.get("reschedule", "—"),
                )
                await self._hass.services.async_call(domain, name, payload, blocking=False)
        except ServiceNotFound as exc:
            _fail("service_not_found", str(exc))
        except ServiceValidationError as exc:
            _fail("validation_error", str(exc))
        except HomeAssistantError as exc:
            _fail("ha_error", str(exc))
        except Exception as exc:  # noqa: BLE001 — last-resort catch-all
            _fail("unexpected", f"{type(exc).__name__}: {exc}")

    async def _save(self) -> None:
        await self._store.async_save({"schedules": self._schedules})

    def _fire_event(
        self,
        event_type: str,
        device_slug: str,
        cycle_key_override: Optional[str] = None,
        extra: Optional[Dict] = None,
    ) -> None:
        """Fire an HA event with a consistent SmartStart payload.

        Payload always includes device_slug/name and (if known) cycle identifiers.
        cycle_id in the payload is the user-facing string (or the cycle name if
        the user left cycle_id blank) — matches what we send to start scripts.
        """
        device = self._devices.get(device_slug, {})
        sched  = self._schedules.get(device_slug, {})
        cycle_key = cycle_key_override or sched.get("cycle_id")  # scheduler stores cycle_key here
        cycle_cfg = next(
            (c for c in device.get("cycles", []) if c.get("cycle_key") == cycle_key), {}
        )
        payload = {
            "device_slug": device_slug,
            "device_name": device.get("name", device_slug),
            "cycle_key":   cycle_key,
            "cycle_name":  cycle_cfg.get("name"),
            "cycle_id":    cycle_cfg.get("cycle_id") or cycle_cfg.get("name", ""),
        }
        if extra:
            payload.update(extra)
        self._hass.bus.async_fire(event_type, payload)

    def add_listener(self, listener: Callable[[str], None]) -> None:
        self._listeners.append(listener)

    def _notify(self, slug: str) -> None:
        for cb in self._listeners:
            try:
                cb(slug)
            except Exception as exc:
                _LOGGER.error("SmartStart: listener error for %s: %s", slug, exc)
        if self._rescore:
            self._rescore()
