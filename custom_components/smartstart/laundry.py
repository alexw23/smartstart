"""LaundryManager — schedules flat wash/dry step plans across a washer+dryer pair.

One manager per laundry config entry. The paired appliances usually live in
OTHER config entries (the user runs one entry per appliance), so devices are
resolved lazily across entries on every use — entry load order is not
guaranteed.

The manager owns plan-level state only. Each step is executed through the
owning device's own SmartStartScheduler (so the appliance's entities, events,
power recording, and curve learning all behave exactly as for a normal run),
with laundry_run=True in the schedule so the device coordinator's
auto-reschedule / better-window paths stand down — composite re-optimisation
happens here instead.
"""
from __future__ import annotations

import re
from itertools import permutations
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple
import logging
import uuid

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_POWER_DOWN,
    POWER_DOWN_AVOID,
    CONF_BEDTIME,
    CONF_MAX_WAIT_HOURS,
    CONF_MAX_WET_WAIT,
    CONF_TIME_VALUE,
    CONF_WAKE_TIME,
    DEFAULT_MAX_WAIT_HOURS,
    DEFAULT_TIME_VALUE,
    DEFAULT_BEDTIME,
    DEFAULT_MAX_WET_WAIT,
    DEFAULT_WAKE_TIME,
    CONF_DRYER_SLUG,
    CONF_GAP_MINUTES,
    CONF_FLOWS,
    CONF_WASHER_SLUG,
    CURVE_USE_THRESHOLD,
    DRY_LABEL_PREFIX,
    DEFAULT_BALANCED_TARGET_RATE,
    DEFAULT_DELAY_HANDLING,
    DEFAULT_DELAY_STEP_MINUTES,
    DEFAULT_GAP_MINUTES,
    DEFAULT_MAX_DELAY_MINUTES,
    DEFAULT_MIN_DELAY_MINUTES,
    DEFAULT_MODE,
    DELAY_HANDLING_END_TIME,
    DELAY_HANDLING_MANAGED,
    DOMAIN,
    ENTRY_TYPE_LAUNDRY,
    CONF_ENTRY_TYPE,
    FLOW_LABEL_PREFIX,
    LAUNDRY_ORDER_AS_LISTED,
    LAUNDRY_ORDER_EXHAUSTIVE_MAX,
    LAUNDRY_ORDER_OPTIMISED,
    LAUNDRY_ROWS,
    LAUNDRY_STATE_AWAITING,
    LAUNDRY_STATE_IDLE,
    LAUNDRY_STATE_RUNNING,
    LAUNDRY_STATE_SCHEDULED,
    LAUNDRY_STORAGE_KEY,
    MODE_BALANCED,
    MODE_NOW,
    OFFER_MIN_FRACTION,
    OFFER_MIN_SAVING,
    OFFER_REFIRE_SHIFT_MIN,
    PLANNER_HORIZON_HOURS,
    RESCHEDULE_RATE_EPSILON,
    ROW_NONE_LABEL,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RUNNING,
    STATE_SCHEDULED,
    STORAGE_VERSION,
    WASH_LABEL_PREFIX,
)
from .laundry_plan import (
    MACHINE_DRY,
    MACHINE_WASH,
    PlanStep,
    build_composite_profile,
    derive_offsets,
    max_wet_wait,
    unblocked_steps,
    validate_plan,
)
from .window_scorer import (
    ScoredWindow,
    _avg_rate,
    _time_weighted_rate,
    find_soonest_below_rate,
    find_window_for_deadline,
    score_windows,
    window_band,
)

_LOGGER = logging.getLogger(__name__)

_STEP_PENDING = "pending"
_STEP_FIRED   = "fired"    # commanded; waiting for the device to report running
_STEP_RUNNING = "running"
_STEP_DONE    = "done"

# A fired step whose device hasn't reported running this long after its
# expected start is "stalled" (machine asleep, door open, command lost).
STALL_AFTER_MINUTES = 15
# "Before you leave" heads-up this long before an away period starts.
LEAVING_NOTICE_MINUTES = 45
# Continue rescoring: candidate delayed starts on this grid, this far out.
CONTINUE_STEP_MINUTES = 30
CONTINUE_HORIZON_HOURS = 12


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_hhmm(raw: Optional[str], default: str) -> time:
    for value in (raw, default):
        try:
            hh, mm = (int(x) for x in str(value).split(":")[:2])
            return time(hh, mm)
        except (TypeError, ValueError):
            continue
    return time(0, 0)


def _from_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def all_laundry_managers(hass: HomeAssistant) -> List["LaundryManager"]:
    return [
        store["laundry"]
        for store in hass.data.get(DOMAIN, {}).values()
        if isinstance(store, dict) and "laundry" in store
    ]


def find_claiming_manager(hass: HomeAssistant, device_slug: str) -> Optional["LaundryManager"]:
    """The LaundryManager whose active run still needs this device, if any.
    Consulted by the device-level Start buttons so a claimed washer/dryer
    can't be double-booked under a live plan."""
    for mgr in all_laundry_managers(hass):
        if mgr.claims(device_slug):
            return mgr
    return None


LAUNDRY_MAX_LOADS = 200


