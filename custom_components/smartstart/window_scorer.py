"""Pure window-scoring logic — no HA dependencies.

Scoring works at minute precision. Candidate start times for a cycle are:

  - **Boundary candidates** — each rate-slot start that's in [now+min_delay, now+max_delay].
  - **End-aligned candidates** — each rate-slot start `B` shifted backwards by
    `cycle_duration`, so the cycle ends exactly at `B`. Lets us land on a
    "cheap → expensive" transition without paying for any of the expensive slot.

Every candidate is scored by the time-weighted average rate it experiences
across the slots it covers. The lowest-cost candidate wins.

For appliances that only accept a coarse delay step (e.g. LG dryer = 60-min
reservation, some washers = 30 or 15 minute increments), pass
`delay_step_minutes` > 1 — candidates are then restricted to integer
multiples of that step from now. The default (1) keeps the full
boundary/end-aligned candidate set.
"""
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Sequence, Tuple
import logging

from .providers.base import RatePeriod

_LOGGER = logging.getLogger(__name__)

# Start-dependent plan shape: start → (profile, duration_minutes), or None when
# that start is infeasible. Used when a plan's timeline depends on WHEN it
# runs (laundry changeovers pushed past the user's sleep window), so one fixed
# profile can't describe every candidate.
ProfileFn = Callable[[datetime], Optional[Tuple[List[float], float]]]

# Spans a cycle must not overlap (Octopus power-down / saving sessions): sorted
# or not, half-open [start, end). A window that merely touches a span (ends at
# its start, or starts at its end) is fine.
Blackouts = Sequence[Tuple[datetime, datetime]]


@dataclass
class ScoredWindow:
    start: datetime
    end: datetime
    avg_rate_cost_per_kwh: float
    delay_minutes: int
    n_slots: int  # ceil(duration / slot_minutes) — informational
    # True only for the immediate-start ("now") window when it overlaps a
    # blackout. It stays scoreable as the comparison baseline, but nothing may
    # choose it (Mode Now is the one explicit override — see the coordinator).
    blocked: bool = False


def _overlaps(blackouts: Optional[Blackouts], start: datetime, end: datetime) -> bool:
    return bool(blackouts) and any(b_start < end and start < b_end for b_start, b_end in blackouts)


def _infer_slot_minutes(rates: List[RatePeriod]) -> int:
    if len(rates) >= 2:
        gap = (rates[1].start - rates[0].start).total_seconds() / 60
        if 15 <= gap <= 60:
            return int(gap)
    return 30


