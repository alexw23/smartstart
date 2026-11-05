"""Stores and analyses per-cycle power curves."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CURVE_CONFIDENT_CURVES,
    CURVE_FLAT_CV,
    CURVE_MIN_CURVES,
    CURVE_MIN_DURATION_FRACTION,
    CURVE_N_BUCKETS,
    CURVE_WARN_THRESHOLD,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_STORAGE_KEY     = f"{DOMAIN}_power_curves"
_STORAGE_VERSION = 1
_MAX_CURVES      = 20  # keep most-recent N curves per cycle


def _samples_to_kwh(samples: list) -> float:
    """Integrate power samples to kWh using the trapezoidal method."""
    if len(samples) < 2:
        return 0.0
    total_ws = 0.0
    for i in range(len(samples) - 1):
        dt    = samples[i + 1]["t_s"] - samples[i]["t_s"]
        p_avg = (float(samples[i]["power_w"]) + float(samples[i + 1]["power_w"])) / 2.0
        total_ws += p_avg * dt
    return round(total_ws / 3_600_000, 4)  # W·s → kWh


def _samples_to_cost(samples: list, rate_at) -> Optional[float]:
    """Per-interval cost: ∑ (interval kWh × rate in effect at that moment).

    rate_at: Callable[[datetime], Optional[float]] — returns £/kWh for a wall
    clock instant, or None if no rate data covers it.
    Returns the running total in £, or None if no sample carries wall-clock
    info (legacy samples from before per-slot costing was added).
    """
    from datetime import datetime
    if len(samples) < 2:
        return 0.0
    total_cost = 0.0
    saw_wall_ts = False
    for i in range(len(samples) - 1):
        dt_s   = samples[i + 1]["t_s"] - samples[i]["t_s"]
        p_avg  = (float(samples[i]["power_w"]) + float(samples[i + 1]["power_w"])) / 2.0
        kwh    = p_avg * dt_s / 3_600_000
        wall_ts_str = samples[i].get("wall_ts")
        if wall_ts_str is None:
            continue
        saw_wall_ts = True
        try:
            wall_ts = datetime.fromisoformat(wall_ts_str)
        except ValueError:
            continue
        rate = rate_at(wall_ts)
        if rate is None:
            continue
        total_cost += kwh * rate
    return round(total_cost, 6) if saw_wall_ts else None


def _to_profile(samples: list, n_buckets: int = CURVE_N_BUCKETS) -> list[float]:
    """Convert raw [{t_s, power_w}] samples to a normalised energy-fraction profile."""
    if len(samples) < 2:
        return [1.0 / n_buckets] * n_buckets

    t0  = float(samples[0]["t_s"])
    dur = float(samples[-1]["t_s"]) - t0
    if dur <= 0:
        return [1.0 / n_buckets] * n_buckets

    energy = [0.0] * n_buckets

    for k in range(len(samples) - 1):
        t_a  = (float(samples[k]["t_s"])     - t0) / dur  # normalised 0-1
        t_b  = (float(samples[k + 1]["t_s"]) - t0) / dur
        p_a  = float(samples[k]["power_w"])
        p_b  = float(samples[k + 1]["power_w"])
        if t_b <= t_a:
            continue

        ba   = t_a * n_buckets
        bb   = t_b * n_buckets
        span = bb - ba

        i0 = int(ba)
        i1 = min(int(bb), n_buckets - 1)

        for i in range(i0, i1 + 1):
            lo = max(ba, float(i))
            hi = min(bb, float(i + 1))
            if hi <= lo:
                continue
            # Linearly interpolate power at lo and hi within this segment
            f_lo = (lo - ba) / span
            f_hi = (hi - ba) / span
            p_lo = p_a + (p_b - p_a) * f_lo
            p_hi = p_a + (p_b - p_a) * f_hi
            energy[i] += (p_lo + p_hi) * 0.5 * (hi - lo)

    total = sum(energy)
    if total <= 0:
        return [1.0 / n_buckets] * n_buckets
    return [e / total for e in energy]


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _mean_profile(profiles: list[list[float]]) -> list[float]:
    n, n_b = len(profiles), len(profiles[0])
    return [sum(p[i] for p in profiles) / n for i in range(n_b)]


def _profile_type(mean_profile: list[float]) -> str:
    """Classify whether a cycle's power draw is flat (dryer-like) or dynamic (washer/dishwasher).

    Uses the coefficient of variation of the mean profile. A uniform distribution
    (all buckets equal) has CV=0. We call anything below CURVE_FLAT_CV "flat".
    """
    if not mean_profile:
        return "unknown"
    mu = sum(mean_profile) / len(mean_profile)  # always 1/N_BUCKETS
    if mu <= 0:
        return "unknown"
    variance = sum((v - mu) ** 2 for v in mean_profile) / len(mean_profile)
    cv = math.sqrt(variance) / mu
    return "flat" if cv < CURVE_FLAT_CV else "dynamic"


def _duration_stats(durations_s: list[float]) -> dict:
    """Return avg/std in minutes for a list of actual run durations."""
    if not durations_s:
        return {"avg_minutes": None, "std_minutes": None}
    mins = [d / 60.0 for d in durations_s]
    avg  = sum(mins) / len(mins)
    std  = math.sqrt(sum((m - avg) ** 2 for m in mins) / len(mins)) if len(mins) > 1 else 0.0
    return {"avg_minutes": round(avg, 1), "std_minutes": round(std, 1)}


def _confidence(profiles: list[list[float]]) -> float:
    """0-1 confidence combining completeness (number of curves) and consistency.

    Consistency measures whether the *energy-vs-time* shape repeats — the only
    thing the scheduler bets on when it aligns a cycle with cheap tariff slots.
    We score it on the cumulative energy curve (the running fraction of total
    energy delivered by each time bucket) rather than the per-bucket fractions.

    Per-bucket CV was the wrong tool: a bucket holding ~1% of the energy in a
    low-power wash/rinse phase divides sensor noise by a near-zero mean and
    reports a huge CV, so long quiet tails dragged genuinely repeatable heated
    cycles to zero. The cumulative curve is monotonic and dominated by where the
    energy mass actually sits, so a few minutes of jitter in the heater spike (or
    noise in a near-zero tail) barely moves it, while a run that dumps its energy
    at a genuinely different time stands out.

    consistency = 1 - 2 · max_bucket( std of the cumulative curve at that bucket )
    i.e. one minus (twice) the worst single-point disagreement about how much
    energy has been delivered so far. The ×2 scales it: the cumulative std peaks
    near 0.5 when half the runs front-load and half back-load their energy.
    """
    n = len(profiles)
    if n == 0:
        return 0.0

    completeness = min(n / CURVE_CONFIDENT_CURVES, 1.0)
    if n < 2:
        return round(completeness * 0.5, 4)

    n_b = len(profiles[0])
    # Cumulative energy fraction delivered by the end of each bucket, per run.
    cumulatives = [
        [sum(p[: i + 1]) for i in range(n_b)] for p in profiles
    ]
    max_std = 0.0
    for i in range(n_b):
        mu = sum(c[i] for c in cumulatives) / n
        variance = sum((c[i] - mu) ** 2 for c in cumulatives) / (n - 1)
        std = math.sqrt(variance)
        if std > max_std:
            max_std = std

    consistency = max(0.0, 1.0 - 2.0 * max_std)
    return round(completeness * consistency, 4)


class CurveStore:
    """Persists per-cycle power curves and computes averaged profiles + confidence."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(hass, _STORAGE_VERSION, f"{_STORAGE_KEY}.{entry_id}")
        # {device_slug: {cycle_id: {"profiles": [...], "captured_at": [...]}}}
        self._data: Dict = {}
        # track which cycles currently have active warnings (to avoid repeat notifications)
        self._warned: set[tuple[str, str]] = set()

    async def async_load(self) -> None:
        raw = await self._store.async_load() or {}
        self._data = raw
        if self._sanitize_short_curves():
            await self._store.async_save(self._data)

    def _sanitize_short_curves(self) -> bool:
        """Drop already-stored aborted runs (duration far below the cycle median).

        Applies the same guard as async_add_curve to historical data captured
        before the guard existed. Returns True if anything was removed.
        """
        changed = False
        for slug, cycles in self._data.items():
            for cid, entry in cycles.items():
                if cid == "_" or not isinstance(entry, dict) or "profiles" not in entry:
                    continue
                durs = entry.get("durations_s", [])
                valid = [d for d in durs if d > 0]
                if len(valid) < CURVE_MIN_CURVES:
                    continue
                floor = _median(valid) * CURVE_MIN_DURATION_FRACTION
                keep = [i for i, d in enumerate(durs) if not (0 < d < floor)]
                if len(keep) == len(durs):
                    continue
                caps = entry.get("captured_at", [])
                entry["profiles"]    = [entry["profiles"][i] for i in keep]
                entry["captured_at"] = [caps[i] for i in keep if i < len(caps)]
                entry["durations_s"] = [durs[i] for i in keep]
                changed = True
                _LOGGER.warning(
                    "SmartStart: dropped %d aborted curve(s) from %s/%s on load",
                    len(durs) - len(keep), slug, cid,
                )
        return changed

    async def async_add_curve(
        self,
        device_slug: str,
        cycle_id: str,
        samples: list,
        captured_at: str,
    ) -> bool:
        """Learn one run's curve. Returns False if it was rejected."""
        # Derive actual duration from the sample timestamps before normalising
        actual_duration_s = (
            float(samples[-1]["t_s"]) - float(samples[0]["t_s"])
            if len(samples) >= 2 else 0.0
        )
        profile = _to_profile(samples)

        entry = (
            self._data
            .setdefault(device_slug, {})
            .setdefault(cycle_id, {"profiles": [], "captured_at": [], "durations_s": []})
        )
        # Back-fill durations_s if missing from older stored data
        entry.setdefault("durations_s", [])

        # Reject aborted/interrupted runs — a duration far below the cycle's own
        # median is not a representative curve and would pollute the average.
        # Only guard once we have a stable baseline (≥ CURVE_MIN_CURVES prior runs).
        prior = [d for d in entry["durations_s"] if d > 0]
        if len(prior) >= CURVE_MIN_CURVES:
            floor = _median(prior) * CURVE_MIN_DURATION_FRACTION
            if 0 < actual_duration_s < floor:
                _LOGGER.warning(
                    "SmartStart: skipping curve for %s/%s — duration %.1f min is below "
                    "%.0f%% of the %.1f-min median (aborted run?)",
                    device_slug, cycle_id, actual_duration_s / 60,
                    CURVE_MIN_DURATION_FRACTION * 100, _median(prior) / 60,
                )
                return False

        entry["profiles"].append(profile)
        entry["captured_at"].append(captured_at)
        entry["durations_s"].append(actual_duration_s)

        if len(entry["profiles"]) > _MAX_CURVES:
            entry["profiles"]    = entry["profiles"][-_MAX_CURVES:]
            entry["captured_at"] = entry["captured_at"][-_MAX_CURVES:]
            entry["durations_s"] = entry["durations_s"][-_MAX_CURVES:]

        await self._store.async_save(self._data)
        _LOGGER.info(
            "SmartStart: saved power curve for %s/%s — %d curves, last duration %.1f min",
            device_slug, cycle_id, len(entry["profiles"]), actual_duration_s / 60,
        )
        return True

    async def async_remove_curve(
        self, device_slug: str, cycle_id: str, captured_at: str
    ) -> bool:
        """Forget one learned run (matched by its captured_at stamp).

        Used when a run tagged from the panel is re-tagged or deleted.
        """
        entry = self._data.get(device_slug, {}).get(cycle_id)
        if not isinstance(entry, dict):
            return False
        caps = entry.get("captured_at", [])
        if captured_at not in caps:
            return False
        i = caps.index(captured_at)
        for key in ("profiles", "captured_at", "durations_s"):
            if i < len(entry.get(key, [])):
                entry[key].pop(i)
        await self._store.async_save(self._data)
        return True

    @property
    def raw_data(self) -> Dict:
        return self._data

    def get_profile(self, device_slug: str, cycle_id: str) -> Optional[list[float]]:
        profiles = self._data.get(device_slug, {}).get(cycle_id, {}).get("profiles", [])
        return _mean_profile(profiles) if profiles else None

    def get_duration_info(self, device_slug: str, cycle_id: str) -> dict:
        """Return avg/std actual run duration. None values until ≥1 run is recorded."""
        durations = self._data.get(device_slug, {}).get(cycle_id, {}).get("durations_s", [])
        # Filter out zero/invalid values (e.g. back-filled blanks from older data)
        valid = [d for d in durations if d > 0]
        return _duration_stats(valid)

    def get_avg_duration_minutes(self, device_slug: str, cycle_id: str) -> Optional[float]:
        """Return learned average duration in minutes, or None if not enough data."""
        entry     = self._data.get(device_slug, {}).get(cycle_id, {})
        n_curves  = len(entry.get("profiles", []))
        if n_curves < CURVE_MIN_CURVES:
            return None
        info = self.get_duration_info(device_slug, cycle_id)
        return info.get("avg_minutes")

    def get_confidence_info(self, device_slug: str, cycle_id: str) -> dict:
        entry    = self._data.get(device_slug, {}).get(cycle_id, {})
        profiles = entry.get("profiles", [])
        n        = len(profiles)
        conf     = _confidence(profiles)
        warning  = n >= CURVE_CONFIDENT_CURVES and conf < CURVE_WARN_THRESHOLD

        if n == 0:
            status = "waiting"
        elif n < CURVE_MIN_CURVES:
            status = "learning"
        elif warning:
            status = "inconsistent"
        else:
            status = "confident"

        mean = _mean_profile(profiles) if profiles else []
        last = entry.get("captured_at", [])
        dur  = self.get_duration_info(device_slug, cycle_id)

        return {
            "n_curves":          n,
            "confidence":        conf,
            "status":            status,
            "last_captured_at":  last[-1] if last else None,
            "warning":           warning,
            "profile_type":      _profile_type(mean) if mean else "unknown",
            "avg_duration_minutes": dur["avg_minutes"],
            "duration_std_minutes": dur["std_minutes"],
        }

    async def async_record_last_run(
        self,
        device_slug: str,
        cycle_id: str,
        cycle_name: str,
        kwh: float,
        cost: float,
        duration_minutes: float,
    ) -> None:
        """Record last-run stats and update accumulated totals at device level."""
        from datetime import datetime, timezone
        device_entry = self._data.setdefault(device_slug, {}).setdefault("_", {})

        # Last run — always overwritten
        device_entry["last_run"] = {
            "cycle_id":         cycle_id,
            "cycle_name":       cycle_name,
            "kwh":              round(kwh, 4),
            "cost":             round(cost, 6),
            "duration_minutes": round(duration_minutes, 1),
            "completed_at":     datetime.now(timezone.utc).isoformat(),
        }

        # Accumulated totals — grow forever
        device_entry["total_kwh"]       = round(device_entry.get("total_kwh", 0.0)       + kwh,  4)
        device_entry["total_cost"]      = round(device_entry.get("total_cost", 0.0)      + cost, 6)
        device_entry["total_minutes"]   = round(device_entry.get("total_minutes", 0.0)   + duration_minutes, 1)
        device_entry["total_run_count"] = device_entry.get("total_run_count", 0) + 1

        await self._store.async_save(self._data)
        _LOGGER.info(
            "SmartStart: %s — run: %s, %.4f kWh, £%.4f, %.1f min "
            "(totals: %d runs, %.3f kWh, £%.4f)",
            device_slug, cycle_name, kwh, cost, duration_minutes,
            device_entry["total_run_count"],
            device_entry["total_kwh"],
            device_entry["total_cost"],
        )

    def get_last_run(self, device_slug: str) -> dict:
        """Return last completed run stats for the device, or {} if none yet."""
        return self._data.get(device_slug, {}).get("_", {}).get("last_run", {})

    def get_totals(self, device_slug: str) -> dict:
        """Return accumulated totals across all runs for the device."""
        d = self._data.get(device_slug, {}).get("_", {})
        return {
            "total_kwh":       d.get("total_kwh"),
            "total_cost":      d.get("total_cost"),
            "total_minutes":   d.get("total_minutes"),
            "total_run_count": d.get("total_run_count", 0),
        }

    async def async_clear_curves(self, device_slug: str, cycle_key: str) -> None:
        """Drop all recorded power curves for one cycle. Last-run/totals untouched."""
        device = self._data.get(device_slug)
        if not device or cycle_key not in device:
            return
        device.pop(cycle_key, None)
        await self._store.async_save(self._data)
        _LOGGER.info("SmartStart: cleared learned curves for %s/%s", device_slug, cycle_key)

    async def async_clear_device_stats(self, device_slug: str) -> None:
        """Drop the device's last_run + accumulated totals. Curves untouched."""
        device = self._data.get(device_slug)
        if not device:
            return
        device["_"] = {}
        await self._store.async_save(self._data)
        _LOGGER.info("SmartStart: cleared last-run + totals for %s", device_slug)

    def iter_warnings(self):
        """Yield (device_slug, cycle_id, info) for cycles with active warnings."""
        for slug, cycles in self._data.items():
            for cid, entry in cycles.items():
                if cid == "_":  # skip device-level metadata key
                    continue
                if not isinstance(entry, dict) or "profiles" not in entry:
                    continue
                info = self.get_confidence_info(slug, cid)
                if info["warning"]:
                    yield slug, cid, info
