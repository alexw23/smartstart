"""Tests for run history, rhythm analysis, and run recording on idle."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.smartstart.const import EXTERNAL_CYCLE_KEY, STATE_RUNNING
from custom_components.smartstart.curve_store import CurveStore
from custom_components.smartstart.rhythm import cycle_rhythm, start_heatmap
from custom_components.smartstart.run_history import (
    MAX_SAMPLED_RUNS_PER_DEVICE,
    RunHistoryStore,
    compact_samples,
    rate_segments,
)
from custom_components.smartstart.scheduler import SmartStartScheduler

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def _store(cls, *args):
    with patch(f"{cls.__module__}.Store") as MockStore:
        s = MagicMock()
        s.async_load = AsyncMock(return_value=None)
        s.async_save = AsyncMock()
        MockStore.return_value = s
        return cls(MagicMock(), *args)


# ── rhythm ────────────────────────────────────────────────────────────────────

class TestRhythm:
    def test_too_few_runs_is_learning(self):
        r = cycle_rhythm([NOW - timedelta(days=3), NOW - timedelta(days=1)], NOW)
        assert r["status"] == "learning"
        assert r["next_due"] is None
        assert r["run_count"] == 2

    def test_weekly_cycle_predicts_next_week(self):
        starts = [NOW - timedelta(days=7 * k - 5) for k in range(5, 0, -1)]  # last 2 days ago
        r = cycle_rhythm(starts, NOW)
        assert r["interval_days"] == pytest.approx(7.0)
        assert r["regularity"] == pytest.approx(1.0)
        assert r["status"] == "upcoming"
        assert datetime.fromisoformat(r["next_due"]) == starts[-1] + timedelta(days=7)

    def test_overdue_after_tolerance(self):
        starts = [NOW - timedelta(days=d) for d in (12, 10, 8, 6)]  # every 2 days
        r = cycle_rhythm(starts, NOW)
        # due 2 days after the last (4 days ago) → 2 days late, tolerance 14.4h
        assert r["status"] == "overdue"

    def test_back_to_back_runs_do_not_shrink_interval(self):
        base = [NOW - timedelta(days=d) for d in (21, 14, 7)]
        starts = sorted(base + [b + timedelta(hours=2) for b in base])
        r = cycle_rhythm(starts, NOW)
        assert r["interval_days"] == pytest.approx(7 - 2 / 24, abs=0.01)

    def test_irregular_has_no_due_date(self):
        starts = [NOW - timedelta(days=d) for d in (40, 39, 25, 24.5, 3, 1)]
        r = cycle_rhythm(starts, NOW)
        assert r["status"] == "irregular"
        assert r["next_due"] is None

    def test_runs_per_week_only_counts_cycle_lifetime(self):
        starts = [NOW - timedelta(days=d) for d in (13, 6)]
        r = cycle_rhythm(starts, NOW)
        # 2 runs over ~1.9 weeks of existence, not over 8 weeks
        assert r["runs_per_week"] == pytest.approx(2 / (13 / 7), abs=0.01)

    def test_typical_hour_wraps_midnight(self):
        starts = [NOW.replace(hour=23), NOW.replace(hour=1) + timedelta(days=1)]
        r = cycle_rhythm(starts, NOW + timedelta(days=2))
        assert r["typical_hour"] in (0.0, 24.0) or r["typical_hour"] < 0.01 or r["typical_hour"] > 23.99

    def test_heatmap_counts_weekday_hour(self):
        grid = start_heatmap([NOW, NOW, NOW + timedelta(days=1, hours=3)])
        assert grid[NOW.weekday()][12] == 2
        assert grid[(NOW.weekday() + 1) % 7][15] == 1


# ── run history ───────────────────────────────────────────────────────────────

class TestSampleHelpers:
    def test_compact_samples(self):
        out = compact_samples([{"t_s": 59.6, "power_w": 1234.56, "wall_ts": "x"}])
        assert out == [[60, 1234.6]]

    def test_rate_segments_only_on_change(self):
        t0 = NOW
        samples = [
            {"t_s": i * 60, "power_w": 1, "wall_ts": (t0 + timedelta(minutes=i)).isoformat()}
            for i in range(60)
        ]
        rate_at = lambda w: 0.10 if w < t0 + timedelta(minutes=30) else 0.20
        assert rate_segments(samples, rate_at) == [[0, 0.1], [1800, 0.2]]

    def test_rate_segments_without_lookup(self):
        assert rate_segments([{"t_s": 0, "power_w": 1, "wall_ts": NOW.isoformat()}], None) == []


class TestRunHistoryStore:
    async def test_seed_from_curves_once(self):
        h = _store(RunHistoryStore, "e1")
        await h.async_load()
        curves = {
            "washer": {
                "_": {"total_run_count": 3},
                "eco": {
                    "profiles": [[1.0], [1.0]],
                    "captured_at": [NOW.isoformat(), (NOW + timedelta(days=2)).isoformat()],
                    "durations_s": [3600, 0],
                },
            },
        }
        await h.async_seed_from_curves(curves)
        runs = h.runs("washer")
        assert len(runs) == 2
        assert runs[0]["started_at"] == (NOW - timedelta(hours=1)).isoformat()
        assert runs[0]["learned"] and runs[0]["source"] == "seed"
        await h.async_seed_from_curves(curves)  # idempotent
        assert len(h.runs("washer")) == 2

    async def test_prune_drops_old_samples(self):
        h = _store(RunHistoryStore, "e1")
        await h.async_load()
        for i in range(MAX_SAMPLED_RUNS_PER_DEVICE + 2):
            await h.async_add_run({
                "device_slug": "w", "cycle_key": "eco",
                "started_at": f"2026-01-01T00:{i:04d}", "samples": [[0, 1]],
            })
        runs = h.runs("w")
        assert "samples" not in runs[0] and "samples" not in runs[1]
        assert runs[2]["samples"]

    async def test_update_and_delete(self):
        h = _store(RunHistoryStore, "e1")
        await h.async_load()
        rec = await h.async_add_run({"device_slug": "w", "cycle_key": EXTERNAL_CYCLE_KEY, "started_at": "x"})
        await h.async_update(rec["id"], cycle_key="eco")
        assert h.get(rec["id"])["cycle_key"] == "eco"
        assert await h.async_delete(rec["id"])
        assert h.get(rec["id"]) is None
        assert not await h.async_delete(rec["id"])


class TestCurveRemoval:
    async def test_remove_curve_by_captured_at(self):
        cs = _store(CurveStore, "e1")
        samples = [{"t_s": i * 60.0, "power_w": 500.0} for i in range(10)]
        assert await cs.async_add_curve("w", "eco", samples, "a")
        assert await cs.async_add_curve("w", "eco", samples, "b")
        assert await cs.async_remove_curve("w", "eco", "a")
        assert cs.raw_data["w"]["eco"]["captured_at"] == ["b"]
        assert len(cs.raw_data["w"]["eco"]["profiles"]) == 1
        assert not await cs.async_remove_curve("w", "eco", "zzz")


# ── scheduler integration ─────────────────────────────────────────────────────

class TestSchedulerRecordsRuns:
    async def test_idle_records_run_with_trace_and_baseline(self):
        hass = MagicMock()
        device = {
            "device_slug": "washer", "name": "Washer", "power_sensor": "sensor.p",
            "cycles": [{"cycle_key": "eco", "name": "Eco", "duration_minutes": 60, "total_kwh": 1}],
        }
        with patch("custom_components.smartstart.scheduler.Store") as MockStore:
            st = MagicMock(); st.async_save = AsyncMock(); MockStore.return_value = st
            curve_store = MagicMock()
            curve_store.async_add_curve = AsyncMock(return_value=True)
            curve_store.async_record_last_run = AsyncMock()
            sched = SmartStartScheduler(hass, [device], curve_store)
        history = MagicMock(); history.async_add_run = AsyncMock()
        sched.set_run_history(history)
        sched.set_rate_lookup(lambda _w: 0.10)

        started = datetime.now(timezone.utc) - timedelta(minutes=30)
        samples = [
            {"t_s": i * 60.0, "power_w": 2000.0,
             "wall_ts": (started + timedelta(minutes=i)).isoformat()}
            for i in range(31)
        ]
        recorder = MagicMock()
        recorder.stop.return_value = samples
        recorder.paused_total_seconds = 0
        sched._recorders["washer"] = recorder
        sched._schedules["washer"] = {
            "status": STATE_RUNNING, "cycle_id": "eco", "triggered_by": "smartstart",
            "running_since": started.isoformat(),
            "scheduled_at": (started - timedelta(hours=2)).isoformat(),
            "rate_cost_per_kwh": 0.10, "now_rate_cost_per_kwh": 0.30,
        }
        await sched.async_set_idle("washer")

        rec = history.async_add_run.call_args[0][0]
        assert rec["cycle_key"] == "eco"
        assert rec["kwh"] == pytest.approx(1.0, abs=0.01)
        assert rec["cost"] == pytest.approx(0.10, abs=0.002)
        assert rec["baseline_cost"] == pytest.approx(0.30, abs=0.005)
        assert rec["delay_minutes"] == pytest.approx(120, abs=0.1)
        assert rec["learned"] is True
        assert len(rec["samples"]) == 31
        assert rec["rates"] == [[0, 0.1]]
