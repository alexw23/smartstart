from datetime import datetime, time as dt_time, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import logging

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_HOLD_ENGAGE_LEAD,
    DEFAULT_OFFERS,
    DEFAULT_MAX_WAIT_HOURS,
    OFFER_MIN_FRACTION,
    OFFER_MIN_SAVING,
    OFFER_REFIRE_SHIFT_MIN,
    DEFAULT_RATES_EXPECTED_AT,
    HOLD_PUBLICATION_BUFFER_MIN,
    PLANNER_HORIZON_HOURS,
    PLANNER_MIN_SEPARATION_MIN,
    PLANNER_WINDOWS,
    CURVE_USE_THRESHOLD,
    CURVE_WARN_THRESHOLD,
    CURVE_CONFIDENT_CURVES,
    DEFAULT_CHEAP_WINDOW_THRESHOLD,
    DEFAULT_BALANCED_TARGET_RATE,
    DEFAULT_DELAY_HANDLING,
    DEFAULT_DELAY_STEP_MINUTES,
    DEFAULT_MODE,
    DEFAULT_POWER_DOWN,
    DEFAULT_RESCHEDULE_MIN_SAVING,
    POWER_DOWN_AVOID,
    DELAY_HANDLING_END_TIME,
    DELAY_HANDLING_MANAGED,
    MODE_BALANCED,
    MODE_MAX_SAVINGS,
    MODE_NOW,
    DOMAIN,
    DEFAULT_MAX_DELAY_MINUTES,
    DEFAULT_MIN_DELAY_MINUTES,
    PREFER_SOONER_RATE_DIFF,
    RESCHEDULE_RATE_EPSILON,
    STATE_SCHEDULED,
    UPDATE_INTERVAL_MINUTES,
)
from .providers.base import RateProvider, RatePeriod
from .window_scorer import (
    best_windows,
    window_band,
    score_windows,
    find_soonest_below_rate,
    find_window_for_deadline,
    ScoredWindow,
)

_LOGGER = logging.getLogger(__name__)


