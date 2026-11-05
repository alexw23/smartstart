"""Per-run history: one record per completed cycle, with its power curve.

CurveStore only keeps what the scheduler needs (normalised profiles, durations,
last run + running totals). The sidebar panel needs the runs themselves — when
each ran, what it cost, its real power trace — so it can list them, draw the
curve, tag Unknown (externally started) runs after the fact, and work out how
often each cycle is run.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_STORAGE_KEY     = f"{DOMAIN}_run_history"
_STORAGE_VERSION = 1
MAX_RUNS_PER_DEVICE         = 500  # records (tiny without samples)
MAX_SAMPLED_RUNS_PER_DEVICE = 200  # older runs drop their power trace


def compact_samples(samples: list) -> list:
    """[{t_s, power_w, wall_ts}] → [[t_s, power_w]] rounded, for storage."""
    return [
        [round(float(s["t_s"])), round(float(s["power_w"]), 1)]
        for s in samples
    ]


def rate_segments(samples: list, rate_at: Optional[Callable]) -> list:
    """[[t_s, rate]] — the tariff in effect, one entry per change.

    Stored with the run because past rate slots fall out of the provider's
    window within a day or two, and the panel draws the rate under the curve.
    """
    if not rate_at:
        return []
    out: list = []
    for s in samples:
        ts = s.get("wall_ts")
        if not ts:
            continue
        try:
            rate = rate_at(datetime.fromisoformat(ts))
        except ValueError:
            continue
        if rate is None:
            continue
        rate = round(float(rate), 4)
        if not out or out[-1][1] != rate:
            out.append([round(float(s["t_s"])), rate])
    return out


class RunHistoryStore:
    """Persists completed runs for every device in one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(hass, _STORAGE_VERSION, f"{_STORAGE_KEY}.{entry_id}")
        self._runs: List[Dict] = []
        self._seeded = False

    async def async_load(self) -> None:
        raw = await self._store.async_load() or {}
        self._runs = raw.get("runs", [])
        self._seeded = bool(raw.get("seeded"))

    async def _save(self) -> None:
        await self._store.async_save({"runs": self._runs, "seeded": self._seeded})

    async def async_seed_from_curves(self, curve_data: Dict) -> None:
        """One-shot backfill from CurveStore's captured_at/durations.

        Learned curves are the only record of runs from before this store
        existed. They carry no kWh/cost/trace, but their timestamps are enough
        to give each cycle a rhythm from day one.
        """
        if self._seeded:
            return
        self._seeded = True
        seeded = 0
        for slug, cycles in curve_data.items():
            for cid, entry in cycles.items():
                if cid == "_" or not isinstance(entry, dict):
                    continue
                caps = entry.get("captured_at", [])
                durs = entry.get("durations_s", [])
                for i, cap in enumerate(caps):
                    try:
                        ended = datetime.fromisoformat(cap)
                    except (TypeError, ValueError):
                        continue
                    dur_s = durs[i] if i < len(durs) and durs[i] else 0.0
                    self._runs.append({
                        "id":               uuid.uuid4().hex,
                        "device_slug":      slug,
                        "cycle_key":        cid,
                        "started_at":       (ended - timedelta(seconds=dur_s)).isoformat(),
                        "ended_at":         ended.isoformat(),
                        "duration_minutes": round(dur_s / 60.0, 1) if dur_s else None,
                        "kwh":              None,
                        "cost":             None,
                        "triggered_by":     "smartstart",
                        "source":           "seed",
                        "learned":          True,
                    })
                    seeded += 1
        self._runs.sort(key=lambda r: r["started_at"])
        await self._save()
        if seeded:
            _LOGGER.info("SmartStart: seeded run history with %d learned runs", seeded)

    async def async_add_run(self, record: Dict) -> Dict:
        record = {"id": uuid.uuid4().hex, "source": "recorded", **record}
        self._runs.append(record)
        self._prune(record["device_slug"])
        await self._save()
        return record

    def _prune(self, slug: str) -> None:
        mine = [r for r in self._runs if r["device_slug"] == slug]
        if len(mine) > MAX_RUNS_PER_DEVICE:
            drop = {r["id"] for r in mine[: len(mine) - MAX_RUNS_PER_DEVICE]}
            self._runs = [r for r in self._runs if r["id"] not in drop]
            mine = [r for r in mine if r["id"] not in drop]
        sampled = [r for r in mine if r.get("samples")]
        for r in sampled[: max(0, len(sampled) - MAX_SAMPLED_RUNS_PER_DEVICE)]:
            r.pop("samples", None)
            r.pop("rates", None)

    def runs(self, device_slug: Optional[str] = None) -> List[Dict]:
        """Runs oldest-first (shared references — copy before mutating)."""
        if device_slug is None:
            return list(self._runs)
        return [r for r in self._runs if r["device_slug"] == device_slug]

    def get(self, run_id: str) -> Optional[Dict]:
        return next((r for r in self._runs if r["id"] == run_id), None)

    async def async_update(self, run_id: str, **changes) -> Optional[Dict]:
        run = self.get(run_id)
        if run is None:
            return None
        run.update(changes)
        await self._save()
        return run

    async def async_delete(self, run_id: str) -> bool:
        before = len(self._runs)
        self._runs = [r for r in self._runs if r["id"] != run_id]
        if len(self._runs) == before:
            return False
        await self._save()
        return True