def _slots_needed(duration_minutes: float, slot_minutes: int) -> int:
    return max(1, int(-(-int(round(duration_minutes)) // slot_minutes)))


def _profile_weighted_rate(
    rates: List[RatePeriod],
    window_start: datetime,
    duration_minutes: float,
    profile: List[float],
) -> float:
    """Profile-weighted average rate — energy distributed per the learned profile."""
    n_b = len(profile)
    bucket_secs = duration_minutes * 60.0 / n_b
    starts = [r.start for r in rates]  # rates are sorted; bisect beats a scan
    total = 0.0
    for i, frac in enumerate(profile):
        midpoint = window_start + timedelta(seconds=(i + 0.5) * bucket_secs)
        j = bisect_right(starts, midpoint) - 1
        rate = (
            rates[j].rate_cost_per_kwh
            if j >= 0 and midpoint < rates[j].end else None
        )
        if rate is None:
            rate = next(
                (r.rate_cost_per_kwh for r in rates if r.end > window_start), 0.0
            )
        total += frac * rate
    return total


def _time_weighted_rate(
    rates: List[RatePeriod],
    start: datetime,
    duration_minutes: float,
) -> Optional[float]:
    """Uniform time-weighted average rate across the cycle's actual minutes.

    Returns None if the window isn't fully covered by loaded rates (the cycle
    would run past the data we have).
    """
    end = start + timedelta(minutes=duration_minutes)
    weighted_sum = 0.0
    total_seconds = 0.0
    for slot in rates:
        if slot.end <= start or slot.start >= end:
            continue
        overlap_start = max(slot.start, start)
        overlap_end   = min(slot.end, end)
        overlap_s = (overlap_end - overlap_start).total_seconds()
        if overlap_s <= 0:
            continue
        weighted_sum += slot.rate_cost_per_kwh * overlap_s
        total_seconds += overlap_s

    if total_seconds <= 0:
        return None
    # Sanity: the cycle's full duration must be covered
    required_seconds = duration_minutes * 60
    if total_seconds + 1 < required_seconds:  # +1s slack for rounding
        return None
    return weighted_sum / total_seconds


def _avg_rate(
    rates: List[RatePeriod],
    start: datetime,
    duration_minutes: float,
    profile: Optional[List[float]],
) -> Optional[float]:
    """Choose between profile-weighted and time-weighted averaging."""
    if profile is not None:
        return _profile_weighted_rate(rates, start, duration_minutes, profile)
    return _time_weighted_rate(rates, start, duration_minutes)


def _generate_candidates(
    rates: List[RatePeriod],
    now: datetime,
    duration_minutes: float,
    min_delay_minutes: int,
    max_delay_minutes: int,
    delay_step_minutes: int = 1,
    anchor: str = "start",
    constraint_duration_minutes: Optional[float] = None,
    blackouts: Optional[Blackouts] = None,
) -> List[datetime]:
    """Return sorted, deduplicated start-time candidates within delay bounds.

    delay_step_minutes: minimum delay increment the appliance accepts. 1 =
        minute precision (slot-boundary + end-aligned candidates). >1 =
        restrict to multiples of step from now.

    anchor = "start" (default): candidates measured from cycle start. Used for
        start_delay and managed handling.
    anchor = "end": candidates measured from cycle end (now + k*step = when
        the cycle ENDS). start = end − duration. Used for end_time handling
        (LG-style reservation, where the appliance accepts a delayed-end).

    constraint_duration_minutes: the duration the *appliance's* delay feature
        acts on, when it differs from the scored duration. Laundry plans score
        wash + gap + dry as one composite window, but only the first wash is
        machine-commanded — so end-anchored reservations and step math must
        use the wash's duration while scoring spans the full plan. None (the
        default) means they're the same, i.e. a normal single cycle.
    """
    earliest = now + timedelta(minutes=max(0, min_delay_minutes))
    latest   = now + timedelta(minutes=max(min_delay_minutes, max_delay_minutes))
    cdur     = constraint_duration_minutes if constraint_duration_minutes is not None else duration_minutes

    candidates: set = set()
    step = max(1, int(delay_step_minutes))

    if step > 1:
        if anchor == "end":
            # Commanded segment ends at now + k*step; it starts at end − cdur.
            # Smallest viable k: enough that start >= now + min_delay.
            min_end_minutes = max(int(cdur), min_delay_minutes + int(cdur))
            first_k = max(1, -(-min_end_minutes // step))
            last_k  = max_delay_minutes // step
            for k in range(first_k, last_k + 1):
                end_t   = now + timedelta(minutes=k * step)
                start_t = end_t - timedelta(minutes=cdur)
                if earliest <= start_t <= latest:
                    candidates.add(start_t)
        else:
            # Start at k*step offset from now.
            first_k = max(0, -(-min_delay_minutes // step))
            last_k  = max_delay_minutes // step
            for k in range(first_k, last_k + 1):
                candidates.add(now + timedelta(minutes=k * step))
    else:
        # step == 1: minute precision — slot boundaries + end-aligned candidates.
        # When constraint and scoring durations differ, align on both: "plan
        # ends at a boundary" (dryer-dominant plans want this) and "commanded
        # segment ends at a boundary".
        for slot in rates:
            if earliest <= slot.start <= latest:
                candidates.add(slot.start)
            for dur in {duration_minutes, cdur}:
                # End-aligned: (plan or commanded segment) ends exactly at slot.start
                aligned = slot.start - timedelta(minutes=dur)
                if earliest <= aligned <= latest:
                    candidates.add(aligned)
        # Blackout edges: start the moment one ends, or finish the moment one
        # begins — these are the only places a blackout-constrained optimum can
        # sit that rate-slot boundaries might not already cover.
        for b_start, b_end in blackouts or ():
            for dur in {duration_minutes, cdur}:
                for edge in (b_end, b_start - timedelta(minutes=dur)):
                    if earliest <= edge <= latest:
                        candidates.add(edge)

    # Always include "now" as a candidate so the now_window is comparable
    # even when step > 1.
    if now <= latest:
        candidates.add(now)

    return sorted(candidates)


def _now_slot_start(rates: List[RatePeriod], now: datetime) -> Optional[datetime]:
    """Start time of the slot containing `now` (or first future slot if now is gap)."""
    for slot in rates:
        if slot.start <= now < slot.end:
            return slot.start
    for slot in rates:
        if slot.start > now:
            return slot.start
    return None


def _build_window(
    rates: List[RatePeriod],
    start: datetime,
    duration_minutes: float,
    now: datetime,
    profile: Optional[List[float]],
    profile_fn: Optional[ProfileFn] = None,
    blackouts: Optional[Blackouts] = None,
) -> Optional[ScoredWindow]:
    if profile_fn is not None:
        shape = profile_fn(start)
        if shape is None:
            return None
        profile, duration_minutes = shape
    # A delayed window must finish inside the known rates. Profile scoring
    # prices uncovered buckets by fallback (ultimately 0.0), so a window past
    # the horizon scored as free and always won — e.g. every plan landing at
    # 23:00 before day-ahead publication. "Now" is exempt: an immediate start
    # stays scoreable (Mode Now, the comparison baseline) even if it runs on.
    end = start + timedelta(minutes=duration_minutes)
    if start != now and end > rates[-1].end:
        return None
    blocked = _overlaps(blackouts, start, end)
    if blocked and start != now:
        return None
    avg = _avg_rate(rates, start, duration_minutes, profile)
    if avg is None:
        return None
    slot_minutes = _infer_slot_minutes(rates)
    return ScoredWindow(
        start=start,
        end=start + timedelta(minutes=duration_minutes),
        avg_rate_cost_per_kwh=avg,
        delay_minutes=max(0, int((start - now).total_seconds() / 60)),
        n_slots=_slots_needed(duration_minutes, slot_minutes),
        blocked=blocked,
    )


def score_windows(
    rates: List[RatePeriod],
    now: datetime,
    duration_minutes: float,
    min_delay_minutes: int = 0,
    max_delay_minutes: int = 1440,
    prefer_sooner_diff: float = 0.03,
    min_rate_clamp: Optional[float] = None,
    power_profile: Optional[List[float]] = None,
    delay_step_minutes: int = 1,
    anchor: str = "start",
    constraint_duration_minutes: Optional[float] = None,
    profile_fn: Optional[ProfileFn] = None,
    blackouts: Optional[Blackouts] = None,
) -> Tuple[Optional[ScoredWindow], Optional[ScoredWindow], Optional[ScoredWindow]]:
    """Returns (now_window, best_window, chosen_window).

    - now_window  : window starting at "now" (the immediate-start case).
    - best_window : lowest-avg-rate candidate within delay bounds.
    - chosen_window: best_window unless saving < prefer_sooner_diff vs now,
                     in which case now_window wins — unless now_window is
                     blocked by a blackout, which never wins.
    """
    if not rates:
        return None, None, None

    candidates = _generate_candidates(
        rates, now, duration_minutes,
        min_delay_minutes, max_delay_minutes, delay_step_minutes,
        anchor=anchor,
        constraint_duration_minutes=constraint_duration_minutes,
        blackouts=blackouts,
    )
    if not candidates:
        return None, None, None

    now_window: Optional[ScoredWindow] = None
    best_window: Optional[ScoredWindow] = None
    # "now" is always scored (for the comparison baseline) but is only
    # *selectable* as best when min_delay allows an immediate start.
    earliest = now + timedelta(minutes=max(0, min_delay_minutes))

    def _clamped(avg: float) -> float:
        return max(avg, min_rate_clamp) if min_rate_clamp is not None else avg

    for start in candidates:
        win = _build_window(rates, start, duration_minutes, now, power_profile, profile_fn, blackouts)
        if win is None:
            continue
        # The "now" candidate becomes now_window
        if start == now:
            now_window = win
        if start < earliest or win.blocked:
            continue
        if (
            best_window is None
            or _clamped(win.avg_rate_cost_per_kwh) < _clamped(best_window.avg_rate_cost_per_kwh)
        ):
            best_window = win

    if now_window is None and best_window is None:
        return None, None, None
    if now_window is not None and now_window.blocked:
        return now_window, best_window, best_window
    if now_window is None or best_window is None:
        return now_window, best_window, best_window or now_window

    saving = now_window.avg_rate_cost_per_kwh - best_window.avg_rate_cost_per_kwh
    chosen = best_window if saving >= prefer_sooner_diff else now_window
    return now_window, best_window, chosen


def find_soonest_below_rate(
    rates: List[RatePeriod],
    now: datetime,
    duration_minutes: float,
    target_rate: float,
    min_delay_minutes: int = 0,
    max_delay_minutes: int = 1440,
    power_profile: Optional[List[float]] = None,
    delay_step_minutes: int = 1,
    anchor: str = "start",
    constraint_duration_minutes: Optional[float] = None,
    profile_fn: Optional[ProfileFn] = None,
    blackouts: Optional[Blackouts] = None,
) -> Optional[ScoredWindow]:
    """The earliest candidate window whose avg rate is <= target_rate.

    Used by Balanced mode: "find me the soonest acceptable window — don't
    make me wait longer than necessary for a slightly cheaper one".
    Returns None if no candidate in the delay range meets the target.
    """
    if not rates:
        return None
    candidates = _generate_candidates(
        rates, now, duration_minutes,
        min_delay_minutes, max_delay_minutes, delay_step_minutes,
        anchor=anchor,
        constraint_duration_minutes=constraint_duration_minutes,
        blackouts=blackouts,
    )
    earliest = now + timedelta(minutes=max(0, min_delay_minutes))
    for start in candidates:
        if start < earliest:
            continue
        win = _build_window(rates, start, duration_minutes, now, power_profile, profile_fn, blackouts)
        if win is None or win.blocked:
            continue
        if win.avg_rate_cost_per_kwh <= target_rate:
            return win
    return None


def find_window_for_deadline(
    rates: List[RatePeriod],
    now: datetime,
    duration_minutes: float,
    deadline: datetime,
    min_delay_minutes: int = 0,
    power_profile: Optional[List[float]] = None,
    delay_step_minutes: int = 1,
    anchor: str = "start",
    constraint_duration_minutes: Optional[float] = None,
    profile_fn: Optional[ProfileFn] = None,
    blackouts: Optional[Blackouts] = None,
) -> Optional[ScoredWindow]:
    """Cheapest candidate whose window end is on or before the deadline."""
    if not rates:
        return None

    max_delay_minutes = max(
        min_delay_minutes,
        int((deadline - now).total_seconds() / 60) - int(round(duration_minutes)),
    )
    candidates = _generate_candidates(
        rates, now, duration_minutes,
        min_delay_minutes, max_delay_minutes, delay_step_minutes,
        anchor=anchor,
        constraint_duration_minutes=constraint_duration_minutes,
        blackouts=blackouts,
    )

    best: Optional[ScoredWindow] = None
    earliest = now + timedelta(minutes=max(0, min_delay_minutes))
    for start in candidates:
        if start < earliest:
            continue
        if start + timedelta(minutes=duration_minutes) > deadline:
            continue
        win = _build_window(rates, start, duration_minutes, now, power_profile, profile_fn, blackouts)
        if win is None or win.blocked or win.end > deadline:
            continue
        if best is None or win.avg_rate_cost_per_kwh < best.avg_rate_cost_per_kwh:
            best = win
    return best


def best_windows(
    rates: List[RatePeriod],
    now: datetime,
    duration_minutes: float,
    n: int = 3,
    min_separation_minutes: float = 360,
    min_delay_minutes: int = 0,
    power_profile: Optional[List[float]] = None,
    delay_step_minutes: int = 1,
    anchor: str = "start",
    constraint_duration_minutes: Optional[float] = None,
    profile_fn: Optional[ProfileFn] = None,
    blackouts: Optional[Blackouts] = None,
) -> List[ScoredWindow]:
    """The n cheapest windows across the whole rate horizon, cheapest first,
    each starting at least max(duration, min_separation) from the others —
    distinct cheap spells (tonight / tomorrow night) rather than n starts a
    few minutes apart inside the same trough. Ignores max_delay: this is a
    planning list, not a schedule."""
    if not rates:
        return []
    horizon_min = int((rates[-1].end - now).total_seconds() / 60)
    candidates = _generate_candidates(
        rates, now, duration_minutes,
        min_delay_minutes, max(min_delay_minutes, horizon_min), delay_step_minutes,
        anchor=anchor,
        constraint_duration_minutes=constraint_duration_minutes,
        blackouts=blackouts,
    )
    earliest = now + timedelta(minutes=max(0, min_delay_minutes))
    scored = []
    for start in candidates:
        if start < earliest:
            continue
        win = _build_window(rates, start, duration_minutes, now, power_profile, profile_fn, blackouts)
        if win is not None and not win.blocked:
            scored.append(win)
    scored.sort(key=lambda w: (w.avg_rate_cost_per_kwh, w.start))

    sep = timedelta(minutes=max(duration_minutes, min_separation_minutes))
    picked: List[ScoredWindow] = []
    for win in scored:
        if all(abs(win.start - p.start) >= sep for p in picked):
            picked.append(win)
            if len(picked) == n:
                break
    return picked


def window_band(
    rates: List[RatePeriod],
    window: ScoredWindow,
    power_profile: Optional[List[float]] = None,
) -> Tuple[float, float]:
    """(low, high) average rate for a window, from the forecast bands of the
    slots it covers. Real slots (no band) contribute their actual rate to
    both bounds, so a fully-real window collapses to (avg, avg)."""
    duration = (window.end - window.start).total_seconds() / 60

    def _with(attr: str) -> List[RatePeriod]:
        return [
            RatePeriod(
                start=r.start, end=r.end,
                rate_cost_per_kwh=(
                    getattr(r, attr) if getattr(r, attr) is not None
                    else r.rate_cost_per_kwh
                ),
            )
            for r in rates
        ]

    low = _avg_rate(_with("rate_low"), window.start, duration, power_profile)
    high = _avg_rate(_with("rate_high"), window.start, duration, power_profile)
    avg = window.avg_rate_cost_per_kwh
    return (avg if low is None else low, avg if high is None else high)

