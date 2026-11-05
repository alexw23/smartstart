"""Tests for curve_store.py — pure functions, no HA fixtures needed."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.smartstart.curve_store import (
    CurveStore,
    _confidence,
    _duration_stats,
    _mean_profile,
    _profile_type,
    _to_profile,
)
from custom_components.smartstart.const import CURVE_N_BUCKETS


# ── _to_profile ───────────────────────────────────────────────────────────────

class TestToProfile:
    def test_sums_to_one(self):
        samples = [{"t_s": i * 60.0, "power_w": 1000.0} for i in range(10)]
        profile = _to_profile(samples)
        assert abs(sum(profile) - 1.0) < 1e-9

    def test_correct_number_of_buckets(self):
        samples = [{"t_s": i * 60.0, "power_w": 500.0} for i in range(20)]
        profile = _to_profile(samples, n_buckets=10)
        assert len(profile) == 10

    def test_flat_signal_gives_uniform_profile(self):
        samples = [{"t_s": i * 60.0, "power_w": 1000.0} for i in range(20)]
        profile = _to_profile(samples, n_buckets=4)
        expected = 1.0 / 4
        for bucket in profile:
            assert abs(bucket - expected) < 0.02  # within 2%

    def test_spike_at_start_loads_first_buckets(self):
        # Very high power for first 20% of cycle, then low
        samples = (
            [{"t_s": i * 10.0, "power_w": 3000.0} for i in range(4)]  # first 40s high
            + [{"t_s": 40.0 + i * 50.0, "power_w": 100.0} for i in range(8)]  # rest low
        )
        profile = _to_profile(samples, n_buckets=4)
        # First bucket should have more energy than last
        assert profile[0] > profile[-1]

    def test_spike_at_end_loads_last_buckets(self):
        samples = (
            [{"t_s": i * 50.0, "power_w": 100.0} for i in range(8)]
            + [{"t_s": 400.0 + i * 10.0, "power_w": 3000.0} for i in range(4)]
        )
        profile = _to_profile(samples, n_buckets=4)
        assert profile[-1] > profile[0]

    def test_fewer_than_two_samples_returns_uniform(self):
        assert _to_profile([{"t_s": 0, "power_w": 1000}]) == [
            1.0 / CURVE_N_BUCKETS
        ] * CURVE_N_BUCKETS

    def test_zero_duration_returns_uniform(self):
        samples = [{"t_s": 0.0, "power_w": 1000.0}, {"t_s": 0.0, "power_w": 1000.0}]
        profile = _to_profile(samples)
        assert abs(sum(profile) - 1.0) < 1e-9

    def test_all_zero_power_returns_uniform(self):
        samples = [{"t_s": i * 60.0, "power_w": 0.0} for i in range(5)]
        profile = _to_profile(samples)
        assert abs(sum(profile) - 1.0) < 1e-9


# ── _confidence ───────────────────────────────────────────────────────────────

class TestConfidence:
    def test_no_profiles_returns_zero(self):
        assert _confidence([]) == 0.0

    def test_one_profile_returns_half_of_completeness(self):
        # 1 curve / CURVE_CONFIDENT_CURVES(5) = 0.2 completeness → 0.2 * 0.5 = 0.1
        profile = [1.0 / 20] * 20
        conf = _confidence([profile])
        assert 0.0 < conf <= 0.5

    def test_identical_profiles_give_high_confidence(self):
        profile = [1.0 / 20] * 20
        conf = _confidence([list(profile)] * 5)
        assert conf >= 0.9

    def test_very_different_profiles_give_low_confidence(self):
        # Front-loaded vs back-loaded — maximum disagreement
        p_front = [0.1 if i < 10 else 0.0 for i in range(20)]
        p_back  = [0.0 if i < 10 else 0.1 for i in range(20)]
        conf = _confidence([p_front, p_back] * 3)
        assert conf < 0.3

    def test_confidence_increases_with_more_curves(self):
        profile = [1.0 / 20] * 20
        conf_2 = _confidence([list(profile)] * 2)
        conf_5 = _confidence([list(profile)] * 5)
        assert conf_5 > conf_2

    def test_near_zero_tail_noise_does_not_tank_confidence(self):
        # A heated cycle: energy mass fixed up front, then a long low-power tail
        # that jiggles run-to-run (the real washer pattern). The mass is
        # repeatable, so confidence must stay high even though the tail buckets
        # have huge per-bucket CV — the failure mode the old metric had.
        profiles = []
        for k in range(6):
            p = [0.0] * 20
            p[0], p[1], p[2] = 0.20, 0.45, 0.15   # stable heater spike, 80% of energy
            for i in range(6, 17):                 # noisy near-zero wash/rinse tail
                p[i] = 0.01 + 0.008 * ((k + i) % 3)
            p[18] = 0.05
            s = sum(p)
            profiles.append([v / s for v in p])
        conf = _confidence(profiles)
        assert conf > 0.7

    def test_bimodal_energy_location_scores_low(self):
        # Half the runs dump energy early, half late — the mass location is
        # genuinely unpredictable, so this must NOT be trusted for scheduling.
        early = [0.2 if i < 5 else 0.0 for i in range(20)]
        late  = [0.0 if i < 15 else 0.2 for i in range(20)]
        conf = _confidence([early, late] * 3)
        assert conf < 0.4


# ── _profile_type ─────────────────────────────────────────────────────────────

class TestProfileType:
    def test_uniform_profile_is_flat(self):
        uniform = [1.0 / 20] * 20
        assert _profile_type(uniform) == "flat"

    def test_heavily_front_loaded_is_dynamic(self):
        spike = [0.0] * 20
        spike[0] = 0.7
        spike[1] = 0.3
        assert _profile_type(spike) == "dynamic"

    def test_empty_returns_unknown(self):
        assert _profile_type([]) == "unknown"

    def test_slight_variation_still_flat(self):
        # Small natural variation around uniform — should still be "flat"
        profile = [0.05 + (0.001 * (i % 3 - 1)) for i in range(20)]
        # Normalise
        total = sum(profile)
        profile = [v / total for v in profile]
        assert _profile_type(profile) == "flat"


# ── _duration_stats ───────────────────────────────────────────────────────────

class TestDurationStats:
    def test_single_value(self):
        stats = _duration_stats([3600.0])  # 60 min
        assert stats["avg_minutes"] == pytest.approx(60.0)
        assert stats["std_minutes"] == pytest.approx(0.0)

    def test_multiple_values(self):
        stats = _duration_stats([3600.0, 5400.0, 4500.0])  # 60, 90, 75 min
        assert stats["avg_minutes"] == pytest.approx(75.0)
        assert stats["std_minutes"] > 0

    def test_empty_returns_none(self):
        stats = _duration_stats([])
        assert stats["avg_minutes"] is None
        assert stats["std_minutes"] is None

    def test_consistent_durations_have_low_std(self):
        # Dryer with consistent load
        consistent = [5400.0] * 5  # always 90 min
        stats = _duration_stats(consistent)
        assert stats["std_minutes"] == pytest.approx(0.0)

    def test_variable_durations_have_high_std(self):
        # Dryer with heavy vs light loads: 45 min vs 2.5 hours
        variable = [2700.0, 9000.0, 2700.0, 9000.0]
        stats = _duration_stats(variable)
        assert stats["std_minutes"] > 30  # substantial variation


# ── CurveStore (with mocked Store) ───────────────────────────────────────────

@pytest.fixture
def curve_store(mock_hass):
    with patch("custom_components.smartstart.curve_store.Store") as MockStore:
        store_instance = MagicMock()
        store_instance.async_load = AsyncMock(return_value=None)
        store_instance.async_save = AsyncMock()
        MockStore.return_value = store_instance
        cs = CurveStore(mock_hass, "test_entry_id")
    return cs


@pytest.fixture
def mock_hass():
    return MagicMock()


class TestCurveStore:
    async def test_load_empty(self, curve_store):
        await curve_store.async_load()
        assert curve_store.get_confidence_info("device", "cycle")["n_curves"] == 0

    async def test_add_curve_increments_count(self, curve_store):
        await curve_store.async_load()
        samples = [{"t_s": i * 60.0, "power_w": 1000.0} for i in range(15)]
        await curve_store.async_add_curve("device", "cycle", samples, "2026-01-01T00:00:00Z")
        info = curve_store.get_confidence_info("device", "cycle")
        assert info["n_curves"] == 1

    async def test_status_progression(self, curve_store):
        await curve_store.async_load()
        samples = [{"t_s": i * 60.0, "power_w": 1000.0} for i in range(15)]

        info = curve_store.get_confidence_info("device", "cycle")
        assert info["status"] == "waiting"

        # Add curves up to MIN_CURVES - 1
        for i in range(2):
            await curve_store.async_add_curve("device", "cycle", samples, f"2026-01-0{i+1}T00:00:00Z")
        info = curve_store.get_confidence_info("device", "cycle")
        assert info["status"] == "learning"

        # Add more to reach confident territory (5 total identical curves → high confidence)
        for i in range(3):
            await curve_store.async_add_curve("device", "cycle", samples, f"2026-01-0{i+3}T00:00:00Z")
        info = curve_store.get_confidence_info("device", "cycle")
        assert info["status"] == "confident"

    async def test_avg_duration_not_available_below_min_curves(self, curve_store):
        await curve_store.async_load()
        samples = [{"t_s": i * 60.0, "power_w": 1000.0} for i in range(10)]
        await curve_store.async_add_curve("device", "cycle", samples, "2026-01-01T00:00:00Z")
        # Only 1 curve, below CURVE_MIN_CURVES=3
        assert curve_store.get_avg_duration_minutes("device", "cycle") is None

    async def test_avg_duration_available_after_min_curves(self, curve_store):
        await curve_store.async_load()
        # 90-min cycles (5400s)
        samples = [{"t_s": i * 360.0, "power_w": 1000.0} for i in range(16)]  # 15 * 360 = 5400s
        for i in range(3):
            await curve_store.async_add_curve("device", "cycle", samples, f"2026-01-0{i+1}T00:00:00Z")

        avg = curve_store.get_avg_duration_minutes("device", "cycle")
        assert avg is not None
        assert avg == pytest.approx(90.0, abs=1.0)

    async def test_profile_type_flat_for_constant_power(self, curve_store):
        await curve_store.async_load()
        samples = [{"t_s": i * 60.0, "power_w": 2000.0} for i in range(20)]
        for i in range(5):
            await curve_store.async_add_curve("device", "cycle", samples, f"2026-01-0{i+1}T00:00:00Z")

        info = curve_store.get_confidence_info("device", "cycle")
        assert info["profile_type"] == "flat"

    async def test_curves_capped_at_max(self, curve_store):
        """Storing more than _MAX_CURVES=20 should only keep the most recent 20."""
        await curve_store.async_load()
        samples = [{"t_s": i * 60.0, "power_w": 1000.0} for i in range(10)]
        for i in range(25):
            await curve_store.async_add_curve("device", "cycle", samples, f"2026-01-01T{i:02d}:00:00Z")

        info = curve_store.get_confidence_info("device", "cycle")
        assert info["n_curves"] == 20

    async def test_warning_flag_for_inconsistent_curves(self, curve_store):
        await curve_store.async_load()
        # Alternating front-loaded and back-loaded — should be inconsistent
        front = [{"t_s": i * 100.0, "power_w": 3000.0 if i < 3 else 100.0} for i in range(20)]
        back  = [{"t_s": i * 100.0, "power_w": 100.0 if i < 17 else 3000.0} for i in range(20)]
        for i in range(5):
            samples = front if i % 2 == 0 else back
            await curve_store.async_add_curve("device", "cycle", samples, f"2026-01-0{i+1}T00:00:00Z")

        info = curve_store.get_confidence_info("device", "cycle")
        # 5 curves with high variance → low confidence → warning
        if info["confidence"] < 0.25:
            assert info["warning"] is True

    async def test_aborted_run_rejected_from_curve_learning(self, curve_store):
        """A run far shorter than the cycle median must not be learned."""
        await curve_store.async_load()
        full = [{"t_s": i * 60.0, "power_w": 1000.0} for i in range(61)]  # 60 min
        for i in range(3):
            await curve_store.async_add_curve("device", "cycle", full, f"2026-01-0{i+1}T00:00:00Z")
        assert curve_store.get_confidence_info("device", "cycle")["n_curves"] == 3

        # 6-min aborted run (well under 50% of the 60-min median) — should be dropped
        aborted = [{"t_s": i * 60.0, "power_w": 1000.0} for i in range(7)]
        await curve_store.async_add_curve("device", "cycle", aborted, "2026-01-04T00:00:00Z")
        assert curve_store.get_confidence_info("device", "cycle")["n_curves"] == 3

    async def test_load_sanitizes_existing_short_curves(self, curve_store):
        """A previously-stored aborted run is purged on load."""
        good = _to_profile([{"t_s": i * 60.0, "power_w": 1000.0} for i in range(61)])
        junk = _to_profile([{"t_s": i * 60.0, "power_w": 1000.0} for i in range(7)])
        stored = {
            "device": {
                "cycle": {
                    "profiles":    [good, good, good, junk],
                    "captured_at": ["a", "b", "c", "d"],
                    "durations_s": [3600.0, 3600.0, 3600.0, 360.0],
                }
            }
        }
        curve_store._store.async_load = AsyncMock(return_value=stored)
        await curve_store.async_load()
        info = curve_store.get_confidence_info("device", "cycle")
        assert info["n_curves"] == 3
        curve_store._store.async_save.assert_awaited()