class SmartStartCoordinator(DataUpdateCoordinator):
    """Fetches rates and scores windows for all configured devices/cycles."""

    def __init__(
        self,
        hass: HomeAssistant,
        provider: RateProvider,
        devices: List[Dict],
        currency_symbol: str,
        rate_unit: str,
        curve_store=None,
        prefer_sooner_diff: float = PREFER_SOONER_RATE_DIFF,
        cheap_window_threshold: float = DEFAULT_CHEAP_WINDOW_THRESHOLD,
        reschedule_min_saving: float = DEFAULT_RESCHEDULE_MIN_SAVING,
        rates_expected_at: str = DEFAULT_RATES_EXPECTED_AT,
        hold_engage_lead: int = DEFAULT_HOLD_ENGAGE_LEAD,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._provider    = provider
        self._devices     = devices
        self.currency_symbol = currency_symbol
        self.rate_unit    = rate_unit
        self._rates: List[RatePeriod] = []
        # User-published forecast slots (smartstart.publish_predicted_rates).
        # Only consulted while a hold is active, and only for time beyond the
        # real rate horizon — real rates always win.
        self._predicted_rates: List[RatePeriod] = []
        self._curve_store = curve_store
        self._prefer_sooner_diff = prefer_sooner_diff
        self._cheap_window_threshold = cheap_window_threshold
        self._reschedule_min_saving = reschedule_min_saving
        self._rates_expected_at = rates_expected_at
        self._hold_engage_lead = hold_engage_lead
        # Optional scheduler reference, set after construction in __init__.py.
        # Used by _maybe_fire_better_window to read SCHEDULED state.
        self._scheduler = None
        # (slug, window_start_iso) → True. Edge-trigger so we don't spam events
        # when the best window stays put across multiple ticks.
        self._better_window_fired: Dict[tuple, bool] = {}
        self._warned_cycles: set = set()
        self._selected_cycles: Dict[str, str] = {}  # slug → cycle_id chosen by user
        # slug → scheduling mode (max_savings | balanced | now) set via SelectEntity.
        # Persisted across restarts by the entity's RestoreEntity behaviour.
        self._modes: Dict[str, str] = {}
        # slug → power-down handling (avoid | ignore), set via PowerDownSelectEntity.
        self._power_down: Dict[str, str] = {}
        # Cached blackout spans (Octopus power-down etc.); None = feature off.
        self._blackout_source = None
        # slug → optional deadline ("Cycle Due By"). Cleared on each Start press.
        self._cycle_due_at: Dict[str, datetime] = {}
        # (slug, cycle_key) → last best_window.start ISO we fired smartstart_cheap_window for.
        # Edge-trigger: re-fire only when the best_window itself moves to a different start.
        self._cheap_window_alerted: Dict[tuple, str] = {}
        # Slugs with an auto-reschedule task in flight. _build_data can run
        # again (rescore tick, rate push) before the async re-commit lands;
        # without this guard the same move would be dispatched twice.
        self._resched_inflight: set = set()
        # slug → current offer (display dict) and the window behind it.
        # Recomputed every tick while a run is scheduled; see _update_offer.
        self._offers: Dict[str, Dict] = {}
        self._offer_windows: Dict[str, ScoredWindow] = {}
        # slug → (start, predicted) of the last offer announced via
        # smartstart_offer_found — edge-trigger against forecast wobble.
        self._offer_fired: Dict[str, tuple] = {}

    def push_rates(self, rates: List[RatePeriod]) -> None:
        """Called by the manual provider handler when rates arrive via service."""
        self._rates = sorted(rates, key=lambda x: x.start)
        self.async_set_updated_data(self._build_data(datetime.now(timezone.utc)))

    def push_predicted_rates(self, rates: List[RatePeriod]) -> None:
        """Store user-published forecast slots and re-score. Not persisted —
        the publishing automation should re-push after an HA restart."""
        self._predicted_rates = sorted(rates, key=lambda x: x.start)
        if self._rates:
            self.async_set_updated_data(self._build_data(datetime.now(timezone.utc)))

    def _real_horizon_end(self) -> Optional[datetime]:
        return self._rates[-1].end if self._rates else None

    def _is_predicted_window(self, win) -> bool:
        """True if any part of the window lies beyond the real rate horizon."""
        horizon = self._real_horizon_end()
        return horizon is not None and win.end > horizon

    def _rates_with_predictions(self, now: datetime) -> List[RatePeriod]:
        """Real rates plus predicted slots strictly beyond the real horizon,
        priced at the band's upper bound when the source gives one — a hold
        should only wait for slots that look cheap even if the forecast
        runs high."""
        horizon = self._real_horizon_end()
        if horizon is None:
            return list(self._rates)
        tail = [
            RatePeriod(
                start=r.start, end=r.end,
                rate_cost_per_kwh=(
                    r.rate_high if r.rate_high is not None else r.rate_cost_per_kwh
                ),
            )
            for r in self._predicted_rates
            if r.start >= horizon and r.end > now
        ]
        return self._rates + tail

    def planner_rates(self, now: datetime) -> List[RatePeriod]:
        """Real rates plus forecast slots (central estimate, bands kept) out
        to PLANNER_HORIZON_HOURS — the best-windows list's view. Forecasts
        past ~48h can't see cheap spells, so the list stops there."""
        horizon = self._real_horizon_end()
        if horizon is None:
            return []
        limit = now + timedelta(hours=PLANNER_HORIZON_HOURS)
        tail = [
            r for r in self._predicted_rates
            if r.start >= horizon and r.end <= limit
        ]
        return self._rates + tail

    def best_windows_info(
        self,
        now: datetime,
        *,
        duration_minutes: float,
        kwh: float,
        profile: Optional[List[float]] = None,
        min_delay_minutes: int = 0,
        delay_step_minutes: int = 1,
        anchor: str = "start",
        constraint_duration_minutes: Optional[float] = None,
        profile_fn=None,
        blackouts=None,
    ) -> List[Dict]:
        """The PLANNER_WINDOWS cheapest distinct windows over real + forecast
        rates, as display rows (cheapest first). profile_fn: start-dependent
        plan shape (laundry sleep-window changeovers), see window_scorer."""
        rates = self.planner_rates(now)
        if not rates:
            return []
        horizon = self._real_horizon_end()
        out: List[Dict] = []
        for win in best_windows(
            rates, now, duration_minutes,
            n=PLANNER_WINDOWS,
            min_separation_minutes=PLANNER_MIN_SEPARATION_MIN,
            min_delay_minutes=min_delay_minutes,
            power_profile=profile,
            delay_step_minutes=delay_step_minutes,
            anchor=anchor,
            constraint_duration_minutes=constraint_duration_minutes,
            profile_fn=profile_fn,
            blackouts=blackouts,
        ):
            shape = profile_fn(win.start) if profile_fn else None
            low, high = window_band(rates, win, shape[0] if shape else profile)
            rate = win.avg_rate_cost_per_kwh
            out.append({
                "start":     win.start.isoformat(),
                "end":       win.end.isoformat(),
                "rate":      round(rate, 4),
                "rate_low":  round(low, 4),
                "rate_high": round(high, 4),
                "cost":      round(rate * kwh, 3),
                "confirmed": horizon is not None and win.end <= horizon,
            })
        return out

    @callback
    def rescore_now(self) -> None:
        """Re-score windows against the current time without re-fetching rates."""
        if not self._rates:
            return
        self.async_set_updated_data(self._build_data(datetime.now(timezone.utc)))

    async def _async_update_data(self) -> Dict:
        try:
            self._rates = await self._provider.async_get_rates()
        except Exception as exc:
            raise UpdateFailed(f"Failed to fetch rates: {exc}") from exc

        if self._rates:
            _LOGGER.info(
                "SmartStart: loaded %d rate slots (%s → %s)",
                len(self._rates),
                self._rates[0].start.strftime("%H:%M"),
                self._rates[-1].end.strftime("%H:%M"),
            )
        else:
            _LOGGER.warning("SmartStart: no rate slots available — windows cannot be scored")

        return self._build_data(datetime.now(timezone.utc))

    def _build_data(self, now: datetime) -> Dict:
        data: Dict = {}
        current_rate = self._current_rate(now)

        # Clear any deadlines that have already passed. (Per-cycle meetability is
        # not enforced here — it depends on cycle duration, and we don't want one
        # long cycle to nuke the deadline for the user's intended short cycle.)
        for slug in list(self._cycle_due_at.keys()):
            if self._cycle_due_at[slug] <= now:
                self._cycle_due_at.pop(slug, None)

        for device in self._devices:
            slug      = device["device_slug"]
            min_delay = device.get("min_delay_minutes", DEFAULT_MIN_DELAY_MINUTES)
            raw_max_delay  = device.get("max_delay_minutes", DEFAULT_MAX_DELAY_MINUTES)
            delay_handling = device.get("delay_handling", DEFAULT_DELAY_HANDLING)
            delay_step_minutes = int(device.get("delay_step_minutes", DEFAULT_DELAY_STEP_MINUTES))
            # Mode is runtime state (from the SelectEntity), not config
            mode               = self._modes.get(slug, DEFAULT_MODE)
            balanced_target    = float(device.get("balanced_target_rate", DEFAULT_BALANCED_TARGET_RATE))
            # Hold-for-rates: near next-day publication, push candidates past
            # publication + buffer so the committed placeholder can be
            # re-optimised when rates land. Bypassed by Mode=Now (explicit
            # immediate intent) and by a Cycle Due By deadline (which already
            # pins the timing decision).
            hold_until_dt = None
            if (
                device.get("hold_for_next_day_rates")
                and device.get("auto_reschedule")
                and mode != MODE_NOW
                and slug not in self._cycle_due_at
            ):
                hold_until_dt = self._compute_hold_until(now)
            # For "managed" with 0 / blank max_delay, search the full loaded rate
            # horizon — the crypto-miner / multi-day forecast case.
            if delay_handling == DELAY_HANDLING_MANAGED and (not raw_max_delay or raw_max_delay <= 0):
                if self._rates:
                    horizon_min = (self._rates[-1].end - now).total_seconds() / 60
                    raw_max_delay = max(1, int(horizon_min))
                else:
                    raw_max_delay = DEFAULT_MAX_DELAY_MINUTES
            cycles_data: Dict = {}
            sched = self._scheduler.get_schedule(slug) if self._scheduler else {}
            offer_cycle = (
                sched.get("cycle_id")
                if sched.get("status") == STATE_SCHEDULED
                and not sched.get("laundry_run")
                and device.get("offers", DEFAULT_OFFERS)
                else None
            )
            if offer_cycle is None:
                self._drop_offer(slug)

            blackouts = self.blackouts_for(slug, now)

            for cycle in device.get("cycles", []):
                cid = cycle["cycle_key"]

                profile          = None
                learned_duration = None
                curve_info       = {}

                if self._curve_store:
                    curve_info = self._curve_store.get_confidence_info(slug, cid)
                    if curve_info["confidence"] >= CURVE_USE_THRESHOLD:
                        profile = self._curve_store.get_profile(slug, cid)
                    # Use learned avg duration once we have enough runs, regardless of
                    # profile confidence (a dryer's duration varies but the average is
                    # still more accurate than the user's single estimate over time)
                    learned_duration = self._curve_store.get_avg_duration_minutes(slug, cid)
                    self._handle_curve_warning(slug, cid, device["name"], cycle, curve_info)

                duration_minutes = learned_duration or cycle["duration_minutes"]
                duration_source  = "learned" if learned_duration else "configured"
                if learned_duration and abs(learned_duration - cycle["duration_minutes"]) > 5:
                    _LOGGER.debug(
                        "SmartStart: %s/%s using learned duration %.1f min (configured: %d min)",
                        slug, cid, learned_duration, cycle["duration_minutes"],
                    )

                # For end_time appliances, max_delay = latest finish-from-now
                # (LG-style "finish by N hours"). The scorer's end-anchor path
                # treats max_delay_minutes as the END cap directly, so we pass
                # raw_max_delay unmodified — subtracting duration here would
                # double-shorten the allowed end horizon. The min_delay /
                # duration interaction is enforced inside _generate_candidates
                # (first_k clamps start_t >= now + min_delay).
                if delay_handling == DELAY_HANDLING_END_TIME and raw_max_delay < min_delay + cycle["duration_minutes"]:
                    _LOGGER.error(
                        "SmartStart: %s/%s — max_delay (%dmin) is shorter than "
                        "min_delay + cycle duration (%dmin); cycle longer than "
                        "appliance can hold",
                        slug, cid, raw_max_delay, min_delay + cycle["duration_minutes"],
                    )
                effective_max_delay = raw_max_delay

                # For end_time appliances, anchor candidates at cycle end (LG-style:
                # "finish in N hours" maps to "end = now + N hours, start = end − duration").
                anchor = "end" if delay_handling == DELAY_HANDLING_END_TIME else "start"

                # Balanced + hold: good-enough-now beats waiting. Probe with the
                # normal (unclamped) min_delay against KNOWN rates — if any
                # window already meets the target, take it and skip the hold
                # for this cycle. Max savings has no "good enough", so it
                # always holds while in the engage band.
                cycle_hold_until = hold_until_dt
                balanced_probe   = None
                if cycle_hold_until is not None and mode == MODE_BALANCED:
                    balanced_probe = find_soonest_below_rate(
                        self._rates, now,
                        duration_minutes=duration_minutes,
                        target_rate=balanced_target,
                        min_delay_minutes=min_delay,
                        max_delay_minutes=effective_max_delay,
                        power_profile=profile,
                        delay_step_minutes=delay_step_minutes,
                        anchor=anchor,
                        blackouts=blackouts,
                    )
                    if balanced_probe is not None:
                        cycle_hold_until = None

                # Hold clamp: candidates must start after publication + buffer.
                eff_min_delay = min_delay
                if cycle_hold_until is not None:
                    eff_min_delay = max(
                        min_delay,
                        int(-(-(cycle_hold_until - now).total_seconds() // 60)),
                    )

                # While holding, predicted rates (if the user publishes any)
                # extend the horizon so the placeholder can be pre-positioned
                # on a likely-cheap slot tomorrow. Real rates always win —
                # predictions only cover time beyond the real horizon, and the
                # reschedule-on-release re-checks against actuals.
                scoring_rates = self._rates
                if cycle_hold_until is not None and self._predicted_rates:
                    scoring_rates = self._rates_with_predictions(now)

                # We disable score_windows' built-in threshold (pass 0) so we
                # can apply the mode policy explicitly below — and only then
                # apply the prefer_sooner_diff filter on top.
                now_win, best_win, _ = score_windows(
                    scoring_rates,
                    now,
                    duration_minutes=duration_minutes,
                    min_delay_minutes=eff_min_delay,
                    max_delay_minutes=effective_max_delay,
                    prefer_sooner_diff=0.0,
                    power_profile=profile,
                    delay_step_minutes=delay_step_minutes,
                    anchor=anchor,
                    blackouts=blackouts,
                )

                # ── Apply mode policy ────────────────────────────────────────
                # mode picks the *candidate*; the global prefer_sooner_diff
                # threshold then decides whether the candidate is actually
                # worth delaying for vs. just running now.
                mode_fallback_used = False
                if mode == MODE_NOW:
                    candidate = now_win
                elif mode == MODE_BALANCED:
                    candidate = balanced_probe if balanced_probe is not None else find_soonest_below_rate(
                        scoring_rates, now,
                        duration_minutes=duration_minutes,
                        target_rate=balanced_target,
                        min_delay_minutes=eff_min_delay,
                        max_delay_minutes=effective_max_delay,
                        power_profile=profile,
                        delay_step_minutes=delay_step_minutes,
                        anchor=anchor,
                        blackouts=blackouts,
                    )
                    if candidate is None:
                        # No window meets the target — fall back to cheapest
                        candidate = best_win
                        mode_fallback_used = True
                else:  # MODE_MAX_SAVINGS
                    candidate = best_win

                # Apply the "don't wait for pennies" threshold on top of mode.
                # Skipped while a hold is active — snapping back to "now"
                # because the placeholder looks expensive would defeat the
                # whole point of waiting for tomorrow's rates.
                if candidate and now_win and candidate is not now_win and cycle_hold_until is None:
                    saving = now_win.avg_rate_cost_per_kwh - candidate.avg_rate_cost_per_kwh
                    # A "now" that overlaps a blackout is never the fallback.
                    if saving < self._prefer_sooner_diff and not now_win.blocked:
                        candidate = now_win

                # Mode Now is the explicit override: it starts now even over a
                # blackout. Everything else never lands on a blocked "now".
                chosen_win = candidate or best_win or (
                    now_win if mode == MODE_NOW or not (now_win and now_win.blocked) else None
                )

                # If user set a "Cycle Due By" deadline, override chosen with the
                # cheapest window that finishes in time. best_window stays
                # unconstrained so the sensor can show what we gave up.
                deadline_dt    = self._cycle_due_at.get(slug)
                deadline_used  = False
                if deadline_dt:
                    if deadline_dt <= now + timedelta(minutes=duration_minutes):
                        # Deadline already unmet — discard it
                        self._cycle_due_at.pop(slug, None)
                        deadline_dt = None
                    else:
                        deadline_win = find_window_for_deadline(
                            self._rates,
                            now,
                            duration_minutes=duration_minutes,
                            deadline=deadline_dt,
                            min_delay_minutes=min_delay,
                            power_profile=profile,
                            delay_step_minutes=delay_step_minutes,
                            anchor=anchor,
                            blackouts=blackouts,
                        )
                        if deadline_win is not None:
                            chosen_win    = deadline_win
                            deadline_used = True

                if now_win or best_win:
                    _LOGGER.info(
                        "SmartStart: [%s/%s] now=%.5f@%s  best=%.5f@%s (+%dmin)  "
                        "chosen=%s  duration=%dmin(%s)  scoring=%s  rates=%d",
                        slug, cid,
                        now_win.avg_rate_cost_per_kwh if now_win else float("nan"),
                        now_win.start.strftime("%H:%M") if now_win else "—",
                        best_win.avg_rate_cost_per_kwh if best_win else float("nan"),
                        best_win.start.strftime("%H:%M") if best_win else "—",
                        best_win.delay_minutes if best_win else 0,
                        "best" if chosen_win is not now_win else "now",
                        int(duration_minutes),
                        duration_source,
                        "profile" if profile else "uniform",
                        len(self._rates),
                    )
                else:
                    _LOGGER.warning(
                        "SmartStart: [%s/%s] no windows found — %d rate slots available",
                        slug, cid, len(self._rates),
                    )

                # Planning list for the selected cycle only — it scores the
                # whole horizon, so skip cycles nobody is looking at.
                selected = self.get_selected_cycle_id(slug, device)
                windows_info = None
                if selected in (cid, cycle.get("cycle_id")):
                    windows_info = self.best_windows_info(
                        now,
                        duration_minutes=duration_minutes,
                        kwh=float(cycle.get("total_kwh") or 0.0),
                        profile=profile,
                        min_delay_minutes=min_delay,
                        delay_step_minutes=delay_step_minutes,
                        anchor=anchor,
                        blackouts=blackouts,
                    )

                cycles_data[cid] = {
                    "best_windows":      windows_info,
                    "cycle":             cycle,
                    "now_window":        now_win,
                    "best_window":       best_win,
                    "chosen_window":     chosen_win,
                    "profile_active":   profile is not None,
                    "duration_minutes": duration_minutes,
                    "duration_source":  duration_source,
                    "deadline":         deadline_dt,
                    "deadline_used":    deadline_used,
                    "mode":             mode,
                    "balanced_target_rate": balanced_target if mode == MODE_BALANCED else None,
                    "mode_fallback_used": mode_fallback_used,
                    "hold_active":      cycle_hold_until is not None,
                    "hold_until":       cycle_hold_until,
                    # True when the chosen window's rate comes (partly) from
                    # user-published predictions rather than real rates.
                    "chosen_is_predicted": (
                        chosen_win is not None and self._is_predicted_window(chosen_win)
                    ),
                }

                self._maybe_fire_cheap_window(slug, cid, cycle, best_win, now_win)
                if device.get("auto_reschedule"):
                    self._maybe_auto_reschedule(
                        slug, cid, now,
                        duration_minutes=duration_minutes,
                        min_delay=min_delay,
                        profile=profile,
                        delay_step_minutes=delay_step_minutes,
                        anchor=anchor,
                    )
                else:
                    self._maybe_fire_better_window(slug, cid, cycle, chosen_win)
                if cid == offer_cycle and slug not in self._resched_inflight:
                    self._update_offer(
                        slug, device, cycle, now,
                        duration_minutes=duration_minutes,
                        min_delay=min_delay,
                        profile=profile,
                        delay_step_minutes=delay_step_minutes,
                        anchor=anchor,
                    )

            data[slug] = {
                "device":       device,
                "current_rate": current_rate,
                "cycles":       cycles_data,
            }

        return data

    def _maybe_fire_cheap_window(
        self,
        slug: str,
        cycle_key: str,
        cycle: Dict,
        best_win,
        now_win,
    ) -> None:
        """Fire smartstart_cheap_window when best_window is below threshold.

        Edge-triggered: re-fires only when the best window's start time changes.
        Hovering around the threshold for the same window stays silent.
        """
        if best_win is None:
            return
        # Never alert on forecast slots — the event promises a real cheap window
        if self._is_predicted_window(best_win):
            return
        if best_win.avg_rate_cost_per_kwh > self._cheap_window_threshold:
            return
        key = (slug, cycle_key)
        last_start = self._cheap_window_alerted.get(key)
        if last_start == best_win.start.isoformat():
            return
        self._cheap_window_alerted[key] = best_win.start.isoformat()

        device = next((d for d in self._devices if d["device_slug"] == slug), {})
        savings_pct = None
        if now_win and now_win.avg_rate_cost_per_kwh > 0:
            savings_pct = round(
                (1 - best_win.avg_rate_cost_per_kwh / now_win.avg_rate_cost_per_kwh) * 100, 1
            )
        self.hass.bus.async_fire("smartstart_cheap_window", {
            "device_slug":       slug,
            "device_name":       device.get("name", slug),
            "cycle_key":         cycle_key,
            "cycle_name":        cycle.get("name"),
            "cycle_id":          cycle.get("cycle_id") or cycle.get("name", ""),
            "window_start":      best_win.start.isoformat(),
            "window_end":        best_win.end.isoformat(),
            "delay_minutes":     best_win.delay_minutes,
            "avg_rate_cost_per_kwh": round(best_win.avg_rate_cost_per_kwh, 4),
            "savings_pct":       savings_pct,
            "threshold":         self._cheap_window_threshold,
        })

    def _maybe_fire_better_window(
        self,
        slug: str,
        cycle_key: str,
        cycle: Dict,
        chosen_win,
    ) -> None:
        """Fire smartstart_better_window_found when a scheduled cycle could be
        moved to a meaningfully cheaper window.

        Notify-only path, used when the device's auto_reschedule option is off —
        users wire their own reschedule logic in automations. With auto_reschedule
        on, _maybe_auto_reschedule runs instead and moves the schedule itself.

        Edge-triggered per (slug, new_start) so we don't repeat-fire on every tick.
        """
        if chosen_win is None or self._scheduler is None:
            return
        sched = self._scheduler.get_schedule(slug)
        if sched.get("status") != STATE_SCHEDULED:
            return
        # A LaundryManager owns this run — plan-level optimisation happens there
        if sched.get("laundry_run"):
            return
        # Only consider the cycle the schedule is currently aimed at
        if sched.get("cycle_id") != cycle_key:
            return

        baseline_rate  = sched.get("rate_cost_per_kwh")
        baseline_start = sched.get("scheduled_start")
        if baseline_rate is None or baseline_start is None:
            return

        # Same window we're already scheduled for — nothing changed
        if chosen_win.start.isoformat() == baseline_start:
            return
        # New window must be cheaper by at least the configured threshold
        if chosen_win.avg_rate_cost_per_kwh > baseline_rate - self._reschedule_min_saving:
            return
        # Don't fire for past windows — sanity guard
        if chosen_win.start <= datetime.now(timezone.utc):
            return

        key = (slug, chosen_win.start.isoformat())
        if key in self._better_window_fired:
            return
        self._better_window_fired[key] = True

        device = next((d for d in self._devices if d["device_slug"] == slug), {})
        cycle_kwh = cycle.get("total_kwh") or 0.0
        saving_per_kwh = baseline_rate - chosen_win.avg_rate_cost_per_kwh
        self.hass.bus.async_fire("smartstart_better_window_found", {
            "device_slug":        slug,
            "device_name":        device.get("name", slug),
            "cycle_key":          cycle_key,
            "cycle_name":         cycle.get("name"),
            "cycle_id":           cycle.get("cycle_id") or cycle.get("name", ""),
            "old_start":          baseline_start,
            "old_avg_rate_cost_per_kwh": round(baseline_rate, 4),
            "new_start":          chosen_win.start.isoformat(),
            "new_end":            chosen_win.end.isoformat(),
            "new_avg_rate_cost_per_kwh": round(chosen_win.avg_rate_cost_per_kwh, 4),
            "saving_per_kwh":     round(saving_per_kwh, 4),
            "estimated_saving":   round(saving_per_kwh * cycle_kwh, 4),
        })
        _LOGGER.info(
            "SmartStart: better window for %s/%s — %s → %s (saving %.4f £/kWh)",
            slug, cycle_key, baseline_start, chosen_win.start.isoformat(), saving_per_kwh,
        )

    def _maybe_auto_reschedule(
        self,
        slug: str,
        cycle_key: str,
        now: datetime,
        *,
        duration_minutes: float,
        min_delay: int,
        profile,
        delay_step_minutes: int,
        anchor: str,
    ) -> None:
        """Move a SCHEDULED cycle to a cheaper window as new rates arrive.

        Runs instead of _maybe_fire_better_window when the device has
        auto_reschedule enabled. The search is constrained to the envelope
        committed at schedule time (sched["reschedule_cap"]): the finish-by
        deadline if one was set, else original-schedule-time + max_delay —
        so repeated reschedules can't creep the start ever later. Any rate
        improvement beyond RESCHEDULE_RATE_EPSILON triggers the move; each
        candidate is compared against the *currently committed* rate, so
        savings don't need to stack across successive reschedules.
        """
        if self._scheduler is None or not self._rates:
            return
        if slug in self._resched_inflight:
            return
        sched = self._scheduler.get_schedule(slug)
        if sched.get("status") != STATE_SCHEDULED:
            return
        # A LaundryManager owns this run — wash-only rescheduling would fight
        # the composite plan; the manager re-optimises the whole plan itself.
        if sched.get("laundry_run"):
            return
        if sched.get("cycle_id") != cycle_key:
            return

        baseline_rate  = sched.get("rate_cost_per_kwh")
        baseline_start = sched.get("scheduled_start")
        if baseline_rate is None or baseline_start is None:
            return
        # Start already due/past — the appliance is firing imminently
        try:
            baseline_start_dt = datetime.fromisoformat(baseline_start)
            if baseline_start_dt <= now:
                return
        except ValueError:
            return

        baseline_rate = self._committed_rate(sched)

        # A power-down announced after this run was scheduled can leave the
        # committed window overlapping it. That run must move even if the
        # only window clear of it costs more — avoiding the blackout is the
        # point, so the "must be cheaper" test below is waived.
        baseline_blocked = False
        try:
            baseline_end = datetime.fromisoformat(sched["estimated_finish"])
            baseline_blocked = any(
                b_start < baseline_end and baseline_start_dt < b_end
                for b_start, b_end in (self.blackouts_for(slug, now) or ())
            )
        except (KeyError, TypeError, ValueError):
            pass

        # A held run must not be "improved" back into a pre-publication window
        # while tomorrow's rates are still pending — keep the clamp until the
        # hold expires or the rates actually arrive (whichever is first).
        hold_iso = sched.get("hold_until")
        if hold_iso:
            try:
                hold_dt = datetime.fromisoformat(hold_iso)
            except ValueError:
                hold_dt = None
            if (
                hold_dt is not None
                and now < hold_dt
                and self._next_day_rates_missing(now)
            ):
                min_delay = max(
                    min_delay, int(-(-(hold_dt - now).total_seconds() // 60))
                )

        cap: Optional[datetime] = None
        cap_iso = sched.get("reschedule_cap")
        if cap_iso:
            try:
                cap = datetime.fromisoformat(cap_iso)
            except ValueError:
                return

        if sched.get("reschedule_cap_kind") == "deadline" and cap is not None:
            candidate = find_window_for_deadline(
                self._rates, now,
                duration_minutes=duration_minutes,
                deadline=cap,
                min_delay_minutes=min_delay,
                power_profile=profile,
                delay_step_minutes=delay_step_minutes,
                anchor=anchor,
                blackouts=self.blackouts_for(slug, now),
            )
        else:
            # "window" cap: for start-anchored devices max_delay bounds the
            # start; for end_time devices it bounds the finish — both match
            # score_windows' max_delay semantics for their anchor, so
            # (cap − now) in minutes is the effective max_delay either way.
            # No cap (unbounded managed) → search the full rate horizon.
            if cap is not None:
                eff_max = int((cap - now).total_seconds() / 60)
                if eff_max < 1:
                    return
            else:
                eff_max = max(1, int((self._rates[-1].end - now).total_seconds() / 60))
            _, candidate, _ = score_windows(
                self._rates, now,
                duration_minutes=duration_minutes,
                min_delay_minutes=min_delay,
                max_delay_minutes=eff_max,
                prefer_sooner_diff=0.0,
                power_profile=profile,
                delay_step_minutes=delay_step_minutes,
                anchor=anchor,
                blackouts=self.blackouts_for(slug, now),
            )

        if candidate is None:
            return
        if candidate.start.isoformat() == baseline_start:
            return
        if (
            candidate.avg_rate_cost_per_kwh > baseline_rate - RESCHEDULE_RATE_EPSILON
            and not baseline_blocked
        ):
            return

        self._resched_inflight.add(slug)

        async def _do_reschedule() -> None:
            try:
                await self._scheduler.async_reschedule(slug, candidate)
            finally:
                self._resched_inflight.discard(slug)
                # Re-evaluate now that the guard is clear: rates may have
                # advanced while this move was being committed (e.g. the
                # second Octopus entity landing mid-reschedule), and those
                # evaluations were suppressed by the in-flight guard.
                self.rescore_now()

        self.hass.async_create_task(_do_reschedule())

    def _committed_rate(self, sched: Dict) -> Optional[float]:
        """The rate a scheduled run is committed at, for "is this cheaper?".

        A commit placed on a *predicted* rate is re-priced on real rates once
        they cover the window — otherwise an optimistic prediction (say 5p
        that turned out to be 8p) would block a genuinely cheaper real window
        from taking over.
        """
        rate = sched.get("rate_cost_per_kwh")
        if rate is None or not sched.get("rate_predicted"):
            return rate
        from .window_scorer import _time_weighted_rate
        try:
            start = datetime.fromisoformat(sched["scheduled_start"])
            finish = datetime.fromisoformat(sched["estimated_finish"])
        except (KeyError, TypeError, ValueError):
            return rate
        if finish <= start:
            return rate
        real_avg = _time_weighted_rate(
            self._rates, start, (finish - start).total_seconds() / 60
        )
        return real_avg if real_avg is not None else rate

    def next_rate_release(self, now: datetime) -> Optional[datetime]:
        """When tomorrow's rates are expected (today's release time, UTC), or
        None once they're loaded. May be in the past when publication is late."""
        if not self._rates or not self._next_day_rates_missing(now):
            return None
        t = dt_util.parse_time(self._rates_expected_at) or dt_time(16, 0)
        return dt_util.as_utc(dt_util.as_local(now).replace(
            hour=t.hour, minute=t.minute, second=0, microsecond=0
        ))

    # ── Offers ────────────────────────────────────────────────────────────────

    def get_offer(self, slug: str) -> Optional[Dict]:
        return self._offers.get(slug)

    def _drop_offer(self, slug: str) -> None:
        self._offers.pop(slug, None)
        self._offer_windows.pop(slug, None)

    def _reachable_by_auto(self, sched: Dict, win: ScoredWindow, anchor: str) -> bool:
        """True if auto-reschedule could move the run here on its own: real
        rates only, inside the committed envelope."""
        if self._is_predicted_window(win):
            return False
        try:
            cap = datetime.fromisoformat(sched["reschedule_cap"]) if sched.get("reschedule_cap") else None
        except ValueError:
            return False
        if cap is None:
            return True
        if sched.get("reschedule_cap_kind") == "deadline" or anchor == "end":
            return win.end <= cap
        return win.start <= cap

    def _update_offer(
        self,
        slug: str,
        device: Dict,
        cycle: Dict,
        now: datetime,
        *,
        duration_minutes: float,
        min_delay: int,
        profile,
        delay_step_minutes: int,
        anchor: str,
    ) -> None:
        """Recompute the device's offer: the cheapest window finishing within
        the device's wait limit (max_wait_hours) of the original Start press, over real rates plus
        forecast slots at their central estimate, that beats the committed plan by
        OFFER_MIN_SAVING and OFFER_MIN_FRACTION and that the schedule can't
        reach by itself (with auto-reschedule on, reachable windows are just
        taken). Announces via smartstart_offer_found on meaningful change."""
        sched = self._scheduler.get_schedule(slug)
        baseline = self._committed_rate(sched)
        try:
            pressed = datetime.fromisoformat(sched["scheduled_at"])
            planned_start = datetime.fromisoformat(sched["scheduled_start"])
        except (KeyError, TypeError, ValueError):
            self._drop_offer(slug)
            return
        # Central estimate, not the hold's high bound: an offer is a user
        # choice shown with its band, and if real rates disappoint the
        # reschedule-on-release falls back within the accepted deadline.
        rates = self.planner_rates(now)
        if baseline is None or not rates or planned_start <= now:
            self._drop_offer(slug)
            return
        horizon_end = pressed + timedelta(
            hours=int(device.get("max_wait_hours") or DEFAULT_MAX_WAIT_HOURS)
        )
        deadline = min(horizon_end, rates[-1].end)
        win = find_window_for_deadline(
            rates, now,
            duration_minutes=duration_minutes,
            deadline=deadline,
            min_delay_minutes=min_delay,
            power_profile=profile,
            delay_step_minutes=delay_step_minutes,
            anchor=anchor,
            blackouts=self.blackouts_for(slug, now),
        )
        kwh = float(cycle.get("total_kwh") or 0.0)
        saving = (baseline - win.avg_rate_cost_per_kwh) * kwh if win else 0.0
        if (
            win is None
            or abs((win.start - planned_start).total_seconds()) < 60
            or saving < OFFER_MIN_SAVING
            or (baseline > 0 and win.avg_rate_cost_per_kwh > baseline * (1 - OFFER_MIN_FRACTION))
            or (device.get("auto_reschedule") and self._reachable_by_auto(sched, win, anchor))
        ):
            self._drop_offer(slug)
            return

        predicted = self._is_predicted_window(win)
        low, high = window_band(rates, win, profile)
        offer = {
            "start":         win.start.isoformat(),
            "end":           win.end.isoformat(),
            "rate":          round(win.avg_rate_cost_per_kwh, 4),
            "rate_low":      round(low, 4),
            "rate_high":     round(high, 4),
            "cost":          round(win.avg_rate_cost_per_kwh * kwh, 3),
            "saving":        round(saving, 3),
            "saving_pct":    round((1 - win.avg_rate_cost_per_kwh / baseline) * 100) if baseline > 0 else None,
            "predicted":     predicted,
            "planned_start": planned_start.isoformat(),
            "planned_rate":  round(baseline, 4),
            "horizon_end":   horizon_end.isoformat(),
        }
        offer["message"] = self._offer_message(device, offer)
        self._offers[slug] = offer
        self._offer_windows[slug] = win

        prev = self._offer_fired.get(slug)
        if prev is None and sched.get("offer_announced"):
            # Survives restarts — don't re-announce an offer already sent.
            ann = sched["offer_announced"]
            try:
                prev = (datetime.fromisoformat(ann["start"]), bool(ann["predicted"]))
            except (KeyError, TypeError, ValueError):
                prev = None
        if (
            prev is None
            or (prev[1] and not predicted)  # forecast offer now confirmed
            or abs((win.start - prev[0]).total_seconds()) >= OFFER_REFIRE_SHIFT_MIN * 60
        ):
            self._offer_fired[slug] = (win.start, predicted)
            self._scheduler.mark_offer_announced(slug, win.start, predicted)
            self.hass.bus.async_fire("smartstart_offer_found", {
                "device_slug": slug,
                "device_name": device.get("name", slug),
                "cycle_key":   cycle["cycle_key"],
                "cycle_name":  cycle.get("name"),
                "kind":        "forecast" if predicted else "confirmed",
                **offer,
            })
            _LOGGER.info("SmartStart: offer for %s — %s", slug, offer["message"])

    def _offer_message(self, device: Dict, offer: Dict) -> str:
        def _t(iso: str) -> str:
            local = dt_util.as_local(datetime.fromisoformat(iso))
            today = dt_util.as_local(datetime.now(timezone.utc)).date()
            day = "" if local.date() == today else (
                " tomorrow" if local.date() == today + timedelta(days=1)
                else local.strftime(" %a")
            )
            return local.strftime("%-I:%M%p").lower() + day
        saving = offer["saving"]
        # Under one major unit, say it in the minor unit (17p, 17c).
        amount = (
            f"{saving * 100:.0f}{self.rate_unit}" if saving < 1
            else f"{self.currency_symbol}{saving:.2f}"
        )
        source = "the forecast suggests" if offer["predicted"] else "found"
        return (
            f"{device.get('name', 'Appliance')}: {source} ~{amount} cheaper if you wait "
            f"until {_t(offer['start'])} instead of the planned {_t(offer['planned_start'])}."
        )

    async def async_accept_offer(self, slug: str) -> None:
        """Move the scheduled run to its current offer. Widens the run's
        envelope to the offer's finish — the only path that does."""
        from homeassistant.exceptions import HomeAssistantError
        win = self._offer_windows.get(slug)
        offer = self._offers.get(slug)
        if win is None or offer is None or self._scheduler is None:
            raise HomeAssistantError("No SmartStart offer to accept for this device")
        if win.start <= datetime.now(timezone.utc):
            self._drop_offer(slug)
            raise HomeAssistantError("That offer has expired")
        if self._scheduler.get_schedule(slug).get("status") != STATE_SCHEDULED:
            self._drop_offer(slug)
            raise HomeAssistantError("Nothing is scheduled to move")
        self._drop_offer(slug)
        await self._scheduler.async_reschedule(
            slug, win,
            reschedule_cap=win.end,
            rate_predicted=offer["predicted"],
            reason="offer",
        )
        self.rescore_now()

    def _handle_curve_warning(
        self, slug: str, cycle_id: str, device_name: str, cycle: Dict, info: dict
    ) -> None:
        key = (slug, cycle_id)
        if info["warning"] and key not in self._warned_cycles:
            self._warned_cycles.add(key)
            persistent_notification.async_create(
                self.hass,
                f"SmartStart has recorded {info['n_curves']} cycles for "
                f"**{device_name} / {cycle.get('name', cycle_id)}** but the power curves "
                f"are inconsistent (confidence {info['confidence']:.0%}). "
                f"This may mean cycles are being interrupted or the device's consumption "
                f"varies too much between runs. SmartStart will continue using uniform "
                f"rate averaging until confidence improves.",
                title="SmartStart: Inconsistent power curve",
                notification_id=f"smartstart_curve_{slug}_{cycle_id}",
            )
        elif not info["warning"] and key in self._warned_cycles:
            self._warned_cycles.discard(key)
            persistent_notification.async_dismiss(
                self.hass,
                notification_id=f"smartstart_curve_{slug}_{cycle_id}",
            )

    def _next_day_rates_missing(self, now: datetime) -> bool:
        """True if the loaded rate horizon doesn't extend meaningfully into
        tomorrow (local). Around publication, Octopus appends today's
        23:00–24:00 tail ~10 min before the next-day entity updates — a
        horizon ending at/near midnight must still count as missing, or the
        hold lifts early on partial data."""
        if not self._rates:
            return False
        return not self._horizon_covers_tomorrow(
            dt_util.as_local(self._rates[-1].end), dt_util.as_local(now)
        )

    @staticmethod
    def _horizon_covers_tomorrow(last_end_local: datetime, now_local: datetime) -> bool:
        """Rates count as covering tomorrow only past 03:00 tomorrow local —
        comfortably beyond any midnight-tail artifact, comfortably before any
        real next-day publication horizon (Agile gives until 23:00)."""
        threshold = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1, hours=3)
        return last_end_local >= threshold

    def rates_with_predictions(self, now: datetime) -> List[RatePeriod]:
        """Public wrapper — the LaundryManager scores held plans on the same
        real-plus-forecast horizon as single devices."""
        return self._rates_with_predictions(now)

    def is_predicted_window(self, win) -> bool:
        return self._is_predicted_window(win)

    def compute_hold_until(self, now: datetime) -> Optional[datetime]:
        """Public wrapper for the hold-for-rates window — used by the
        LaundryManager, which applies the same hold policy at pair level."""
        return self._compute_hold_until(now)

    def _compute_hold_until(self, now: datetime) -> Optional[datetime]:
        """UTC time to hold starts until (publication + buffer), or None when
        the hold shouldn't engage: tomorrow's rates already loaded, publication
        further away than the engage lead, or the buffer window already past
        (late publication → schedule normally on known data)."""
        if not self._rates or not self._next_day_rates_missing(now):
            return None
        t = dt_util.parse_time(self._rates_expected_at) or dt_time(16, 0)
        pub_local = dt_util.as_local(now).replace(
            hour=t.hour, minute=t.minute, second=0, microsecond=0
        )
        pub = dt_util.as_utc(pub_local)
        hold_until = pub + timedelta(minutes=HOLD_PUBLICATION_BUFFER_MIN)
        if now >= hold_until:
            return None
        if pub - now > timedelta(minutes=self._hold_engage_lead):
            return None
        return hold_until

    def _current_rate(self, now: datetime) -> Optional[float]:
        for rate in self._rates:
            if rate.start <= now < rate.end:
                return rate.rate_cost_per_kwh
        return None

    def rate_at(self, when: datetime) -> Optional[float]:
        """Return the £/kWh rate covering `when`, or None if no slot matches."""
        for rate in self._rates:
            if rate.start <= when < rate.end:
                return rate.rate_cost_per_kwh
        return None

    def set_selected_cycle(self, device_slug: str, cycle_id: str) -> None:
        self._selected_cycles[device_slug] = cycle_id

    def set_scheduling_mode(self, device_slug: str, mode: str) -> None:
        """Set the per-device scheduling mode (max_savings | balanced | now).
        Triggered by the SchedulingModeSelectEntity."""
        self._modes[device_slug] = mode

    def get_scheduling_mode(self, device_slug: str) -> str:
        return self._modes.get(device_slug, DEFAULT_MODE)

    def set_power_down_handling(self, device_slug: str, handling: str) -> None:
        """Set what a cycle does about blackout windows (avoid | ignore).
        Triggered by the PowerDownSelectEntity."""
        self._power_down[device_slug] = handling

    def get_power_down_handling(self, device_slug: str) -> str:
        return self._power_down.get(device_slug, DEFAULT_POWER_DOWN)

    def set_blackout_source(self, source) -> None:
        self._blackout_source = source
        if source is not None:
            source.set_on_change(self.rescore_now)

    @property
    def blackout_calendars(self) -> List[str]:
        return self._blackout_source.entity_ids if self._blackout_source else []

    def blackout_spans(self, now: datetime) -> List[Tuple[datetime, datetime]]:
        """Upcoming blackout spans regardless of any device's setting — what
        the panel draws, and what a laundry pair applies when it avoids them."""
        return self._blackout_source.spans(now) if self._blackout_source else []

    def blackouts_for(self, device_slug: str, now: datetime):
        """Spans this device's cycles must not overlap, or None when it
        ignores them (or none are configured)."""
        if self.get_power_down_handling(device_slug) != POWER_DOWN_AVOID:
            return None
        return self.blackout_spans(now) or None

    def set_scheduler(self, scheduler) -> None:
        """Wire the scheduler in after construction (avoids a circular dep)."""
        self._scheduler = scheduler

    def status_for(self, device_slug: str) -> str:
        """Return the current cycle state (idle/scheduled/running/paused).
        Used by selects to reject changes that would create drift between
        committed schedule and displayed values."""
        from .const import STATE_IDLE
        if self._scheduler is None:
            return STATE_IDLE
        return self._scheduler.get_schedule(device_slug).get("status", STATE_IDLE)

    def clear_better_window_dedup(self, device_slug: str) -> None:
        """Reset the better-window edge-trigger for a device. Called by the
        scheduler when a device starts/cancels/finishes so the next SCHEDULED
        run gets fresh evaluation."""
        for key in [k for k in self._better_window_fired if k[0] == device_slug]:
            self._better_window_fired.pop(key, None)
        self._drop_offer(device_slug)
        self._offer_fired.pop(device_slug, None)

    def set_cycle_due_at(self, device_slug: str, deadline: Optional[datetime]) -> None:
        """Store/clear the per-device 'Cycle Due By' deadline."""
        if deadline is None:
            self._cycle_due_at.pop(device_slug, None)
        else:
            self._cycle_due_at[device_slug] = deadline

    def get_cycle_due_at(self, device_slug: str) -> Optional[datetime]:
        return self._cycle_due_at.get(device_slug)

    @property
    def selected_cycles(self) -> Dict[str, str]:
        return self._selected_cycles

    def get_selected_cycle_id(self, device_slug: str, device_cfg: Dict) -> str | None:
        """Return the selected cycle_id, falling back to the first configured cycle."""
        return (
            self._selected_cycles.get(device_slug)
            or (device_cfg.get("cycles") or [{}])[0].get("cycle_id")
        )

    def get_window_starting_at(
        self, device_slug: str, cycle_id: str, start: datetime
    ) -> Optional[ScoredWindow]:
        """The window at a user-picked start (a Best Windows row), priced on
        real + forecast rates like the planning list it came from. Snaps to
        the appliance's delay step; None if it's in the past or unpriced."""
        device = next((d for d in self._devices if d["device_slug"] == device_slug), None)
        cd = (self.data or {}).get(device_slug, {}).get("cycles", {}).get(cycle_id)
        if device is None or not cd:
            return None
        now = datetime.now(timezone.utc)
        if start <= now:
            return None
        duration = float(cd["duration_minutes"])
        profile = (
            self._curve_store.get_profile(device_slug, cycle_id)
            if self._curve_store and cd.get("profile_active") else None
        )
        step = int(device.get("delay_step_minutes", DEFAULT_DELAY_STEP_MINUTES))
        anchor = "end" if device.get("delay_handling") == DELAY_HANDLING_END_TIME else "start"
        lead = int((start - now).total_seconds() // 60)
        return find_window_for_deadline(
            self.planner_rates(now), now,
            duration_minutes=duration,
            deadline=start + timedelta(minutes=duration + max(1, step)),
            min_delay_minutes=max(0, lead - step + 1) if step > 1 else lead,
            power_profile=profile,
            delay_step_minutes=step,
            anchor=anchor,
        )

    def get_window_for_deadline(
        self, device_slug: str, cycle_id: str, deadline: datetime
    ) -> Optional[ScoredWindow]:
        device = next((d for d in self._devices if d["device_slug"] == device_slug), None)
        if device is None:
            return None
        cycle = next(
            (c for c in device.get("cycles", []) if c["cycle_key"] == cycle_id), None
        )
        if cycle is None:
            return None
        profile          = None
        learned_duration = None
        if self._curve_store:
            info = self._curve_store.get_confidence_info(device_slug, cycle_id)
            if info["confidence"] >= CURVE_USE_THRESHOLD:
                profile = self._curve_store.get_profile(device_slug, cycle_id)
            learned_duration = self._curve_store.get_avg_duration_minutes(device_slug, cycle_id)

        anchor = "end" if device.get("delay_handling") == DELAY_HANDLING_END_TIME else "start"
        now = datetime.now(timezone.utc)
        return find_window_for_deadline(
            self._rates,
            now,
            duration_minutes=learned_duration or cycle["duration_minutes"],
            deadline=deadline,
            min_delay_minutes=device.get("min_delay_minutes", DEFAULT_MIN_DELAY_MINUTES),
            power_profile=profile,
            delay_step_minutes=int(device.get("delay_step_minutes", DEFAULT_DELAY_STEP_MINUTES)),
            anchor=anchor,
            blackouts=self.blackouts_for(device_slug, now),
        )

    @property
    def rates(self) -> List[RatePeriod]:
        return list(self._rates)

    @property
    def prefer_sooner_diff(self) -> float:
        return self._prefer_sooner_diff