class LaundryManager:
    def __init__(self, hass: HomeAssistant, entry_id: str, config: Dict) -> None:
        self._hass     = hass
        self._entry_id = entry_id
        self.name      = config.get("name", "Laundry")
        self.slug      = _slugify(self.name) or "laundry"
        self._washer_slug = config[CONF_WASHER_SLUG]
        self._dryer_slug  = config[CONF_DRYER_SLUG]
        self._gap         = float(config.get(CONF_GAP_MINUTES, DEFAULT_GAP_MINUTES))
        self._auto_reschedule = bool(config.get("auto_reschedule", False))
        self._hold_for_rates  = bool(config.get("hold_for_next_day_rates", False))
        self._flows: List[Dict] = list(config.get(CONF_FLOWS, []))
        # Sleep window: no changeovers planned between bedtime and wake
        # (equal times = off). Wet-wait cap: a start whose plan leaves washed
        # laundry waiting longer than this for its dry is not chosen.
        self._bedtime = _parse_hhmm(config.get(CONF_BEDTIME), DEFAULT_BEDTIME)
        self._wake    = _parse_hhmm(config.get(CONF_WAKE_TIME), DEFAULT_WAKE_TIME)
        self._wet_cap_minutes = 60.0 * float(
            config.get(CONF_MAX_WET_WAIT, DEFAULT_MAX_WET_WAIT) or 0
        )
        # Load-order optimiser blends cost with time-to-finish at this £/h.
        self._time_value = float(config.get(CONF_TIME_VALUE, DEFAULT_TIME_VALUE) or 0)
        # Reschedule envelope / offer horizon, fixed from the Start press (as
        # for appliances): the plan must finish within this many hours.
        self._max_wait_hours = int(config.get(CONF_MAX_WAIT_HOURS) or DEFAULT_MAX_WAIT_HOURS)

        self._store = Store(hass, STORAGE_VERSION, f"{LAUNDRY_STORAGE_KEY}_{entry_id}")
        self._run: Optional[Dict] = None
        # Prospective plan for the current selections, scored before Start is
        # pressed so the Planned sensors preview the window. None when no
        # valid plan is selected or a run is active.
        self._preview: Optional[Dict] = None
        # Row selections: one "Load N" label per row — a flow name, or a
        # single cycle as "Wash: X" / "Dry: Y".
        self._rows: List[str] = [ROW_NONE_LABEL] * LAUNDRY_ROWS
        self._mode: str = DEFAULT_MODE
        # Power-down handling for the whole plan (avoid | ignore); see
        # _plan_offsets for how a blackout splits the plan.
        self._power_down: str = DEFAULT_POWER_DOWN
        self._due_at: Optional[datetime] = None
        # Per-plan "next changeover after" override (lie-in, out until 9pm);
        # cleared after each Start like Due By.
        self._unavailable_until: Optional[datetime] = None
        # Repeat-every-N-days flows: flow slug → last completed (ISO), and
        # flow slug → the due date already auto-filled (so clearing the row
        # doesn't get it re-added every minute). Persisted with the run.
        self._last_done: Dict[str, str] = {}
        self._autofilled: Dict[str, str] = {}
        # Completed loads, oldest first (panel history + flow rhythm).
        self._loads: List[Dict] = []
        # Away periods: no changeovers between start and end (end None = not
        # back until the next wake time). Persisted; pruned once passed.
        self._away: List[Dict] = []
        self._away_spans: List[Tuple[datetime, datetime]] = []
        # "Optimised" lets Start reorder the Loads; "As listed" keeps them.
        self._order_mode: str = LAUNDRY_ORDER_OPTIMISED
        # Order search is the expensive part — cached by its inputs (see
        # _plan_choice); the chosen order is still rescored fresh each time.
        self._choice_key = None
        self._choice: Optional[Dict] = None
        # Current offer for the scheduled run (display dict + its window).
        self._offer: Optional[Dict] = None
        self._offer_window: Optional[ScoredWindow] = None
        # What Continue would do right now (delay-start option), refreshed
        # each minute while a changeover is awaited.
        self._continue_opt: Optional[Dict] = None

        self._listeners: List[Callable[[], None]] = []
        self._subscribed_schedulers: set = set()
        self._coordinator_unsub: Optional[Callable] = None
        self._resched_inflight = False
        self._unloaded = False

    # ── Cross-entry device resolution ────────────────────────────────────────

    def _resolve(self, device_slug: str) -> Tuple[Dict, Dict]:
        """(hass.data store, device config) for the entry owning device_slug."""
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            config = entry.options if entry.options else entry.data
            if config.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_LAUNDRY:
                continue
            device = next(
                (d for d in config.get("devices", []) if d.get("device_slug") == device_slug),
                None,
            )
            if device is not None:
                return self._hass.data.get(DOMAIN, {}).get(entry.entry_id, {}), device
        return {}, {}

    def _slug_for(self, machine: str) -> str:
        return self._washer_slug if machine == MACHINE_WASH else self._dryer_slug

    def device_config(self, machine: str) -> Dict:
        return self._resolve(self._slug_for(machine))[1]

    def washer_coordinator(self):
        return self._resolve(self._washer_slug)[0].get("coordinator")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def async_load(self) -> None:
        raw = await self._store.async_load() or {}
        self._run = raw.get("run")
        self._last_done = dict(raw.get("last_done") or {})
        self._autofilled = dict(raw.get("autofilled") or {})
        self._loads = list(raw.get("loads") or [])
        self._order_mode = raw.get("order_mode") or LAUNDRY_ORDER_OPTIMISED
        self._set_away(list(raw.get("away") or []))
        if self._run and not self._run.get("id"):
            self._run["id"] = uuid.uuid4().hex  # run started before load ids
        if self._seed_last_done(_utcnow()):
            await self._save()
        self._ensure_subscriptions()
        if self._run:
            _LOGGER.info(
                "SmartStart laundry: %s resumed run in %s after restart",
                self.slug, self._run.get("status"),
            )
            # Device states may have moved while HA was down.
            self._hass.async_create_task(self._reconcile_all())

    async def async_teardown(self) -> None:
        self._unloaded = True
        if self._coordinator_unsub:
            self._coordinator_unsub()
            self._coordinator_unsub = None

    def _ensure_subscriptions(self) -> None:
        """Attach to the paired devices' schedulers/coordinator once their
        entries are loaded. Retried from the minute tick — load order between
        the laundry entry and the appliance entries is not guaranteed."""
        for slug in (self._washer_slug, self._dryer_slug):
            if slug in self._subscribed_schedulers:
                continue
            scheduler = self._resolve(slug)[0].get("scheduler")
            if scheduler is None:
                continue
            scheduler.add_listener(self._make_device_listener(slug))
            self._subscribed_schedulers.add(slug)
        if self._coordinator_unsub is None:
            coordinator = self.washer_coordinator()
            if coordinator is not None:
                self._coordinator_unsub = coordinator.async_add_listener(
                    self._on_rates_update
                )

    def _make_device_listener(self, slug: str) -> Callable[[str], None]:
        @callback
        def _listener(changed_slug: str) -> None:
            if self._unloaded or changed_slug != slug or self._run is None:
                return
            self._hass.async_create_task(self._reconcile_device(slug))
        return _listener

    @callback
    def minute_tick(self, _now=None) -> None:
        self._ensure_subscriptions()
        now = _utcnow()
        self._check_due(now)
        if self._prune_away(now):
            self._hass.async_create_task(self._save())
        if self._run is None:
            # Keep the idle preview honest as time passes (a previewed window
            # can slip into the past between rate updates).
            self._refresh_preview()
        else:
            self._update_offer(now)
            self._check_stalled(now)
            self._check_leaving(now)
            if self._run is not None and self._run.get("status") == LAUNDRY_STATE_RUNNING:
                self._hass.async_create_task(self._changeover_while_running())
            if self._run is not None and self._run.get("status") == LAUNDRY_STATE_AWAITING:
                self._continue_opt = self._continue_option(now)
        self._notify()

    # ── Listeners (entities) ─────────────────────────────────────────────────

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)
        return _remove

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("SmartStart laundry: listener error: %s", exc)

    # ── Row / mode / deadline state ──────────────────────────────────────────

    def row_options(self) -> List[str]:
        """Options for a Load select: saved flows by name, then single wash
        and dry cycles. Ad-hoc wash+dry combos are two rows (or a flow)."""
        opts = [ROW_NONE_LABEL]
        opts += [f["name"] for f in self._flows if f.get("steps")]
        opts += [
            f"{WASH_LABEL_PREFIX}{c['name']}"
            for c in self.device_config(MACHINE_WASH).get("cycles", [])
        ]
        opts += [
            f"{DRY_LABEL_PREFIX}{c['name']}"
            for c in self.device_config(MACHINE_DRY).get("cycles", [])
        ]
        return opts

    def get_row_selection(self, idx: int) -> str:
        return self._rows[idx]

    def set_row_selection(self, idx: int, label: str) -> None:
        # Pre-merge restored labels used "Flow: X" — map to the flow name.
        if label.startswith(FLOW_LABEL_PREFIX):
            label = label[len(FLOW_LABEL_PREFIX):]
        self._rows[idx] = label
        self._refresh_preview()
        self._notify()

    def _reset_rows_after_run(self) -> None:
        # Load 1 keeps its last-used selection; later rows reset so the
        # everyday single load stays zero-config. A repeating flow resets too —
        # it re-fills itself when next due, and keeping it would drag it into
        # the next (auto-filled) plan.
        first = self._flow_by_label(self._rows[0])
        keep_first = first is None or int(first.get("repeat_days") or 0) <= 0
        for i in range(0 if not keep_first else 1, len(self._rows)):
            self._rows[i] = ROW_NONE_LABEL

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._refresh_preview()
        self._notify()

    @property
    def power_down(self) -> str:
        return self._power_down

    def set_power_down(self, handling: str) -> None:
        self._power_down = handling
        self._refresh_preview()
        self._notify()

    def _blackout_spans(self, when: datetime) -> List[Tuple[datetime, datetime]]:
        """Blackouts this pair keeps its machines out of (none when set to
        Ignore, or no calendars / washer coordinator)."""
        if self._power_down != POWER_DOWN_AVOID:
            return []
        coordinator = self.washer_coordinator()
        return coordinator.blackout_spans(when) if coordinator else []

    def _first_step_blocked(self, steps: List[PlanStep], start: datetime) -> bool:
        """The plan's first step runs at the plan start; it can't wait for a
        blackout to end (that would be a different start), so a start whose
        first step overlaps one is simply not a candidate."""
        if not steps:
            return False
        end = start + timedelta(minutes=steps[0].duration_minutes)
        return any(b0 < end and start < b1 for b0, b1 in self._blackout_spans(start))

    @property
    def due_at(self) -> Optional[datetime]:
        return self._due_at

    def set_due_at(self, deadline: Optional[datetime]) -> None:
        self._due_at = deadline
        self._refresh_preview()
        self._notify()

    @property
    def gap_minutes(self) -> float:
        return self._gap

    @property
    def unavailable_until(self) -> Optional[datetime]:
        """"Changeover After" — now just an away period from now until then
        (one concept on the panel). Its end, if one is set."""
        return next(
            (end for a, (_, end) in zip(self._away, self._away_spans) if a.get("kind") == "until"),
            None,
        )

    def set_unavailable_until(self, when: Optional[datetime]) -> None:
        periods = [a for a in self._away if a.get("kind") != "until"]
        if when is not None and when > _utcnow():
            periods.append({"start": _iso(_utcnow()), "end": _iso(when), "kind": "until"})
        self._set_away(periods)
        self._hass.async_create_task(self._save())
        self._refresh_preview()
        self._notify()

    @property
    def wake_time(self) -> time:
        return self._wake

    @property
    def bedtime(self) -> time:
        return self._bedtime

    @property
    def order_mode(self) -> str:
        return self._order_mode

    def set_order_mode(self, mode: str) -> None:
        self._order_mode = (
            LAUNDRY_ORDER_AS_LISTED if mode == LAUNDRY_ORDER_AS_LISTED else LAUNDRY_ORDER_OPTIMISED
        )
        self._hass.async_create_task(self._save())
        self._refresh_preview()
        self._notify()

    # ── Away periods ─────────────────────────────────────────────────────────

    @property
    def away(self) -> List[Dict]:
        """Away periods with their effective end (``until`` resolves an
        open-ended "not back today" to the next wake time)."""
        return [
            {"start": a["start"], "end": a.get("end"), "until": _iso(span[1]), "kind": a.get("kind")}
            for a, span in zip(self._away, self._away_spans)
        ]

    def _set_away(self, periods: List[Dict]) -> None:
        kept, spans = [], []
        for a in periods:
            start = _from_iso(a.get("start"))
            if start is None:
                continue
            end = _from_iso(a.get("end")) or self._next_wake(start)
            if end <= start:
                continue
            kept.append({
                "start": _iso(start), "end": _iso(_from_iso(a.get("end"))),
                **({"kind": a["kind"]} if a.get("kind") else {}),
                **({"announced": True} if a.get("announced") else {}),
            })
            spans.append((start, end))
        order = sorted(range(len(kept)), key=lambda i: spans[i][0])
        self._away = [kept[i] for i in order]
        self._away_spans = [spans[i] for i in order]

    async def async_add_away(self, start: datetime, end: Optional[datetime]) -> None:
        if end is not None and end <= start:
            raise HomeAssistantError(f"Laundry {self.name}: away period ends before it starts")
        self._set_away(self._away + [{"start": _iso(start), "end": _iso(end)}])
        await self._save()
        self._refresh_preview()
        self._notify()

    async def async_remove_away(self, index: int) -> None:
        if not 0 <= index < len(self._away):
            raise HomeAssistantError(f"Laundry {self.name}: no away period {index + 1}")
        self._set_away(self._away[:index] + self._away[index + 1:])
        await self._save()
        self._refresh_preview()
        self._notify()

    def _prune_away(self, now: datetime) -> bool:
        if not any(end <= now for _, end in self._away_spans):
            return False
        self._set_away([a for a, (_, end) in zip(self._away, self._away_spans) if end > now])
        self._refresh_preview()
        return True

    def _next_wake(self, when: datetime) -> datetime:
        local = dt_util.as_local(when)
        wake = datetime.combine(local.date(), self._wake, tzinfo=local.tzinfo)
        if wake <= local:
            wake = datetime.combine(local.date() + timedelta(days=1), self._wake, tzinfo=local.tzinfo)
        return dt_util.as_utc(wake)

    # ── Changeover availability ──────────────────────────────────────────────

    def _available_from(self, when: datetime, unavailable_until: Optional[datetime]) -> datetime:
        """Earliest moment >= when that a human can do a changeover: past any
        per-plan override, away periods and the nightly sleep window (looped,
        since leaving one can land in another)."""
        if unavailable_until is not None and when < unavailable_until:
            when = unavailable_until
        for _ in range(8):
            moved = when
            for start, end in self._away_spans:
                if start <= moved < end:
                    moved = end
            moved = self._past_sleep(moved)
            if moved == when:
                break
            when = moved
        return when

    def _past_sleep(self, when: datetime) -> datetime:
        bed, wake = self._bedtime, self._wake
        if bed == wake:
            return when
        local = dt_util.as_local(when)
        tod = local.time()
        if bed > wake:  # window crosses midnight (23:00–07:00)
            if tod >= bed:
                day = local.date() + timedelta(days=1)
            elif tod < wake:
                day = local.date()
            else:
                return when
        elif bed <= tod < wake:  # same-day window (e.g. 01:00–09:00)
            day = local.date()
        else:
            return when
        return dt_util.as_utc(datetime.combine(day, wake, tzinfo=local.tzinfo))

    def _plan_offsets(
        self, steps: List[PlanStep], t0: datetime, unavailable_until: Optional[datetime]
    ) -> List[float]:
        def changeover_at(off: float) -> float:
            at = self._available_from(t0 + timedelta(minutes=off), unavailable_until)
            return (at - t0).total_seconds() / 60.0

        # Blackouts as offsets from t0: a step that would run into one waits
        # for it to end, so the plan splits (wash, pause, dry) instead of a
        # machine running through a power-down.
        rel = [
            ((b0 - t0).total_seconds() / 60.0, (b1 - t0).total_seconds() / 60.0)
            for b0, b1 in self._blackout_spans(t0)
        ]

        def clear_of(start: float, duration: float) -> float:
            for _ in range(len(rel) + 1):
                moved = False
                for b0, b1 in rel:
                    if b0 < start + duration and start < b1:
                        start = b1
                        moved = True
                if not moved:
                    break
            return start

        return derive_offsets(
            steps, self._gap, changeover_at=changeover_at,
            clear_of=clear_of if rel else None,
        )

    def _plan_shape(
        self,
        steps: List[PlanStep],
        t0: datetime,
        unavailable_until: Optional[datetime],
        *,
        enforce_cap: bool = False,
    ) -> Optional[Tuple[List[float], List[float], float, float]]:
        """(offsets, profile, total_duration, total_kwh) for the plan starting
        at t0 — changeovers pushed past the sleep window. None when
        enforce_cap and washed laundry would wait past the wet-wait cap."""
        offsets = self._plan_offsets(steps, t0, unavailable_until)
        if (
            enforce_cap and self._wet_cap_minutes > 0
            and max_wet_wait(steps, offsets) > self._wet_cap_minutes + self._gap
        ):
            return None
        profile, total_dur, total_kwh = build_composite_profile(steps, offsets)
        return offsets, profile, total_dur, total_kwh

    def _shape_fn(
        self, steps: List[PlanStep], now: datetime, unavailable_until: Optional[datetime]
    ):
        """window_scorer ProfileFn: per-candidate (profile, duration). The cap
        doesn't apply to an immediate start under Mode Now (explicit intent —
        it must always have a window); in other modes "now" is a candidate
        like any other, so a plan that would leave washing sitting too long
        isn't chosen just because it starts immediately. Profiles are cached
        by offsets: most candidates share a timeline, only those whose
        changeovers hit the night differ."""
        cache: Dict[Tuple[float, ...], Tuple[List[float], float]] = {}
        exempt_now = self._mode == MODE_NOW

        def fn(start: datetime):
            if (
                steps and steps[0].attended and start != now
                and self._available_from(start, unavailable_until) != start
            ):
                return None  # in-use-overnight load can't be pre-loaded for a night start
            if (
                not (exempt_now and start == now)
                and self._first_step_blocked(steps, start)
            ):
                return None  # first step would run through a power-down
            offsets = self._plan_offsets(steps, start, unavailable_until)
            if (
                not (exempt_now and start == now) and self._wet_cap_minutes > 0
                and max_wet_wait(steps, offsets) > self._wet_cap_minutes + self._gap
            ):
                return None
            key = tuple(round(o, 2) for o in offsets)
            if key not in cache:
                profile, total_dur, _ = build_composite_profile(steps, offsets)
                cache[key] = (profile, total_dur)
            return cache[key]
        return fn

    @property
    def flows(self) -> List[Dict]:
        return list(self._flows)

    # ── Status / claiming ────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._run.get("status", LAUNDRY_STATE_IDLE) if self._run else LAUNDRY_STATE_IDLE

    def claims(self, device_slug: str) -> bool:
        """True while an active run still has undone steps on this device."""
        if self._run is None or device_slug not in (self._washer_slug, self._dryer_slug):
            return False
        machine = MACHINE_WASH if device_slug == self._washer_slug else MACHINE_DRY
        steps = self._run_steps()
        return any(
            s.machine == machine and st != _STEP_DONE
            for s, st in zip(steps, self._run["states"])
        )

    # ── Repeat-every-N-days flows ────────────────────────────────────────────

    @staticmethod
    def _flow_slug(flow: Dict) -> str:
        return flow.get("slug") or _slugify(flow.get("name", ""))

    def _flow_due(self, flow: Dict) -> Optional[datetime]:
        days = int(flow.get("repeat_days") or 0)
        last = _from_iso(self._last_done.get(self._flow_slug(flow)))
        if days <= 0 or last is None:
            return None
        return last + timedelta(days=days)

    def _seed_last_done(self, now: datetime) -> bool:
        """A flow that just gained an interval starts counting from now —
        no history must not mean "due immediately" for every flow."""
        changed = False
        for flow in self._flows:
            slug = self._flow_slug(flow)
            if int(flow.get("repeat_days") or 0) > 0 and slug not in self._last_done:
                self._last_done[slug] = _iso(now)
                changed = True
        return changed

    def _check_due(self, now: datetime) -> None:
        """Auto-fill the next empty Load with any flow due within the planner
        horizon, once per due date, and let the earliest future due date
        become the plan deadline (unless the user already set one)."""
        if self._run is not None:
            return
        changed = self._seed_last_done(now)
        horizon = now + timedelta(hours=PLANNER_HORIZON_HOURS)
        due_flows = sorted(
            ((due, flow) for flow in self._flows
             if (due := self._flow_due(flow)) is not None and due <= horizon),
            key=lambda x: x[0],
        )
        for due, flow in due_flows:
            slug = self._flow_slug(flow)
            if self._autofilled.get(slug) == _iso(due):
                continue
            name = flow["name"]
            if name not in self._rows:
                try:
                    idx = self._rows.index(ROW_NONE_LABEL)
                except ValueError:
                    _LOGGER.info(
                        "SmartStart laundry: %s — %s is due but every Load is taken",
                        self.slug, name,
                    )
                    continue
                self._rows[idx] = name
                self._fire_event("smartstart_laundry_load_due", {
                    "flow":   name,
                    "due_at": _iso(due),
                    "load":   idx + 1,
                })
                _LOGGER.info(
                    "SmartStart laundry: %s — %s due %s, filled Load %d",
                    self.slug, name, dt_util.as_local(due).strftime("%a %H:%M"), idx + 1,
                )
            self._autofilled[slug] = _iso(due)
            if due > now and (self._due_at is None or due < self._due_at):
                self._due_at = due
            changed = True
        if changed:
            self._hass.async_create_task(self._save())
            self._refresh_preview()
            self._notify()

    def flows_due(self) -> List[Dict]:
        out = []
        for flow in self._flows:
            if int(flow.get("repeat_days") or 0) <= 0:
                continue
            due = self._flow_due(flow)
            out.append({
                "flow":        flow["name"],
                "repeat_days": int(flow["repeat_days"]),
                "last_done":   self._last_done.get(self._flow_slug(flow)),
                "next_due":    _iso(due),
            })
        return out

    # ── Plan building ────────────────────────────────────────────────────────

    def _flow_by_label(self, label: str) -> Optional[Dict]:
        return next((f for f in self._flows if f.get("name") == label), None)

    def _make_step(
        self,
        machine: str,
        cycle_key: str,
        *,
        confirm_before: bool = True,
        duration_override: Optional[float] = None,
        kwh_override: Optional[float] = None,
        flow_slug: Optional[str] = None,
    ) -> PlanStep:
        slug = self._slug_for(machine)
        store, device = self._resolve(slug)
        cycle = next(
            (c for c in device.get("cycles", []) if c.get("cycle_key") == cycle_key),
            None,
        )
        if cycle is None:
            raise HomeAssistantError(
                f"Laundry {self.name}: cycle {cycle_key!r} not found on {slug}"
            )
        profile = None
        learned = None
        curve_store = store.get("curve_store")
        if curve_store:
            info = curve_store.get_confidence_info(slug, cycle_key)
            if info["confidence"] >= CURVE_USE_THRESHOLD:
                profile = curve_store.get_profile(slug, cycle_key)
            learned = curve_store.get_avg_duration_minutes(slug, cycle_key)
        duration = duration_override or learned or float(cycle["duration_minutes"])
        kwh = kwh_override if kwh_override is not None else float(cycle.get("total_kwh") or 0.0)
        return PlanStep(
            machine=machine,
            cycle_key=cycle_key,
            name=cycle.get("name", cycle_key),
            duration_minutes=float(duration),
            kwh=kwh,
            confirm_before=confirm_before,
            profile=profile,
            flow_slug=flow_slug,
        )

    def _expand_flow(self, flow: Dict) -> List[PlanStep]:
        steps: List[PlanStep] = []
        for s in flow.get("steps", []):
            steps.append(
                self._make_step(
                    s["machine"],
                    s["cycle_key"],
                    confirm_before=bool(s.get("confirm_before", True)),
                    duration_override=float(s["duration_minutes"]) if s.get("duration_minutes") else None,
                    kwh_override=float(s["kwh"]) if s.get("kwh") else None,
                    flow_slug=flow.get("slug") or _slugify(flow.get("name", "")),
                )
            )
        # "In use overnight" (bed linen): the load can't be put in the night
        # before and delay-started — its first wash must start while you're
        # around. Mid-plan that's already true (it's a changeover); as the
        # plan's first step _shape_fn enforces it.
        if steps and flow.get("overnight"):
            steps[0].attended = True
        return steps

    def _cycle_key_by_name(self, machine: str, name: str) -> Optional[str]:
        device = self.device_config(machine)
        return next(
            (c["cycle_key"] for c in device.get("cycles", []) if c.get("name") == name),
            None,
        )

    def _steps_for_label(self, label: str) -> List[PlanStep]:
        flow = self._flow_by_label(label)
        if flow is not None:
            steps = self._expand_flow(flow)
        else:
            for machine, prefix in (
                (MACHINE_WASH, WASH_LABEL_PREFIX), (MACHINE_DRY, DRY_LABEL_PREFIX),
            ):
                if label.startswith(prefix):
                    key = self._cycle_key_by_name(machine, label[len(prefix):])
                    if key is not None:
                        steps = [self._make_step(machine, key)]
                        break
            else:
                raise HomeAssistantError(f"Laundry {self.name}: unknown load {label!r}")
        return steps

    def _row_loads(self) -> List[Tuple[List[int], str, List[PlanStep]]]:
        """The filled Load rows grouped into physical loads — the units the
        optimiser may reorder: (row indices, label, steps). A row starting
        with a wash opens a load; rows starting with a dry ("Dry: X", a 2×
        dry flow) belong to the load before them, so a manual Wash row + Dry
        row stay together and in order."""
        loads: List[Tuple[List[int], List[str], List[PlanStep]]] = []
        for i, label in enumerate(self._rows):
            if label == ROW_NONE_LABEL:
                continue
            steps = self._steps_for_label(label)
            if not steps:
                continue
            name = label
            for prefix in (WASH_LABEL_PREFIX, DRY_LABEL_PREFIX):
                if label.startswith(prefix) and self._flow_by_label(label) is None:
                    name = label[len(prefix):]
            if loads and steps[0].machine == MACHINE_DRY:
                loads[-1][0].append(i)
                loads[-1][1].append(name)
                loads[-1][2].extend(steps)
            else:
                loads.append(([i], [name], steps))
        out = []
        for rows, names, steps in loads:
            label = " → ".join(names)
            for step in steps:
                step.load = label
            out.append((rows, label, steps))
        return out

    def build_plan_from_rows(self) -> List[PlanStep]:
        """The plan as listed (row order) — Start may reorder it, see _plan_choice."""
        return [s for _, _, steps in self._row_loads() for s in steps]

    # ── Scoring ──────────────────────────────────────────────────────────────

    def _washer_constraints(self, now: datetime, rates) -> Dict:
        device = self.device_config(MACHINE_WASH)
        min_delay = int(device.get("min_delay_minutes", DEFAULT_MIN_DELAY_MINUTES))
        max_delay = int(device.get("max_delay_minutes", DEFAULT_MAX_DELAY_MINUTES) or 0)
        handling  = device.get("delay_handling", DEFAULT_DELAY_HANDLING)
        if handling == DELAY_HANDLING_MANAGED and max_delay <= 0:
            max_delay = (
                max(1, int((rates[-1].end - now).total_seconds() / 60))
                if rates else DEFAULT_MAX_DELAY_MINUTES
            )
        return {
            "min_delay": min_delay,
            "max_delay": max_delay,
            "delay_step": int(device.get("delay_step_minutes", DEFAULT_DELAY_STEP_MINUTES)),
            "anchor": "end" if handling == DELAY_HANDLING_END_TIME else "start",
            "balanced_target": float(
                device.get("balanced_target_rate", DEFAULT_BALANCED_TARGET_RATE)
            ),
        }

    def score_plan(
        self,
        steps: List[PlanStep],
        now: Optional[datetime] = None,
        *,
        min_delay_override: Optional[int] = None,
        cap: Optional[datetime] = None,
        cap_kind: Optional[str] = None,
        use_predictions: bool = False,
        unavailable_until: Optional[datetime] = None,
        rates: Optional[list] = None,
    ) -> Optional[ScoredWindow]:
        """Best composite window for the plan under the current mode/deadline.
        With cap/cap_kind set, searches only the committed envelope (the
        pair-level reschedule path; "deadline" and "wait" caps bound the plan
        END, the legacy "window" cap bounds its start). use_predictions (only
        while a hold is active) extends the horizon with published forecast
        slots, same as the device coordinator. rates overrides the rate list
        outright (offers / suggestions score on the planner's central
        forecast)."""
        now = now or _utcnow()
        coordinator = self.washer_coordinator()
        if coordinator is None:
            return None
        if rates is None:
            rates = coordinator.rates
            if rates and use_predictions:
                rates = coordinator.rates_with_predictions(now)
        if not rates:
            return None

        # Base (no-night) shape seeds candidate generation; profile_fn gives
        # each candidate its real timeline.
        offsets = derive_offsets(steps, self._gap)
        profile, total_dur, _ = build_composite_profile(steps, offsets)
        c = self._washer_constraints(now, rates)
        common = dict(
            duration_minutes=total_dur,
            power_profile=profile,
            delay_step_minutes=c["delay_step"],
            anchor=c["anchor"],
            constraint_duration_minutes=steps[0].duration_minutes,
            profile_fn=self._shape_fn(steps, now, unavailable_until),
        )
        min_delay = min_delay_override if min_delay_override is not None else c["min_delay"]

        if cap_kind in ("deadline", "wait") and cap is not None:
            return find_window_for_deadline(
                rates, now, deadline=cap, min_delay_minutes=min_delay, **common
            )
        if cap is not None:  # "window" cap → bounded max_delay search
            eff_max = int((cap - now).total_seconds() / 60)
            if eff_max < 1:
                return None
            _, best, _ = score_windows(
                rates, now,
                min_delay_minutes=min_delay, max_delay_minutes=eff_max,
                prefer_sooner_diff=0.0, **common,
            )
            return best

        # Fresh scoring — apply the mode policy.
        due = self._due_at
        if due is not None and due > now + timedelta(minutes=total_dur):
            win = find_window_for_deadline(
                rates, now, deadline=due, min_delay_minutes=min_delay, **common
            )
            if win is not None:
                return win

        now_win, best_win, _ = score_windows(
            rates, now,
            min_delay_minutes=min_delay, max_delay_minutes=c["max_delay"],
            prefer_sooner_diff=0.0, **common,
        )
        if self._mode == MODE_NOW:
            return now_win or best_win
        candidate = None
        if self._mode == MODE_BALANCED:
            candidate = find_soonest_below_rate(
                rates, now,
                target_rate=c["balanced_target"],
                min_delay_minutes=min_delay, max_delay_minutes=c["max_delay"],
                **common,
            )
        if candidate is None:
            candidate = best_win
        # "Don't wait for pennies" — skipped while a hold clamp is active
        # (min_delay_override set), same as the device coordinator.
        if (
            candidate is not None and now_win is not None
            and candidate is not now_win and min_delay_override is None
        ):
            saving = now_win.avg_rate_cost_per_kwh - candidate.avg_rate_cost_per_kwh
            if saving < coordinator.prefer_sooner_diff:
                candidate = now_win
        return candidate or now_win

    def _hold_clamp(
        self, now: datetime, coordinator
    ) -> Tuple[Optional[datetime], Optional[int]]:
        """Pair-level hold-for-next-day-rates, same policy as single devices:
        engages only near publication, and never against Mode=Now or a
        deadline (both pin the timing decision).
        Returns (hold_until, min_delay_override)."""
        if (
            not self._hold_for_rates or not self._auto_reschedule
            or self._mode == MODE_NOW or self._due_at is not None
        ):
            return None, None
        hold_until = coordinator.compute_hold_until(now)
        if hold_until is None:
            return None, None
        min_delay_override = max(
            self._washer_constraints(now, coordinator.rates)["min_delay"],
            int(-(-(hold_until - now).total_seconds() // 60)),
        )
        return hold_until, min_delay_override

    # ── Load-order optimiser ─────────────────────────────────────────────────

    def _evaluate(
        self,
        steps: List[PlanStep],
        now: datetime,
        *,
        min_delay_override: Optional[int],
        use_predictions: bool,
        uu: Optional[datetime],
    ) -> Optional[Dict]:
        """One ordering: its window under the mode policy, and the score the
        optimiser compares — cost + time_value × hours until the last load
        finishes. Mode still picks each ordering's start."""
        window = self.score_plan(
            steps, now, min_delay_override=min_delay_override,
            use_predictions=use_predictions, unavailable_until=uu,
        )
        if window is None:
            return None
        _, _, total_dur, total_kwh = self._plan_shape(steps, window.start, uu)
        finish = window.start + timedelta(minutes=total_dur)
        cost = window.avg_rate_cost_per_kwh * total_kwh
        hours = max(0.0, (finish - now).total_seconds() / 3600.0)
        return {
            "window": window, "finish": finish, "cost": cost,
            "score": cost + self._time_value * hours,
        }

    @staticmethod
    def _search_order(n: int, score: Callable[[Tuple[int, ...]], float]) -> Tuple[int, ...]:
        """Best permutation of n loads: exhaustive up to
        LAUNDRY_ORDER_EXHAUSTIVE_MAX, else move-one-load local search from the
        listed order. Keeps the listed order unless the winner is at least
        1p-equivalent better (no reshuffling on rounding noise)."""
        ident = tuple(range(n))
        if n <= LAUNDRY_ORDER_EXHAUSTIVE_MAX:
            best = min(permutations(ident), key=score)  # first minimum = listed on ties
        else:
            best, best_s = ident, score(ident)
            for _ in range(3):
                improved = False
                for i in range(n):
                    for j in range(n):
                        if i == j:
                            continue
                        p = list(best)
                        p.insert(j, p.pop(i))
                        p = tuple(p)
                        if (ps := score(p)) < best_s - 1e-9:
                            best, best_s, improved = p, ps, True
                if not improved:
                    break
        return ident if score(best) > score(ident) - 0.01 else best

    def _plan_choice(self, now: datetime, coordinator) -> Optional[Dict]:
        """Which order to run the selected Loads in, plus drop-a-load
        suggestions. The search scores up to 4! orderings, so it's cached by
        its inputs in 10-minute buckets; callers rescore the chosen order
        fresh. Raises HomeAssistantError for an unknown load label."""
        loads = self._row_loads()
        if not loads:
            return None
        hold, mdo = self._hold_clamp(now, coordinator)
        uu = self._unavailable_until
        key = (
            tuple(self._rows), self._mode, _iso(self._due_at), _iso(uu),
            tuple((a["start"], a.get("end")) for a in self._away), self._order_mode,
            id(coordinator.rates), len(coordinator.rates), hold is not None,
            int(now.timestamp() // 600),
        )
        if key == self._choice_key:
            return self._choice
        memo: Dict[Tuple[str, ...], Optional[Dict]] = {}

        def ev(perm: Tuple[int, ...]) -> Optional[Dict]:
            sig = tuple(loads[i][1] for i in perm)  # identical loads swap for free
            if sig not in memo:
                steps = [s for i in perm for s in loads[i][2]]
                memo[sig] = None if validate_plan(steps) else self._evaluate(
                    steps, now, min_delay_override=mdo,
                    use_predictions=hold is not None, uu=uu,
                )
            return memo[sig]

        def score(perm: Tuple[int, ...]) -> float:
            e = ev(perm)
            return e["score"] if e else float("inf")

        n = len(loads)
        ident = tuple(range(n))
        best = self._search_order(n, score) if n > 1 else ident
        order = best if self._order_mode == LAUNDRY_ORDER_OPTIMISED else ident
        choice = {
            "loads":     loads,
            "order":     order,
            "optimised": best,
            "listed":    ev(ident),
            "best":      ev(best),
        }
        choice["suggestions"] = self._drop_suggestions(loads, order, ev, now, coordinator, uu)
        self._choice_key, self._choice = key, choice
        return choice

    def _drop_suggestions(self, loads, order, ev, now: datetime, coordinator, uu) -> List[Dict]:
        """Loads worth washing another day. If nothing fits (away periods vs
        the wet-wait cap), the loads whose removal makes the plan fit;
        otherwise the load that costs at least OFFER_MIN_SAVING and
        OFFER_MIN_FRACTION more in this plan than on its own after it (same
        bar as offers — money-gated, so a load that merely adds hours doesn't
        nag; the time it adds is reported alongside)."""
        if len(order) < 2:
            return []
        chosen = ev(order)
        if chosen is None:
            out = []
            for k, i in enumerate(order):
                without = ev(order[:k] + order[k + 1:])
                if without is not None:
                    label = loads[i][1]
                    out.append({
                        "rows": loads[i][0], "load": label, "kind": "infeasible",
                        "finish": _iso(without["finish"]),
                        "message": (
                            f"Nothing fits your away times and wet-wait limit with every load — "
                            f"without {label} it's done by {self._fmt_when(without['finish'])}."
                        ),
                    })
            return out
        rates = coordinator.planner_rates(now)
        if not rates:
            return []
        deadline = min(now + timedelta(hours=PLANNER_HORIZON_HOURS), rates[-1].end)
        after = max(0, int((chosen["finish"] - now).total_seconds() // 60) + 1)
        best = None
        for k, i in enumerate(order):
            without = ev(order[:k] + order[k + 1:])
            if without is None:
                continue
            steps_i = loads[i][2]
            alone = self.score_plan(
                steps_i, now, min_delay_override=after, cap=deadline,
                cap_kind="deadline", rates=rates, unavailable_until=uu,
            )
            if alone is None:
                continue
            alone_cost = alone.avg_rate_cost_per_kwh * sum(s.kwh for s in steps_i)
            extra_cost = chosen["cost"] - without["cost"]
            extra_min = max(0.0, (chosen["finish"] - without["finish"]).total_seconds() / 60)
            benefit = extra_cost - alone_cost
            if (
                benefit < OFFER_MIN_SAVING
                or alone_cost > extra_cost * (1 - OFFER_MIN_FRACTION)
                or (best is not None and benefit <= best["benefit"])
            ):
                continue
            label = loads[i][1]
            best = {
                "rows": loads[i][0], "load": label, "kind": "expensive",
                "extra_cost": round(extra_cost, 4), "extra_minutes": round(extra_min),
                "alone_start": _iso(alone.start), "alone_cost": round(alone_cost, 4),
                "benefit": round(benefit, 4),
                "message": (
                    f"{label} adds {self._fmt_money(extra_cost)}"
                    f"{f' and {self._fmt_dur(extra_min)}' if extra_min >= 15 else ''} to this plan — "
                    f"on its own from {self._fmt_when(alone.start)} it'd cost about "
                    f"{self._fmt_money(alone_cost)}. Consider washing it another day."
                ),
            }
        return [best] if best else []

    def _order_info(self, choice: Dict) -> Dict:
        labels = [l[1] for l in choice["loads"]]
        ident = tuple(range(len(labels)))
        info = {
            "mode":      self._order_mode,
            "listed":    labels,
            "order":     [labels[i] for i in choice["order"]],
            "optimised": [labels[i] for i in choice["optimised"]],
            "reordered": choice["order"] != ident,
            "improves":  choice["optimised"] != ident,
        }
        listed, best = choice["listed"], choice["best"]
        if info["improves"] and listed and best:
            info["minutes_sooner"] = round((listed["finish"] - best["finish"]).total_seconds() / 60)
            info["cost_delta"] = round(best["cost"] - listed["cost"], 4)
        return info

    def _refresh_preview(self) -> None:
        """Score the prospective plan for the current row selections so the
        Planned sensors show the window BEFORE Start is pressed. Result is
        cached — snapshot() runs once per entity per update."""
        self._preview = None
        if self._run is not None:
            return
        self._preview = self._compute_preview()

    def windows_for_loads(self, labels: List[str]) -> Optional[Dict]:
        """Best windows for an ad-hoc set of Loads (flow names / single cycles),
        without touching the pair's rows, plan or run. Same scoring as the
        Planned sensors. Raises HomeAssistantError for an unknown load or too
        many loads; returns None when there are no rates. The swap below is
        synchronous (no awaits), so nothing observes the temporary rows."""
        if len(labels) > len(self._rows):
            raise HomeAssistantError(
                f"Laundry {self.name}: at most {len(self._rows)} loads at once"
            )
        for label in labels:
            self._steps_for_label(label)  # raises on an unknown load
        saved = (self._rows, self._choice_key, self._choice)
        try:
            self._rows = list(labels) + [ROW_NONE_LABEL] * (len(self._rows) - len(labels))
            self._choice_key = self._choice = None
            return self._compute_preview(per_window_steps=True)
        finally:
            self._rows, self._choice_key, self._choice = saved

    def _compute_preview(self, per_window_steps: bool = False) -> Optional[Dict]:
        """The scored preview dict for the current rows, or None.
        per_window_steps: attach each best window's own step timetable (the plan
        shape depends on its start, e.g. changeovers pushed out of the sleep window)."""
        coordinator = self.washer_coordinator()
        if coordinator is None or not coordinator.rates:
            return None
        now = _utcnow()
        try:
            choice = self._plan_choice(now, coordinator)
        except HomeAssistantError:
            return None  # stale restored label etc. — blank preview, not an error
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("SmartStart laundry: %s plan search failed: %s", self.slug, exc)
            return None
        if choice is None:
            return None
        steps = [s for i in choice["order"] for s in choice["loads"][i][2]]
        if validate_plan(steps):
            return None
        hold, min_delay_override = self._hold_clamp(now, coordinator)
        uu = self._unavailable_until
        try:
            window = self.score_plan(
                steps, now, min_delay_override=min_delay_override,
                use_predictions=hold is not None,
                unavailable_until=uu,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "SmartStart laundry: %s preview scoring failed: %s", self.slug, exc
            )
            return None
        extras = {"order": self._order_info(choice), "suggestions": choice["suggestions"]}
        if window is None:
            return {"infeasible": True, **extras}
        t0 = window.start
        offsets, _, total_dur, total_kwh = self._plan_shape(steps, t0, uu)
        base_offsets = derive_offsets(steps, self._gap)
        base_profile, base_dur, _ = build_composite_profile(steps, base_offsets)
        c = self._washer_constraints(now, coordinator.rates)
        best = coordinator.best_windows_info(
            now,
            duration_minutes=base_dur,
            kwh=total_kwh,
            profile=base_profile,
            min_delay_minutes=c["min_delay"],
            delay_step_minutes=c["delay_step"],
            anchor=c["anchor"],
            constraint_duration_minutes=steps[0].duration_minutes,
            profile_fn=self._shape_fn(steps, now, uu),
        )
        if per_window_steps:
            for w in best:
                t = datetime.fromisoformat(w["start"])
                w_offsets, _, _, _ = self._plan_shape(steps, t, uu)
                w["steps"] = [
                    {
                        "machine":        s.machine,
                        "name":           s.name,
                        "load":           s.load,
                        "confirm_before": s.confirm_before,
                        "planned_start":  _iso(t + timedelta(minutes=o)),
                        "planned_end":    _iso(t + timedelta(minutes=o + s.duration_minutes)),
                    }
                    for s, o in zip(steps, w_offsets)
                ]
        _, now_profile, now_dur, _ = self._plan_shape(steps, now, uu)
        now_rate = self._now_rate(coordinator.rates, now, now_dur, now_profile, t0)
        return {
            **extras,
            "best_windows":   best,
            "now_rate":       now_rate,
            "planned_start":  _iso(t0),
            "planned_finish": _iso(t0 + timedelta(minutes=total_dur)),
            "plan_rate":      window.avg_rate_cost_per_kwh,
            "plan_cost":      round(window.avg_rate_cost_per_kwh * total_kwh, 4),
            "total_kwh":      total_kwh,
            "steps": [
                {
                    "machine":          s.machine,
                    "name":             s.name,
                    "cycle_key":        s.cycle_key,
                    "state":            "planned",
                    "confirm_before":   s.confirm_before,
                    "flow":             s.flow_slug,
                    "load":             s.load,
                    "planned_start":    _iso(t0 + timedelta(minutes=o)),
                    "planned_end":      _iso(t0 + timedelta(minutes=o + s.duration_minutes)),
                    "duration_minutes": round(s.duration_minutes, 1),
                    "kwh":              round(s.kwh, 3),
                }
                for s, o in zip(steps, offsets)
            ],
        }

    def _window_at(
        self, steps: List[PlanStep], t0: datetime, uu: Optional[datetime], now: datetime
    ) -> ScoredWindow:
        """The plan starting exactly at t0 (a picked window), priced on real
        rates plus the forecast's central estimate."""
        coordinator = self.washer_coordinator()
        rates = coordinator.planner_rates(now) if coordinator else []
        _, profile, total_dur, _ = self._plan_shape(steps, t0, uu)
        end = t0 + timedelta(minutes=total_dur)
        if not rates or end > rates[-1].end:
            raise HomeAssistantError(
                f"Laundry {self.name}: rates don't reach {dt_util.as_local(end):%a %H:%M} yet"
            )
        return ScoredWindow(
            start=t0, end=end,
            avg_rate_cost_per_kwh=_avg_rate(rates, t0, total_dur, profile),
            delay_minutes=max(0, int((t0 - now).total_seconds() / 60)),
            n_slots=1,
        )

    # ── Run lifecycle ────────────────────────────────────────────────────────

    def _run_steps(self) -> List[PlanStep]:
        return [PlanStep.from_dict(d) for d in self._run["steps"]]

    async def async_start(
        self,
        steps: Optional[List[PlanStep]] = None,
        *,
        start_at: Optional[datetime] = None,
    ) -> None:
        """Commit the plan. Without explicit steps, the Load rows are run in
        the optimiser's order (unless Load Order is "As listed"). start_at
        pins the plan's start (a picked window) instead of scoring one."""
        if self._run is not None:
            raise HomeAssistantError(
                f"Laundry {self.name}: a run is already active — cancel it first"
            )
        self._ensure_subscriptions()
        now = _utcnow()
        coordinator = self.washer_coordinator()
        if coordinator is None or not coordinator.rates:
            raise HomeAssistantError(
                f"Laundry {self.name}: no rates loaded — cannot score the plan"
            )
        if steps is None:
            choice = self._plan_choice(now, coordinator)
            steps = (
                [s for i in choice["order"] for s in choice["loads"][i][2]] if choice else []
            )
        problem = validate_plan(steps)
        if problem:
            raise HomeAssistantError(f"Laundry {self.name}: {problem}")

        uu = self._unavailable_until
        if start_at is not None:
            # A picked window is explicit intent — no hold clamp on it.
            hold_until = None
            window = self._window_at(steps, max(start_at, now), uu, now)
        else:
            hold_until, min_delay_override = self._hold_clamp(now, coordinator)
            window = self.score_plan(
                steps, now, min_delay_override=min_delay_override,
                use_predictions=hold_until is not None,
                unavailable_until=uu,
            )
        if window is None:
            raise HomeAssistantError(
                f"Laundry {self.name}: no viable window found for the plan"
            )

        t0 = window.start
        offsets, _, total_dur, total_kwh = self._plan_shape(steps, t0, uu)
        plan_rate = window.avg_rate_cost_per_kwh
        plan_cost = round(plan_rate * total_kwh, 4)
        _, now_profile, now_dur, _ = self._plan_shape(steps, now, uu)
        now_rate = self._now_rate(coordinator.rates, now, now_dur, now_profile, t0)

        # Reschedule envelope, committed once: due-by deadline if set, else
        # the plan must finish within max_wait_hours of this press (fixed, so
        # repeated reschedules can't creep). A picked window past it widens it.
        if self._due_at is not None:
            cap, cap_kind = self._due_at, "deadline"
        else:
            cap, cap_kind = now + timedelta(hours=self._max_wait_hours), "wait"
        cap = max(cap, t0 + timedelta(minutes=total_dur)) if start_at is not None else cap

        self._run = {
            "id":            uuid.uuid4().hex,
            "status":        LAUNDRY_STATE_SCHEDULED,
            "t0":            _iso(t0),
            "committed_at":  _iso(now),
            "steps":         [s.as_dict() for s in steps],
            "states":        [_STEP_PENDING] * len(steps),
            "actual_start":  [None] * len(steps),
            "actual_end":    [None] * len(steps),
            "offsets":       offsets,
            "plan_rate":     plan_rate,
            "plan_cost":     plan_cost,
            # Rate the plan would have paid starting at commit time — the
            # savings baseline (kept fixed through reschedules).
            "now_rate":      now_rate,
            "total_kwh":     total_kwh,
            "total_duration": total_dur,
            "cap":           _iso(cap),
            "cap_kind":      cap_kind,
            "hold_until":    _iso(hold_until),
            "unavailable_until": _iso(uu),
            # Committed on a forecast slot — re-baselined against actuals once
            # real rates cover the plan (see _maybe_reschedule).
            "rate_predicted": coordinator.is_predicted_window(window),
            "mode":          self._mode,
        }

        # Command the washer for step 1 through its own scheduler.
        try:
            await self._commit_first_step(window, steps, reschedule=False)
        except Exception:
            self._run = None  # nothing was committed — don't strand a phantom run
            raise
        self._run["states"][0] = _STEP_FIRED
        self._preview = None  # the run's own figures take over

        # Deadline / changeover override are per-run intent — clear after commit.
        self._due_at = None
        self._unavailable_until = None
        await self._save()
        self._fire_event("smartstart_laundry_scheduled", {
            "planned_start":  _iso(t0),
            "planned_finish": _iso(t0 + timedelta(minutes=total_dur)),
            "plan_rate_cost_per_kwh": round(plan_rate, 4),
            "estimated_cost": plan_cost,
            "total_kwh":      round(total_kwh, 3),
            "steps": [
                {**s.as_dict(), "planned_start": _iso(t0 + timedelta(minutes=o))}
                for s, o in zip(steps, offsets)
            ],
        })
        self._notify()
        _LOGGER.info(
            "SmartStart laundry: %s scheduled %d-step plan — t0=%s rate=%.5f",
            self.slug, len(steps), t0.strftime("%H:%M"), plan_rate,
        )

    async def _commit_first_step(
        self, window: ScoredWindow, steps: List[PlanStep], *, reschedule: bool
    ) -> None:
        """Schedule the first step on its device with the plan's chosen start.
        The device window covers the FIRST STEP only — the appliance is told
        about its own cycle; the composite plan is the manager's business."""
        first = steps[0]
        slug = self._slug_for(first.machine)
        store, device = self._resolve(slug)
        scheduler = store.get("scheduler")
        coordinator = store.get("coordinator")
        if scheduler is None or not device:
            raise HomeAssistantError(
                f"Laundry {self.name}: device {slug!r} is not loaded"
            )
        wash_window = self._device_window(window.start, first, coordinator)

        if reschedule:
            await scheduler.async_reschedule(slug, wash_window)
            return

        cycle = next(
            (c for c in device.get("cycles", []) if c.get("cycle_key") == first.cycle_key), {}
        )
        payload = self._step_payload(first, cycle, wash_window, reschedule=False)
        now_rate = coordinator.rate_at(_utcnow()) if coordinator else None
        await scheduler.async_schedule(
            device_slug=slug,
            cycle_id=first.cycle_key,
            window=wash_window,
            cycle_kwh=first.kwh,
            now_rate=now_rate,
            start_service=device["start_service"],
            service_payload=payload,
            reschedule_deadline=_from_iso(self._run["cap"]) if self._run.get("cap_kind") == "deadline" else None,
            hold_until=_from_iso(self._run.get("hold_until")),
            laundry_run=True,
            laundry=self._laundry_meta(0),
        )

    def _laundry_meta(self, idx: int) -> Dict:
        step = self._run["steps"][idx]
        return {
            "run_id": self._run["id"],
            "pair":   self.slug,
            "step":   idx,
            "flow":   step.get("flow_slug"),
        }

    def _device_window(self, start: datetime, step: PlanStep, coordinator) -> ScoredWindow:
        end = start + timedelta(minutes=step.duration_minutes)
        rate = None
        if coordinator is not None and coordinator.rates:
            rate = _time_weighted_rate(coordinator.rates, start, step.duration_minutes)
        if rate is None:
            rate = self._run.get("plan_rate", 0.0) if self._run else 0.0
        delay = max(0, int((start - _utcnow()).total_seconds() / 60))
        return ScoredWindow(
            start=start, end=end, avg_rate_cost_per_kwh=rate,
            delay_minutes=delay, n_slots=1,
        )

    @staticmethod
    def _step_payload(step: PlanStep, cycle: Dict, window: ScoredWindow, *, reschedule: bool) -> Dict:
        return {
            "delay_seconds":            window.delay_minutes * 60,
            "cycle_id":                 cycle.get("cycle_id") or cycle.get("name", step.name),
            "start_time":               window.start.isoformat(),
            "cycle_duration":           cycle.get("duration_minutes"),
            "estimated_cycle_duration": step.duration_minutes,
            "rate_cost_per_kwh":        window.avg_rate_cost_per_kwh,
            "estimated_finish":         window.end.isoformat(),
            "reschedule":               reschedule,
        }

    async def _fire_step(self, idx: int, start_at: Optional[datetime] = None) -> None:
        """Start a step on its device — now (a changeover just happened, or
        the step auto-fires), or loaded now and delay-started at start_at
        (Continue found a cheaper slot for what's left)."""
        steps = self._run_steps()
        step = steps[idx]
        slug = self._slug_for(step.machine)
        store, device = self._resolve(slug)
        scheduler = store.get("scheduler")
        coordinator = store.get("coordinator")
        if scheduler is None or not device:
            _LOGGER.error(
                "SmartStart laundry: %s cannot fire step %d — device %s not loaded",
                self.slug, idx + 1, slug,
            )
            return
        now = _utcnow()
        window = self._device_window(start_at or now, step, coordinator)
        cycle = next(
            (c for c in device.get("cycles", []) if c.get("cycle_key") == step.cycle_key), {}
        )
        payload = self._step_payload(step, cycle, window, reschedule=False)
        await scheduler.async_schedule(
            device_slug=slug,
            cycle_id=step.cycle_key,
            window=window,
            cycle_kwh=step.kwh,
            now_rate=coordinator.rate_at(_utcnow()) if coordinator else None,
            start_service=device["start_service"],
            service_payload=payload,
            laundry_run=True,
            laundry=self._laundry_meta(idx),
        )
        self._run["states"][idx] = _STEP_FIRED
        self._run_list("fired_at")[idx] = _iso(now)
        self._run_list("start_at")[idx] = _iso(start_at) if start_at else None
        _LOGGER.info(
            "SmartStart laundry: %s fired step %d/%d (%s on %s)%s",
            self.slug, idx + 1, len(steps), step.name, slug,
            f" — delayed to {dt_util.as_local(start_at):%H:%M}" if start_at else "",
        )

    def _run_list(self, key: str) -> List:
        """Per-step list in the run dict, created for runs saved before it existed."""
        n = len(self._run["steps"])
        if len(self._run.get(key) or []) != n:
            self._run[key] = [None] * n
        return self._run[key]

    async def async_continue(self, *, start_now: bool = False) -> None:
        """The user confirms a changeover trip: fire everything unblocked.
        If waiting saves enough on what's left (see _continue_option), the
        machines are loaded now and delay-started at the cheaper slot —
        start_now (or Mode Now) skips that."""
        if self._run is None:
            raise HomeAssistantError(f"Laundry {self.name}: no active run")
        steps = self._run_steps()
        done   = [st == _STEP_DONE for st in self._run["states"]]
        active = [st in (_STEP_FIRED, _STEP_RUNNING) for st in self._run["states"]]
        ready = unblocked_steps(steps, done, active)
        if not ready:
            _LOGGER.debug("SmartStart laundry: %s Continue pressed, nothing ready", self.slug)
            return
        option = None if start_now else self._continue_option(_utcnow())
        start_at = _from_iso(option["start_at"]) if option else None
        for i in ready:
            await self._fire_step(i, start_at=start_at)
        self._run["status"] = LAUNDRY_STATE_RUNNING
        self._run["continue_delay"] = option
        await self._save()
        self._notify()

    async def async_start_now(self) -> None:
        """Override a Continue delay: start the loaded, delay-started steps now."""
        if self._run is None:
            raise HomeAssistantError(f"Laundry {self.name}: no active run")
        now = _utcnow()
        steps = self._run_steps()
        moved = False
        for i, st in enumerate(self._run["states"]):
            at = _from_iso(self._run_list("start_at")[i])
            if st != _STEP_FIRED or at is None or at <= now:
                continue
            slug = self._slug_for(steps[i].machine)
            store, _device = self._resolve(slug)
            scheduler = store.get("scheduler")
            if scheduler is None:
                continue
            await scheduler.async_reschedule(
                slug, self._device_window(now, steps[i], store.get("coordinator"))
            )
            self._run_list("start_at")[i] = None
            moved = True
        if not moved:
            raise HomeAssistantError(f"Laundry {self.name}: nothing is waiting to start")
        self._run["continue_delay"] = None
        await self._save()
        self._notify()

    def _continue_option(self, now: datetime) -> Optional[Dict]:
        """Rescore what's left at a changeover: run the ready steps now, or
        load them now and delay-start them on a CONTINUE_STEP_MINUTES grid
        (within each machine's delay limits, the wet-wait cap and real
        rates). Returned only when waiting saves ≥ OFFER_MIN_SAVING AND still
        wins on cost + time (the optimiser's blend) — needs auto-reschedule
        on, never under Mode Now."""
        run = self._run
        coordinator = self.washer_coordinator()
        if (
            run is None or not self._auto_reschedule or run.get("mode") == MODE_NOW
            or coordinator is None or not coordinator.rates
        ):
            return None
        steps = self._run_steps()
        states = run["states"]
        done   = [st == _STEP_DONE for st in states]
        active = [st in (_STEP_FIRED, _STEP_RUNNING) for st in states]
        ready = unblocked_steps(steps, done, active)
        if not ready:
            return None
        rates = coordinator.rates
        times = self._projected_times()
        pending = [i for i, st in enumerate(states) if st == _STEP_PENDING]
        sub = [
            PlanStep(**{**steps[i].__dict__, "confirm_before": False}) if i in ready else steps[i]
            for i in pending
        ]
        # Latest wash end before each ready dry — the wet-wait clock.
        wet_since: List[datetime] = []
        for i in ready:
            if steps[i].machine != MACHINE_DRY:
                continue
            ends = [times[j][1] for j in range(i) if steps[j].machine == MACHINE_WASH]
            if ends:
                wet_since.append(max(ends))

        def evaluate(t: datetime) -> Optional[Dict]:
            rel = lambda dt: (dt - t).total_seconds() / 60.0  # noqa: E731
            washer_free = max([0.0] + [
                rel(times[j][1]) for j in range(len(steps))
                if active[j] and steps[j].machine == MACHINE_WASH
            ])
            dryer_free = max([0.0] + [
                rel(times[j][1]) for j in range(len(steps))
                if active[j] and steps[j].machine == MACHINE_DRY
            ])
            prior = [rel(times[j][1]) for j in range(len(steps))
                     if (done[j] or active[j]) and steps[j].machine == MACHINE_WASH]

            def changeover_at(off: float) -> float:
                return rel(self._available_from(t + timedelta(minutes=off), None))
            offsets = derive_offsets(
                sub, self._gap, washer_free=washer_free, dryer_free=dryer_free,
                prior_wash_ends=prior, first_step_immediate=False,
                changeover_at=changeover_at,
            )
            profile, dur, kwh = build_composite_profile(sub, offsets)
            if t != now and t + timedelta(minutes=dur) > rates[-1].end:
                return None
            cost = _avg_rate(rates, t, dur, profile) * kwh
            finish = t + timedelta(minutes=dur)
            return {
                "start": t, "cost": cost, "finish": finish,
                "score": cost + self._time_value * (finish - now).total_seconds() / 3600.0,
            }

        base = evaluate(now)
        if base is None:
            return None
        limits = []
        for i in ready:
            dev = self.device_config(steps[i].machine)
            limits.append((
                int(dev.get("min_delay_minutes", DEFAULT_MIN_DELAY_MINUTES) or 0),
                int(dev.get("max_delay_minutes", DEFAULT_MAX_DELAY_MINUTES) or 0),
            ))
        best = None
        for k in range(1, CONTINUE_HORIZON_HOURS * 60 // CONTINUE_STEP_MINUTES + 1):
            delay = k * CONTINUE_STEP_MINUTES
            t = now + timedelta(minutes=delay)
            if any(delay < lo or (hi and delay > hi) for lo, hi in limits):
                continue
            if self._wet_cap_minutes > 0 and any(
                (t - since).total_seconds() / 60 > self._wet_cap_minutes + self._gap
                for since in wet_since
            ):
                break  # only gets worse from here
            if any(steps[i].attended for i in ready) and self._available_from(t, None) != t:
                continue  # in-use-overnight load: must start while you're around
            cand = evaluate(t)
            if cand is not None and (best is None or cand["score"] < best["score"]):
                best = cand
        if (
            best is None or base["cost"] - best["cost"] < OFFER_MIN_SAVING
            or best["score"] >= base["score"]
        ):
            return None
        saving = base["cost"] - best["cost"]
        names = ", ".join(f"{steps[i].load or steps[i].name}" for i in ready)
        return {
            "start_at": _iso(best["start"]),
            "saving":   round(saving, 4),
            "finish":   _iso(best["finish"]),
            "finish_now": _iso(base["finish"]),
            "message": (
                f"Load now ({names}); it starts at {dt_util.as_local(best['start']):%H:%M} "
                f"to save ~{self._fmt_money(saving)} on what's left."
            ),
        }

    async def async_cancel(self) -> None:
        if self._run is None:
            return
        steps = self._run_steps()
        # Clear device-side schedules for steps that were commanded but not
        # yet running (running cycles just finish as normal device cycles).
        for i, st in enumerate(self._run["states"]):
            if st != _STEP_FIRED:
                continue
            slug = self._slug_for(steps[i].machine)
            store, _device = self._resolve(slug)
            scheduler = store.get("scheduler")
            if scheduler and scheduler.get_schedule(slug).get("status") == STATE_SCHEDULED:
                # Route through smartstart.stop so the user's stop_service
                # fires too (e.g. cancel an appliance-side reservation).
                await self._hass.services.async_call(
                    DOMAIN, "stop", {"device_slug": slug}, blocking=True
                )
        self._fire_event("smartstart_laundry_cancelled", {
            "completed_steps": sum(1 for st in self._run["states"] if st == _STEP_DONE),
            "total_steps":     len(steps),
        })
        self._run = None
        self._offer = self._offer_window = None
        await self._save()
        self._refresh_preview()  # selections are kept — preview them again
        self._notify()
        _LOGGER.info("SmartStart laundry: %s run cancelled", self.slug)

    # ── Device reconciliation (step transitions) ─────────────────────────────

    async def _reconcile_all(self) -> None:
        for slug in (self._washer_slug, self._dryer_slug):
            await self._reconcile_device(slug)

    async def _reconcile_device(self, slug: str) -> None:
        if self._run is None:
            return
        machine = MACHINE_WASH if slug == self._washer_slug else MACHINE_DRY
        steps = self._run_steps()
        idx = next(
            (
                i for i, (s, st) in enumerate(zip(steps, self._run["states"]))
                if s.machine == machine and st in (_STEP_FIRED, _STEP_RUNNING)
            ),
            None,
        )
        if idx is None:
            return
        store, _device = self._resolve(slug)
        scheduler = store.get("scheduler")
        if scheduler is None:
            return
        dev_status = scheduler.get_schedule(slug).get("status", STATE_IDLE)
        state = self._run["states"][idx]

        if dev_status in (STATE_RUNNING, STATE_PAUSED):
            if state == _STEP_FIRED:
                self._run["states"][idx] = _STEP_RUNNING
                self._run["actual_start"][idx] = _iso(_utcnow())
                if self._run["status"] == LAUNDRY_STATE_SCHEDULED:
                    self._run["status"] = LAUNDRY_STATE_RUNNING
                await self._save()
                await self._changeover_while_running()
                self._notify()
        elif dev_status == STATE_IDLE and state == _STEP_RUNNING:
            self._run["states"][idx] = _STEP_DONE
            self._run["actual_end"][idx] = _iso(_utcnow())
            await self._advance(finished_idx=idx)

    async def _advance(self, finished_idx: int) -> None:
        """A step just finished: auto-fire what can fire, work out whether a
        human trip is now needed, or wrap the run up."""
        steps = self._run_steps()
        states = self._run["states"]
        done   = [st == _STEP_DONE for st in states]
        active = [st in (_STEP_FIRED, _STEP_RUNNING) for st in states]

        if all(done):
            t0 = _from_iso(self._run["t0"])
            self._fire_event("smartstart_laundry_completed", {
                "started_at":  self._run["t0"],
                "finished_at": _iso(_utcnow()),
                "total_steps": len(steps),
                "plan_rate_cost_per_kwh": round(self._run.get("plan_rate") or 0.0, 4),
                "estimated_cost":         self._run.get("plan_cost"),
            })
            finished = _iso(_utcnow())
            self._record_load(steps, finished)
            for slug in {s.flow_slug for s in steps if s.flow_slug}:
                self._last_done[slug] = finished
                self._autofilled.pop(slug, None)
            self._run = None
            self._offer = self._offer_window = None
            self._reset_rows_after_run()
            await self._save()
            self._refresh_preview()
            self._notify()
            _LOGGER.info("SmartStart laundry: %s run complete", self.slug)
            return

        confirm_ready = await self._settle_ready()
        await self._save()
        self._fire_event("smartstart_laundry_step_done", {
            "step_index":   finished_idx + 1,
            "step_name":    steps[finished_idx].name,
            "machine":      steps[finished_idx].machine,
            "total_steps":  len(steps),
            "awaiting_changeover": bool(confirm_ready),
            "next_action":  self.next_action(),
            "continue_hint": (self._continue_opt or {}).get("message"),
        })
        self._notify()

    async def _settle_ready(self) -> List[int]:
        """Auto-fire unblocked steps that need no trip, then park the run at
        AWAITING if a confirm-before step can start (e.g. the free machine
        while the other one runs). Returns those confirm-ready steps."""
        steps = self._run_steps()
        states = self._run["states"]
        done   = [st == _STEP_DONE for st in states]
        active = [st in (_STEP_FIRED, _STEP_RUNNING) for st in states]
        ready = unblocked_steps(steps, done, active)
        auto_fired = []
        for i in ready:
            if not steps[i].confirm_before:
                await self._fire_step(i)
                auto_fired.append(i)
        if auto_fired:
            done   = [st == _STEP_DONE for st in states]
            active = [st in (_STEP_FIRED, _STEP_RUNNING) for st in states]
            ready = unblocked_steps(steps, done, active)

        confirm_ready = [i for i in ready if steps[i].confirm_before]
        self._run["status"] = (
            LAUNDRY_STATE_AWAITING if confirm_ready else LAUNDRY_STATE_RUNNING
        )
        self._continue_opt = self._continue_option(_utcnow()) if confirm_ready else None
        return confirm_ready

    async def _changeover_while_running(self) -> None:
        """A step just started (or a pre-fix run was restored): the other
        machine may already be free for a confirm-before step. Without this
        the trip was only offered when a step FINISHED, so a wash queued
        behind a running dry sat idle until the dryer was done."""
        if self._run is None or self._run["status"] != LAUNDRY_STATE_RUNNING:
            return
        if not await self._settle_ready():
            return
        await self._save()
        self._fire_event("smartstart_laundry_changeover_due", {
            "next_action":   self.next_action(),
            "awaiting_machines": self.awaiting_machines(),
            "continue_hint": (self._continue_opt or {}).get("message"),
        })
        self._notify()

    # ── Pair-level auto-reschedule ───────────────────────────────────────────

    @callback
    def _on_rates_update(self) -> None:
        if self._unloaded:
            return
        if self._run is None:
            # New rates can move the previewed window — rescore it live.
            self._refresh_preview()
            self._notify()
            return
        if (
            not self._auto_reschedule or self._resched_inflight
            or self._run.get("status") != LAUNDRY_STATE_SCHEDULED
        ):
            return
        self._resched_inflight = True

        async def _do() -> None:
            try:
                await self._maybe_reschedule()
            finally:
                self._resched_inflight = False

        self._hass.async_create_task(_do())

    async def _maybe_reschedule(self) -> None:
        run = self._run
        if run is None or run.get("status") != LAUNDRY_STATE_SCHEDULED:
            return
        now = _utcnow()
        t0 = _from_iso(run["t0"])
        if t0 is None or t0 <= now:
            return
        coordinator = self.washer_coordinator()
        if coordinator is None or not coordinator.rates:
            return
        steps = self._run_steps()
        uu = _from_iso(run.get("unavailable_until"))

        # Hold clamp: a held run must not move back before publication while
        # tomorrow's rates are still pending.
        min_delay_override = None
        hold_dt = _from_iso(run.get("hold_until"))
        if hold_dt and now < hold_dt and coordinator._next_day_rates_missing(now):
            min_delay_override = int(-(-(hold_dt - now).total_seconds() // 60))

        # Committed on a forecast: once real rates cover the plan, judge
        # candidates against its real cost, not the prediction — an
        # optimistic forecast must not block a genuinely cheaper real window.
        if run.get("rate_predicted"):
            _, profile, total_dur, _ = self._plan_shape(steps, t0, uu)
            if t0 + timedelta(minutes=total_dur) <= coordinator.rates[-1].end:
                real = _avg_rate(coordinator.rates, t0, total_dur, profile)
                if real is not None:
                    run["plan_rate"] = real
                    run["plan_cost"] = round(
                        real * float(run.get("total_kwh") or 0.0), 4
                    )
                    run["rate_predicted"] = False
                    await self._save()
                    self._notify()

        candidate = self.score_plan(
            steps, now,
            min_delay_override=min_delay_override,
            cap=_from_iso(run.get("cap")),
            cap_kind=run.get("cap_kind"),
            use_predictions=min_delay_override is not None,
            unavailable_until=uu,
        )
        if candidate is None:
            return
        if abs((candidate.start - t0).total_seconds()) < 60:
            return
        baseline = float(run.get("plan_rate") or 0.0)
        if candidate.avg_rate_cost_per_kwh > baseline - RESCHEDULE_RATE_EPSILON:
            return

        _LOGGER.info(
            "SmartStart laundry: %s auto-rescheduling plan — %s → %s (%.5f → %.5f)",
            self.slug, t0.strftime("%H:%M"), candidate.start.strftime("%H:%M"),
            baseline, candidate.avg_rate_cost_per_kwh,
        )
        await self._move_run(candidate, explicit=False)

    async def _move_run(self, window: ScoredWindow, *, explicit: bool) -> None:
        """Move the scheduled plan to window (auto-reschedule, an accepted
        offer, or a picked window). An explicit move is the user's call: it
        widens the envelope to cover it and drops the hold."""
        run = self._run
        steps = self._run_steps()
        uu = _from_iso(run.get("unavailable_until"))
        coordinator = self.washer_coordinator()
        await self._commit_first_step(window, steps, reschedule=True)
        run["t0"] = _iso(window.start)
        run["rate_predicted"] = bool(coordinator and coordinator.is_predicted_window(window))
        # The timeline moves with t0 (changeovers vs the sleep window).
        offsets, _, total_dur, _ = self._plan_shape(steps, window.start, uu)
        run["offsets"] = offsets
        run["total_duration"] = total_dur
        run["plan_rate"] = window.avg_rate_cost_per_kwh
        run["plan_cost"] = round(
            window.avg_rate_cost_per_kwh * float(run.get("total_kwh") or 0.0), 4
        )
        if explicit:
            end = window.start + timedelta(minutes=total_dur)
            cap = _from_iso(run.get("cap"))
            if run.get("cap_kind") == "window":  # pre-wait-limit run: start cap → end cap
                run["cap_kind"] = "wait"
                cap = None
            run["cap"] = _iso(max(cap, end) if cap else end)
            run["hold_until"] = None
        self._offer = self._offer_window = None
        await self._save()
        self._notify()

    async def async_schedule_at(self, start_at: datetime) -> None:
        """Schedule the plan at a picked window — Start when idle, a move
        when already scheduled."""
        if self._run is None:
            await self.async_start(start_at=start_at)
            return
        if self._run.get("status") != LAUNDRY_STATE_SCHEDULED:
            raise HomeAssistantError(f"Laundry {self.name}: the run has already started")
        now = _utcnow()
        window = self._window_at(
            self._run_steps(), max(start_at, now),
            _from_iso(self._run.get("unavailable_until")), now,
        )
        await self._move_run(window, explicit=True)

    # ── Offers ───────────────────────────────────────────────────────────────

    def _update_offer(self, now: datetime) -> None:
        """Same rules as appliance offers: the cheapest plan window finishing
        within max_wait_hours of the Start press, over real rates plus the
        forecast's central estimate, ≥ OFFER_MIN_SAVING and OFFER_MIN_FRACTION
        cheaper than the committed plan, and not one auto-reschedule would
        take by itself. Announced via smartstart_laundry_offer_found."""
        self._offer = self._offer_window = None
        run = self._run
        coordinator = self.washer_coordinator()
        if run is None or run.get("status") != LAUNDRY_STATE_SCHEDULED or coordinator is None:
            return
        t0, pressed = _from_iso(run.get("t0")), _from_iso(run.get("committed_at"))
        if t0 is None or pressed is None or t0 <= now:
            return
        rates = coordinator.planner_rates(now)
        if not rates:
            return
        steps = self._run_steps()
        uu = _from_iso(run.get("unavailable_until"))
        horizon_end = pressed + timedelta(hours=self._max_wait_hours)
        try:
            win = self.score_plan(
                steps, now, cap=min(horizon_end, rates[-1].end), cap_kind="deadline",
                rates=rates, unavailable_until=uu,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("SmartStart laundry: %s offer scoring failed: %s", self.slug, exc)
            return
        if win is None:
            return
        baseline = float(run.get("plan_rate") or 0.0)
        kwh = float(run.get("total_kwh") or 0.0)
        saving = (baseline - win.avg_rate_cost_per_kwh) * kwh
        predicted = bool(coordinator.is_predicted_window(win))
        cap = _from_iso(run.get("cap"))
        reachable = self._auto_reschedule and not predicted and (
            cap is None
            or (win.start <= cap if run.get("cap_kind") == "window" else win.end <= cap)
        )
        if (
            abs((win.start - t0).total_seconds()) < 60
            or saving < OFFER_MIN_SAVING
            or (baseline > 0 and win.avg_rate_cost_per_kwh > baseline * (1 - OFFER_MIN_FRACTION))
            or reachable
        ):
            return
        _, profile, _, _ = self._plan_shape(steps, win.start, uu)
        low, high = window_band(rates, win, profile)
        offer = {
            "start":         _iso(win.start),
            "end":           _iso(win.end),
            "rate":          round(win.avg_rate_cost_per_kwh, 4),
            "rate_low":      round(low, 4),
            "rate_high":     round(high, 4),
            "cost":          round(win.avg_rate_cost_per_kwh * kwh, 3),
            "saving":        round(saving, 3),
            "saving_pct":    round((1 - win.avg_rate_cost_per_kwh / baseline) * 100) if baseline > 0 else None,
            "predicted":     predicted,
            "planned_start": _iso(t0),
            "planned_rate":  round(baseline, 4),
            "horizon_end":   _iso(horizon_end),
        }
        offer["message"] = (
            f"{self.name}: {'the forecast suggests' if predicted else 'found'} "
            f"~{self._fmt_money(saving)} cheaper if you wait until "
            f"{self._fmt_when(win.start)} instead of the planned {self._fmt_when(t0)}."
        )
        self._offer, self._offer_window = offer, win

        prev = run.get("offer_announced") or {}
        prev_start = _from_iso(prev.get("start"))
        if (
            prev_start is None
            or (prev.get("predicted") and not predicted)  # forecast offer now confirmed
            or abs((win.start - prev_start).total_seconds()) >= OFFER_REFIRE_SHIFT_MIN * 60
        ):
            run["offer_announced"] = {"start": _iso(win.start), "predicted": predicted}
            self._hass.async_create_task(self._save())
            self._fire_event("smartstart_laundry_offer_found", {
                "kind": "forecast" if predicted else "confirmed", **offer,
            })
            _LOGGER.info("SmartStart laundry: offer for %s — %s", self.slug, offer["message"])

    @property
    def offer(self) -> Optional[Dict]:
        return self._offer

    # ── Stalled steps / leaving heads-up ─────────────────────────────────────

    def _stalled(self, now: datetime) -> List[Dict]:
        """Fired steps whose device still isn't running STALL_AFTER_MINUTES
        after they should have started."""
        run = self._run
        if run is None:
            return []
        steps = self._run_steps()
        out = []
        for i, st in enumerate(run["states"]):
            if st != _STEP_FIRED:
                continue
            due = (
                _from_iso(self._run_list("start_at")[i])
                or (_from_iso(run.get("t0")) if i == 0 and run.get("status") == LAUNDRY_STATE_SCHEDULED else None)
                or _from_iso(self._run_list("fired_at")[i])
                or (_from_iso(run.get("t0")) if i == 0 else None)
            )
            if due is not None and now > due + timedelta(minutes=STALL_AFTER_MINUTES):
                out.append({
                    "step": i, "name": steps[i].name, "load": steps[i].load,
                    "machine": steps[i].machine, "due": _iso(due),
                })
        return out

    def _check_stalled(self, now: datetime) -> None:
        announced = set(self._run.get("stall_announced") or [])
        for st in self._stalled(now):
            if st["step"] in announced:
                continue
            announced.add(st["step"])
            machine = "washer" if st["machine"] == MACHINE_WASH else "dryer"
            self._fire_event("smartstart_laundry_step_stalled", {
                **st,
                "message": (
                    f"The {machine} should have started {st['name']} at "
                    f"{dt_util.as_local(_from_iso(st['due'])):%H:%M} but isn't running — "
                    f"check it's on, the door's shut and remote start is enabled."
                ),
            })
            _LOGGER.warning("SmartStart laundry: %s step %d (%s) stalled", self.slug, st["step"] + 1, st["name"])
        if announced != set(self._run.get("stall_announced") or []):
            self._run["stall_announced"] = sorted(announced)
            self._hass.async_create_task(self._save())

    def _check_leaving(self, now: datetime) -> None:
        """LEAVING_NOTICE_MINUTES before an away period, say which changeover
        is the last one before you go and what's next once you're back."""
        changed = False
        for a, (start, end) in zip(self._away, self._away_spans):
            if a.get("announced") or a.get("kind") == "until":
                continue
            if not (now < start <= now + timedelta(minutes=LEAVING_NOTICE_MINUTES)):
                continue
            a["announced"] = True
            changed = True
            steps = self._run_steps()
            times = self._projected_times()
            trips: Dict[str, List[str]] = {}
            for i, st in enumerate(self._run["states"]):
                if st == _STEP_PENDING and steps[i].confirm_before:
                    trips.setdefault(_iso(times[i][0]), []).append(
                        f"{steps[i].load or steps[i].name} ({steps[i].name})"
                    )
            before = sorted(t for t in trips if _from_iso(t) < start)
            after = sorted(t for t in trips if _from_iso(t) >= end)
            if not trips:
                continue
            hm = lambda iso: f"{dt_util.as_local(_from_iso(iso)):%H:%M}"  # noqa: E731
            parts = [f"Leaving at {hm(_iso(start))}."]
            if before:
                parts.append(f"Last changeover before you go: {hm(before[-1])} — {', '.join(trips[before[-1]])}.")
            else:
                parts.append("No changeover due before you go.")
            if after:
                parts.append(f"Next once you're back: {hm(after[0])} — {', '.join(trips[after[0]])}.")
            self._fire_event("smartstart_laundry_leaving", {
                "away_start": _iso(start), "away_end": _iso(end),
                "last_before": before[-1] if before else None,
                "next_after": after[0] if after else None,
                "message": " ".join(parts),
            })
        if changed:
            self._hass.async_create_task(self._save())

    async def async_accept_offer(self) -> None:
        win = self._offer_window
        if win is None or self._run is None:
            raise HomeAssistantError(f"Laundry {self.name}: no offer to accept")
        if self._run.get("status") != LAUNDRY_STATE_SCHEDULED:
            self._offer = self._offer_window = None
            raise HomeAssistantError(f"Laundry {self.name}: the run has already started")
        if win.start <= _utcnow():
            self._offer = self._offer_window = None
            raise HomeAssistantError(f"Laundry {self.name}: that offer has expired")
        await self._move_run(win, explicit=True)

    # ── Projections / snapshot for entities ──────────────────────────────────

    def _projected_times(self) -> List[Tuple[datetime, datetime]]:
        """Per-step (start, end): actuals where known, queue-rule projection
        for the rest — so a slow changeover honestly pushes the finish out."""
        run = self._run
        steps = self._run_steps()
        t0 = _from_iso(run["t0"]) or _utcnow()
        now_off = (_utcnow() - t0).total_seconds() / 60.0
        uu = _from_iso(run.get("unavailable_until"))

        washer_free = 0.0
        dryer_free  = 0.0
        wash_ends: List[float] = []
        out: List[Tuple[datetime, datetime]] = []
        for i, step in enumerate(steps):
            st = run["states"][i]
            a_start = _from_iso(run["actual_start"][i])
            a_end   = _from_iso(run["actual_end"][i])
            if st == _STEP_DONE and a_end is not None:
                s_off = ((a_start or t0) - t0).total_seconds() / 60.0
                e_off = (a_end - t0).total_seconds() / 60.0
            elif st in (_STEP_RUNNING, _STEP_FIRED):
                delayed = _from_iso((run.get("start_at") or [None] * len(steps))[i])
                if a_start is not None:
                    s_off = (a_start - t0).total_seconds() / 60.0
                elif i == 0 and run["status"] == LAUNDRY_STATE_SCHEDULED:
                    s_off = 0.0
                elif delayed is not None:
                    s_off = max((delayed - t0).total_seconds() / 60.0, now_off)
                else:
                    s_off = max(now_off, 0.0)
                e_off = max(s_off + step.duration_minutes, now_off if st == _STEP_RUNNING else 0.0)
            else:  # pending
                if step.machine == MACHINE_WASH:
                    ready = washer_free
                else:
                    ready = max([dryer_free, *wash_ends]) if wash_ends else dryer_free
                s_off = ready + (self._gap if step.confirm_before else 0.0)
                s_off = max(s_off, now_off)
                if step.confirm_before:
                    at = self._available_from(t0 + timedelta(minutes=s_off), uu)
                    s_off = (at - t0).total_seconds() / 60.0
                e_off = s_off + step.duration_minutes
            out.append((
                t0 + timedelta(minutes=s_off),
                t0 + timedelta(minutes=e_off),
            ))
            if step.machine == MACHINE_WASH:
                washer_free = e_off
                wash_ends.append(e_off)
            else:
                dryer_free = e_off
        return out

    def continue_available(self) -> bool:
        return self.status == LAUNDRY_STATE_AWAITING

    def awaiting_machines(self) -> List[str]:
        """Machine types (``wash`` / ``dry``) with an unblocked, confirm-before
        step waiting on the user right now — i.e. the physical machines that
        must be free and ready before Continue can fire the changeover. Empty
        unless the run is awaiting a changeover. De-duplicated, in step order."""
        if self._run is None or self._run["status"] != LAUNDRY_STATE_AWAITING:
            return []
        steps = self._run_steps()
        states = self._run["states"]
        done   = [st == _STEP_DONE for st in states]
        active = [st in (_STEP_FIRED, _STEP_RUNNING) for st in states]
        machines: List[str] = []
        for i in unblocked_steps(steps, done, active):
            if steps[i].confirm_before and steps[i].machine not in machines:
                machines.append(steps[i].machine)
        return machines

    def next_action(self) -> str:
        if self._run is None:
            if self._preview and self._preview.get("planned_start"):
                t0 = _from_iso(self._preview["planned_start"])
                if t0 is not None:
                    when = dt_util.as_local(t0).strftime("%H:%M")
                    return f"Press Start — would begin at {when}"
            if self._preview and self._preview.get("infeasible"):
                return "No start fits — see the suggestions"
            return "Pick cycles and press Start"
        steps = self._run_steps()
        states = self._run["states"]
        status = self._run["status"]
        if status == LAUNDRY_STATE_SCHEDULED:
            t0 = _from_iso(self._run["t0"])
            when = dt_util.as_local(t0).strftime("%H:%M") if t0 else "?"
            return f"{steps[0].name} starts at {when}"
        done   = [st == _STEP_DONE for st in states]
        active = [st in (_STEP_FIRED, _STEP_RUNNING) for st in states]
        if status == LAUNDRY_STATE_AWAITING:
            ready = unblocked_steps(steps, done, active)
            names = ", ".join(
                steps[i].name for i in ready if steps[i].confirm_before
            )
            running = [steps[i].name for i, st in enumerate(states) if st == _STEP_RUNNING]
            also = f" ({', '.join(running)} still running)" if running else ""
            return f"Changeover — load {names}, then press Continue{also}"
        now = _utcnow()
        waiting = [
            (steps[i].name, at) for i, st in enumerate(states)
            if st == _STEP_FIRED and (at := _from_iso((self._run.get("start_at") or [None] * len(steps))[i])) and at > now
        ]
        running = [steps[i].name for i, a in enumerate(active) if a and steps[i].name not in {n for n, _ in waiting}]
        if waiting and not running:
            return f"Loaded — {', '.join(n for n, _ in waiting)} starts at {dt_util.as_local(min(a for _, a in waiting)):%H:%M}"
        if running:
            return f"Running: {', '.join(running)}"
        return "Waiting"

    def snapshot(self) -> Dict:
        base = {
            "status":         self.status,
            "next_action":    self.next_action(),
            "continue_available": self.continue_available(),
            "awaiting_machines":  self.awaiting_machines(),
            "planned_start":  None,
            "planned_finish": None,
            "plan_rate":      None,
            "plan_cost":      None,
            "total_kwh":      None,
            "current_step":   None,
            "preview":        False,
            "steps":          [],
            "best_windows":   [],
            "flows_due":      self.flows_due(),
            "now_rate":       None,
            "estimated_savings": None,
            "sleep_window":   (
                None if self._bedtime == self._wake
                else f"{self._bedtime.strftime('%H:%M')}–{self._wake.strftime('%H:%M')}"
            ),
            "changeover_after": _iso(
                _from_iso(self._run.get("unavailable_until")) if self._run
                else self._unavailable_until
            ),
            "away":           self.away,
            "order":          None,
            "suggestions":    [],
            "infeasible":     False,
            "offer":          self._offer,
            "continue_option": self._continue_opt if self.status == LAUNDRY_STATE_AWAITING else None,
            # Shown while the trip's machines are loaded but not yet started.
            "continue_delay": (self._run or {}).get("continue_delay") if self._run and any(
                (at := _from_iso(x)) and at > _utcnow() for x in (self._run.get("start_at") or [])
            ) else None,
            "stalled":        self._stalled(_utcnow()),
        }
        if self._run is None:
            if self._preview:
                base.update(self._preview)
                base["preview"] = bool(self._preview.get("planned_start"))
                base["estimated_savings"] = self._savings(base)
            return base
        steps = self._run_steps()
        times = self._projected_times()
        states = self._run["states"]
        n_done = sum(1 for st in states if st == _STEP_DONE)
        base.update({
            "planned_start":  self._run["t0"],
            "planned_finish": _iso(max(e for _, e in times)),
            "plan_rate":      self._run.get("plan_rate"),
            "plan_cost":      self._run.get("plan_cost"),
            "total_kwh":      self._run.get("total_kwh"),
            "now_rate":       self._run.get("now_rate"),
            "current_step":   f"{min(n_done + 1, len(steps))}/{len(steps)}",
            "steps": [
                {
                    "machine":        s.machine,
                    "name":           s.name,
                    "cycle_key":      s.cycle_key,
                    "state":          states[i],
                    "confirm_before": s.confirm_before,
                    "flow":           s.flow_slug,
                    "load":           s.load,
                    "planned_start":  _iso(times[i][0]),
                    "planned_end":    _iso(times[i][1]),
                    "duration_minutes": round(s.duration_minutes, 1),
                    "kwh":            round(s.kwh, 3),
                }
                for i, s in enumerate(steps)
            ],
        })
        base["estimated_savings"] = self._savings(base)
        return base

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _fmt_money(self, v: float) -> str:
        coordinator = self.washer_coordinator()
        sym = getattr(coordinator, "currency_symbol", None)
        unit = getattr(coordinator, "rate_unit", None)
        sym = sym if isinstance(sym, str) else "£"
        if isinstance(unit, str) and unit and abs(v) < 1:
            return f"{v * 100:.0f}{unit}"
        return f"{sym}{v:.2f}"

    @staticmethod
    def _fmt_dur(minutes: float) -> str:
        m = int(round(minutes))
        return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"

    @staticmethod
    def _fmt_when(when: datetime) -> str:
        local = dt_util.as_local(when)
        today = dt_util.as_local(_utcnow()).date()
        if local.date() == today:
            day = "today"
        elif local.date() == today + timedelta(days=1):
            day = "tomorrow"
        else:
            day = local.strftime("%a")
        return f"{day} {local.strftime('%H:%M')}"

    @staticmethod
    def _now_rate(rates, now: datetime, total_dur: float, profile, t0: datetime) -> Optional[float]:
        """What the plan would pay starting right now — None when the plan
        already starts now (nothing saved by waiting)."""
        if abs((t0 - now).total_seconds()) < 60 or not rates:
            return None
        return _avg_rate(rates, now, total_dur, profile)

    @staticmethod
    def _savings(snap: Dict) -> Optional[float]:
        now_rate, rate, kwh = snap.get("now_rate"), snap.get("plan_rate"), snap.get("total_kwh")
        if now_rate is None or rate is None or not kwh:
            return None
        return round((now_rate - rate) * kwh, 4)

    def _record_load(self, steps: List[PlanStep], finished: str) -> None:
        run = self._run
        starts = [a for a in run.get("actual_start", []) if a]
        self._loads.append({
            "id":          run["id"],
            "started_at":  min(starts) if starts else run["t0"],
            "finished_at": finished,
            "committed_at": run.get("committed_at"),
            "flows":       sorted({s.flow_slug for s in steps if s.flow_slug}),
            "steps": [
                {"machine": s.machine, "name": s.name, "cycle_key": s.cycle_key,
                 "start": run["actual_start"][i], "end": run["actual_end"][i]}
                for i, s in enumerate(steps)
            ],
            "plan_cost":   run.get("plan_cost"),
            "plan_rate":   run.get("plan_rate"),
            "now_rate":    run.get("now_rate"),
            "total_kwh":   run.get("total_kwh"),
            "mode":        run.get("mode"),
        })
        self._loads = self._loads[-LAUNDRY_MAX_LOADS:]

    @property
    def loads(self) -> List[Dict]:
        return list(self._loads)

    @property
    def entry_id(self) -> str:
        return self._entry_id

    @property
    def washer_slug(self) -> str:
        return self._washer_slug

    @property
    def dryer_slug(self) -> str:
        return self._dryer_slug

    @property
    def rows(self) -> List[str]:
        return list(self._rows)

    async def _save(self) -> None:
        await self._store.async_save({
            "run":        self._run,
            "last_done":  self._last_done,
            "autofilled": self._autofilled,
            "loads":      self._loads,
            "away":       self._away,
            "order_mode": self._order_mode,
        })

    def _fire_event(self, event_type: str, extra: Optional[Dict] = None) -> None:
        payload = {
            "pair_slug":   self.slug,
            "pair_name":   self.name,
            "washer_slug": self._washer_slug,
            "dryer_slug":  self._dryer_slug,
        }
        if extra:
            payload.update(extra)
        self._hass.bus.async_fire(event_type, payload)
