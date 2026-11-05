"""Pure laundry-plan logic — no HA dependencies.

A laundry run is a flat ordered list of steps across two machines (washer,
dryer). Two rules derive the whole schedule from list order alone — the user
never wires dependencies:

  1. Same-machine steps queue (one washer, one dryer).
  2. A dry step waits for every wash step listed before it.

Parallelism is implicit: in [Wash, Dry, Wash, Dry] the second wash runs
during the first dry because the washer is free and rule 2 doesn't block
washes.

A step with confirm_before=True needs a human trip (a changeover) before it
can start, so its planned start includes the pair's gap allowance;
confirm_before=False steps fire the moment they're unblocked (re-run chains,
e.g. drying thick bedding twice without opening the door). The first step of
a run is machine-commanded at t0 and never pays the gap.

Scoring treats the whole plan as one composite virtual cycle: offsets from
t0 per the rules above, each step's energy distributed over its span (using
its learned curve when available), overlapping steps summing. The resulting
profile plugs straight into window_scorer's profile-weighted scoring.
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

MACHINE_WASH = "wash"
MACHINE_DRY  = "dry"

# Bucket width for composite profiles. Fine enough to resolve 30-min rate
# slots under any offset; coarse enough that a 12-hour plan stays ~150 buckets.
COMPOSITE_BUCKET_MINUTES = 5.0

# Uniform fallback granularity for steps without a learned curve.
_UNIFORM_BUCKETS = 8


@dataclass
class PlanStep:
    machine: str                  # MACHINE_WASH | MACHINE_DRY
    cycle_key: str
    name: str
    duration_minutes: float
    kwh: float
    confirm_before: bool = True
    profile: Optional[List[float]] = None  # learned per-cycle energy fractions
    flow_slug: Optional[str] = None        # source flow (display / curve keying)
    load: Optional[str] = None             # Load row label the step came from (display / reordering)
    attended: bool = False                 # must START while you're around (load in use overnight)

    def as_dict(self) -> dict:
        return {
            "machine":          self.machine,
            "cycle_key":        self.cycle_key,
            "name":             self.name,
            "duration_minutes": self.duration_minutes,
            "kwh":              self.kwh,
            "confirm_before":   self.confirm_before,
            "flow_slug":        self.flow_slug,
            "load":             self.load,
            "attended":         self.attended,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlanStep":
        return cls(
            machine=d["machine"],
            cycle_key=d.get("cycle_key", ""),
            name=d.get("name", d.get("cycle_key", "")),
            duration_minutes=float(d["duration_minutes"]),
            kwh=float(d.get("kwh") or 0.0),
            confirm_before=bool(d.get("confirm_before", True)),
            flow_slug=d.get("flow_slug"),
            load=d.get("load"),
            attended=bool(d.get("attended", False)),
        )


def validate_plan(steps: List[PlanStep]) -> Optional[str]:
    """Return a human-readable problem with the plan, or None if it's valid."""
    if not steps:
        return "plan has no steps"
    for i, s in enumerate(steps):
        if s.machine not in (MACHINE_WASH, MACHINE_DRY):
            return f"step {i + 1} has unknown machine {s.machine!r}"
        if s.duration_minutes <= 0:
            return f"step {i + 1} ({s.name}) has no duration"
    return None


def derive_offsets(
    steps: List[PlanStep],
    gap_minutes: float,
    *,
    washer_free: float = 0.0,
    dryer_free: float = 0.0,
    prior_wash_ends: Optional[List[float]] = None,
    first_step_immediate: bool = True,
    changeover_at: Optional[Callable[[float], float]] = None,
    clear_of: Optional[Callable[[float, float], float]] = None,
) -> List[float]:
    """Planned start offset (minutes) for each step, applying the two rules.

    The seed kwargs support mid-run re-projection: pass the remaining pending
    steps with machine-free times / completed wash ends taken from actuals
    (all measured on the same offset axis), and first_step_immediate=False so
    the next step still pays its changeover gap.

    changeover_at maps the earliest offset a changeover step could start to
    the earliest offset a human is actually available (e.g. past the sleep
    window). Auto-firing steps never wait for it.

    clear_of(start, duration) maps a step's earliest start offset to the
    earliest offset at which a step of that length runs clear of every
    blackout (Octopus power-down etc.). It applies to every step after the
    first — a later step simply waits out the blackout, which splits the plan
    (wash, pause, dry) rather than running a machine through it. The first
    step starts at the plan's own start, so the caller rejects that candidate
    instead (see LaundryManager._shape_fn). Auto-firing steps are delayed too:
    SmartStart fires them, so it can wait.
    """
    wash_ends: List[float] = list(prior_wash_ends or [])
    offsets: List[float] = []
    for i, step in enumerate(steps):
        if step.machine == MACHINE_WASH:
            ready = washer_free
        else:
            ready = max([dryer_free, *wash_ends]) if wash_ends else dryer_free
        pays_gap = step.confirm_before and not (i == 0 and first_step_immediate)
        start = ready + (gap_minutes if pays_gap else 0.0)
        shifts = not (i == 0 and first_step_immediate)
        if (pays_gap and changeover_at is not None) or (shifts and clear_of is not None):
            # Settle: leaving a blackout can land in the sleep window and vice
            # versa, so alternate until neither moves the start.
            for _ in range(8):
                prev = start
                if shifts and clear_of is not None:
                    start = clear_of(start, step.duration_minutes)
                if pays_gap and changeover_at is not None:
                    start = max(start, changeover_at(start))
                if start == prev:
                    break
        offsets.append(start)
        end = start + step.duration_minutes
        if step.machine == MACHINE_WASH:
            washer_free = end
            wash_ends.append(end)
        else:
            dryer_free = end
    return offsets


def max_wet_wait(steps: List[PlanStep], offsets: List[float]) -> float:
    """Longest time (minutes) washed laundry sits waiting for its dry: for
    each dry step, start minus the end of the latest wash listed before it."""
    worst = 0.0
    last_wash_end: Optional[float] = None
    for step, off in zip(steps, offsets):
        if step.machine == MACHINE_WASH:
            last_wash_end = off + step.duration_minutes
        elif last_wash_end is not None:
            worst = max(worst, off - last_wash_end)
            last_wash_end = None  # that load is now in the dryer
    return worst


def plan_total_duration(steps: List[PlanStep], offsets: List[float]) -> float:
    return max(o + s.duration_minutes for s, o in zip(steps, offsets))


def plan_total_kwh(steps: List[PlanStep]) -> float:
    return sum(s.kwh for s in steps)


def build_composite_profile(
    steps: List[PlanStep],
    offsets: List[float],
    bucket_minutes: float = COMPOSITE_BUCKET_MINUTES,
) -> Tuple[List[float], float, float]:
    """Composite energy profile for the whole plan.

    Returns (profile, total_duration_minutes, total_kwh) where profile is a
    list of energy fractions summing to 1 — window_scorer treats each fraction
    as the share of total energy drawn in that time bucket, so kWh-heavy steps
    (usually the dryer) pull the chosen window toward their cheap slots and
    gaps contribute nothing.
    """
    total_dur = plan_total_duration(steps, offsets)
    total_kwh = plan_total_kwh(steps)
    n_b = max(1, int(-(-total_dur // bucket_minutes)))
    energy = [0.0] * n_b

    for step, off in zip(steps, offsets):
        if step.kwh <= 0 or step.duration_minutes <= 0:
            continue
        prof = step.profile if step.profile else [1.0 / _UNIFORM_BUCKETS] * _UNIFORM_BUCKETS
        prof_sum = sum(prof) or 1.0
        seg_len = step.duration_minutes / len(prof)
        for j, frac in enumerate(prof):
            seg_kwh = step.kwh * (frac / prof_sum)
            if seg_kwh <= 0:
                continue
            seg_start = off + j * seg_len
            seg_end   = seg_start + seg_len
            first_b = max(0, int(seg_start // bucket_minutes))
            last_b  = min(n_b - 1, int(-(-seg_end // bucket_minutes)) - 1)
            for b in range(first_b, last_b + 1):
                overlap = min(seg_end, (b + 1) * bucket_minutes) - max(seg_start, b * bucket_minutes)
                if overlap > 0:
                    energy[b] += seg_kwh * overlap / seg_len

    if total_kwh <= 0:
        # No energy data at all — fall back to uniform so scoring still works.
        return [1.0 / n_b] * n_b, total_dur, 0.0
    return [e / total_kwh for e in energy], total_dur, total_kwh


def unblocked_steps(
    steps: List[PlanStep],
    done: List[bool],
    active: List[bool],
) -> List[int]:
    """Indices of steps that could physically start right now.

    A step is unblocked when it isn't done/active, its machine is free (no
    earlier same-machine step still pending or active — the queue can't be
    jumped), and (for a dry) every wash listed before it is done.
    """
    ready: List[int] = []
    for i, step in enumerate(steps):
        if done[i] or active[i]:
            continue
        blocked = False
        for j in range(i):
            if steps[j].machine == step.machine and not done[j]:
                blocked = True  # machine busy or queue-jumping
                break
            if step.machine == MACHINE_DRY and steps[j].machine == MACHINE_WASH and not done[j]:
                blocked = True  # rule 2: dry waits for preceding washes
                break
        if not blocked:
            ready.append(i)
    return ready
