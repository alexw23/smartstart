"""Tests for laundry_plan.py — pure Python, no HA fixtures needed."""
from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.smartstart.laundry_plan import (
    MACHINE_DRY,
    MACHINE_WASH,
    PlanStep,
    build_composite_profile,
    derive_offsets,
    plan_total_duration,
    plan_total_kwh,
    unblocked_steps,
    validate_plan,
)
from custom_components.smartstart.window_scorer import score_windows
from .conftest import make_rates, MIDNIGHT


def W(dur=60, kwh=0.8, confirm=True, name="Wash", profile=None):
    return PlanStep(MACHINE_WASH, "wash_cycle", name, dur, kwh, confirm, profile)


def D(dur=120, kwh=2.5, confirm=True, name="Dry", profile=None):
    return PlanStep(MACHINE_DRY, "dry_cycle", name, dur, kwh, confirm, profile)


# ── derive_offsets ────────────────────────────────────────────────────────────

class TestDeriveOffsets:
    def test_simple_wash_dry(self):
        # Dry waits for the wash, plus the changeover gap.
        offsets = derive_offsets([W(60), D(120)], gap_minutes=15)
        assert offsets == [0, 75]

    def test_first_step_never_pays_gap(self):
        offsets = derive_offsets([W(60, confirm=True)], gap_minutes=15)
        assert offsets == [0]

    def test_auto_confirm_step_skips_gap(self):
        # Re-run dry chain: second dry fires the moment the first ends.
        offsets = derive_offsets([W(60), D(90), D(90, confirm=False)], gap_minutes=15)
        assert offsets == [0, 75, 165]

    def test_split_load_two_dries_queue(self):
        # [W, D, D] — both dries after the one wash, queued on the dryer.
        offsets = derive_offsets([W(60), D(90), D(90)], gap_minutes=15)
        assert offsets == [0, 75, 180]  # second dry: 75+90 ends 165, +15 gap

    def test_combine_two_washes_one_dry(self):
        # [W, W, D] — dry waits for BOTH washes (rule 2).
        offsets = derive_offsets([W(60), W(60), D(120)], gap_minutes=15)
        # wash2: washer free at 60, +gap = 75, ends 135. dry: 135+15 = 150.
        assert offsets == [0, 75, 150]

    def test_pipeline_two_loads(self):
        # [W, D, W, D] — wash 2 runs DURING dry 1.
        offsets = derive_offsets([W(60), D(120), W(60), D(120)], gap_minutes=15)
        # dry1 at 75 (ends 195). wash2: washer free at 60, +15 = 75 (ends 135).
        # dry2: dryer free 195, wash2 done 135 → max 195, +15 = 210.
        assert offsets == [0, 75, 75, 210]
        # Overlap check: wash2 (75–135) inside dry1 (75–195).
        assert offsets[2] < offsets[1] + 120

    def test_linen_sheets_flow(self):
        # Delicate (both sheets) → R&S top → Iron top ∥ R&S bottom → Iron bottom
        steps = [
            W(90, name="Delicate 800"),
            W(20, name="Rinse&Spin top"),
            D(60, name="Iron top"),
            W(20, name="Rinse&Spin bottom"),
            D(60, name="Iron bottom"),
        ]
        offsets = derive_offsets(steps, gap_minutes=10)
        # delicate 0–90. R&S top: 90+10=100, ends 120.
        # Iron top: washes 1,2 done at 120, +10 = 130, ends 190.
        # R&S bottom: washer free 120, +10 = 130 (same trip as iron top), ends 150.
        # Iron bottom: dryer free 190, wash3 done 150 → 190, +10 = 200.
        assert offsets == [0, 100, 130, 130, 200]

    def test_projection_seeds(self):
        # Mid-run: wash done at 70 (actual), project the remaining dry from there.
        offsets = derive_offsets(
            [D(120)],
            gap_minutes=15,
            dryer_free=0.0,
            prior_wash_ends=[70.0],
            first_step_immediate=False,
        )
        assert offsets == [85]  # 70 + 15 gap


# ── composite profile ─────────────────────────────────────────────────────────

class TestCompositeProfile:
    def test_fractions_sum_to_one(self):
        steps = [W(60, kwh=1.0), D(120, kwh=2.5)]
        offsets = derive_offsets(steps, gap_minutes=15)
        profile, total_dur, total_kwh = build_composite_profile(steps, offsets)
        assert total_dur == 195
        assert total_kwh == pytest.approx(3.5)
        assert sum(profile) == pytest.approx(1.0)

    def test_gap_buckets_are_empty(self):
        steps = [W(60, kwh=1.0), D(60, kwh=1.0)]
        offsets = derive_offsets(steps, gap_minutes=20)  # dry at 80
        profile, _, _ = build_composite_profile(steps, offsets, bucket_minutes=5.0)
        # Buckets covering 60–80 min (indices 12..15) are the gap.
        assert all(profile[b] == pytest.approx(0.0) for b in range(12, 16))

    def test_kwh_share_weighting(self):
        # Dryer carries 2.5/3.5 of the energy.
        steps = [W(60, kwh=1.0), D(120, kwh=2.5)]
        offsets = derive_offsets(steps, gap_minutes=15)
        profile, _, _ = build_composite_profile(steps, offsets, bucket_minutes=5.0)
        wash_share = sum(profile[: int(60 // 5)])
        dry_share  = sum(profile[int(75 // 5):])
        assert wash_share == pytest.approx(1.0 / 3.5, abs=0.01)
        assert dry_share == pytest.approx(2.5 / 3.5, abs=0.01)

    def test_overlapping_steps_sum(self):
        # Two steps fully overlapping: energy adds in shared buckets.
        steps = [W(60, kwh=1.0), D(60, kwh=1.0, confirm=False)]
        offsets = [0.0, 0.0]  # forced overlap (not derived — direct profile test)
        profile, total_dur, total_kwh = build_composite_profile(steps, offsets, bucket_minutes=5.0)
        assert total_dur == 60
        assert total_kwh == pytest.approx(2.0)
        assert sum(profile) == pytest.approx(1.0)
        # Uniform overlap → flat profile
        assert max(profile) == pytest.approx(min(profile), rel=0.01)

    def test_learned_step_profile_respected(self):
        # Step profile front-loads energy → composite front-loads too.
        steps = [W(60, kwh=1.0, profile=[0.75, 0.25])]
        profile, _, _ = build_composite_profile(steps, [0.0], bucket_minutes=30.0)
        assert profile[0] == pytest.approx(0.75)
        assert profile[1] == pytest.approx(0.25)

    def test_zero_kwh_plan_falls_back_uniform(self):
        steps = [W(60, kwh=0.0)]
        profile, _, total_kwh = build_composite_profile(steps, [0.0])
        assert total_kwh == 0.0
        assert sum(profile) == pytest.approx(1.0)


# ── end-to-end with the scorer ───────────────────────────────────────────────

class TestPlanScoring:
    def test_composite_prefers_dryer_cheap_slots(self):
        # 30-min slots. Expensive early, cheap late. A wash-only view would
        # start soon; composite scoring should shift so the DRYER (2.5 kWh)
        # lands in the cheap slots even if the wash pays a little more.
        rates = make_rates([0.10, 0.30, 0.30, 0.30, 0.30, 0.30, 0.05, 0.05, 0.05, 0.05, 0.30, 0.30])
        steps = [W(60, kwh=1.0), D(120, kwh=2.5)]
        offsets = derive_offsets(steps, gap_minutes=0)
        profile, total_dur, _ = build_composite_profile(steps, offsets)
        _, best, _ = score_windows(
            rates, MIDNIGHT,
            duration_minutes=total_dur,
            power_profile=profile,
            constraint_duration_minutes=steps[0].duration_minutes,
        )
        assert best is not None
        # Dry portion (offset 60–180 within plan) should cover the cheap block
        # starting at minute 180 (06:00 slot 6). Wash start = 180 - 60 = 120.
        dry_start = best.start + timedelta(minutes=60)
        assert dry_start >= MIDNIGHT + timedelta(minutes=180)

    def test_constraint_duration_end_anchor(self):
        # Whole-hour reservation washer (step=60, anchor=end): the WASH end
        # must land on now + k*60 even though the plan is longer.
        rates = make_rates([0.30, 0.30, 0.05, 0.05, 0.05, 0.05, 0.30, 0.30])
        steps = [W(60, kwh=1.0), D(60, kwh=2.0)]
        offsets = derive_offsets(steps, gap_minutes=0)
        profile, total_dur, _ = build_composite_profile(steps, offsets)
        _, best, _ = score_windows(
            rates, MIDNIGHT,
            duration_minutes=total_dur,
            power_profile=profile,
            delay_step_minutes=60,
            anchor="end",
            constraint_duration_minutes=60,
        )
        assert best is not None
        wash_end = best.start + timedelta(minutes=60)
        assert (wash_end - MIDNIGHT).total_seconds() % 3600 == 0


# ── unblocked_steps ───────────────────────────────────────────────────────────

class TestUnblocked:
    def test_initial_state_only_first_wash(self):
        steps = [W(), D(), W(), D()]
        assert unblocked_steps(steps, [False] * 4, [False] * 4) == [0]

    def test_after_wash1_dry1_and_wash2_unblock(self):
        steps = [W(), D(), W(), D()]
        done   = [True, False, False, False]
        active = [False] * 4
        assert unblocked_steps(steps, done, active) == [1, 2]

    def test_dry2_blocked_until_dry1_and_wash2_done(self):
        steps = [W(), D(), W(), D()]
        done   = [True, False, True, False]
        active = [False, True, False, False]  # dry1 running
        assert unblocked_steps(steps, done, active) == []

    def test_combine_dry_needs_both_washes(self):
        steps = [W(), W(), D()]
        assert unblocked_steps(steps, [True, False, False], [False] * 3) == [1]
        assert unblocked_steps(steps, [True, True, False], [False] * 3) == [2]

    def test_dryer_queue_cannot_be_jumped(self):
        # [W, D, D]: second dry blocked while first dry pending.
        steps = [W(), D(), D()]
        assert unblocked_steps(steps, [True, False, False], [False] * 3) == [1]


# ── validate_plan ─────────────────────────────────────────────────────────────

class TestValidate:
    def test_empty_plan_rejected(self):
        assert validate_plan([]) is not None

    def test_zero_duration_rejected(self):
        assert validate_plan([W(0)]) is not None

    def test_good_plan_passes(self):
        assert validate_plan([W(), D()]) is None

    def test_totals(self):
        steps = [W(60, kwh=1.0), D(120, kwh=2.5)]
        offsets = derive_offsets(steps, gap_minutes=15)
        assert plan_total_duration(steps, offsets) == 195
        assert plan_total_kwh(steps) == pytest.approx(3.5)


# ── blackouts: a later step waits out a power-down, splitting the plan ─────────

def _clear(*spans):
    """clear_of helper: spans are (start_min, end_min) offsets from plan start."""
    def fn(start, duration):
        for _ in range(len(spans) + 1):
            moved = False
            for b0, b1 in spans:
                if b0 < start + duration and start < b1:
                    start, moved = b1, True
            if not moved:
                break
        return start
    return fn


class TestBlackoutSplitting:
    def test_no_blackout_changes_nothing(self):
        steps = [W(60), D(120)]
        assert derive_offsets(steps, 10, clear_of=None) == derive_offsets(steps, 10)
        assert derive_offsets(steps, 10, clear_of=_clear()) == derive_offsets(steps, 10)

    def test_dry_waits_out_a_blackout(self):
        # Wash 0-60; dry would start at 70 (10 min changeover) and run 70-190.
        # Blackout 100-160 overlaps it, so the dry waits until 160.
        offsets = derive_offsets([W(60), D(120)], 10, clear_of=_clear((100, 160)))
        assert offsets == [0.0, 160.0]

    def test_blackout_in_the_changeover_gap_costs_nothing_extra(self):
        # Wash 0-60, blackout 60-130 sits in the gap; dry (70+) is delayed only
        # until the blackout ends at 130, then runs clear.
        offsets = derive_offsets([W(60), D(120)], 10, clear_of=_clear((60, 130)))
        assert offsets == [0.0, 130.0]

    def test_step_finishing_exactly_at_blackout_start_is_fine(self):
        # Dry 70-190 ends exactly as the blackout starts: no delay.
        offsets = derive_offsets([W(60), D(120)], 10, clear_of=_clear((190, 250)))
        assert offsets == [0.0, 70.0]

    def test_first_step_is_never_shifted(self):
        # The plan start isn't ours to move; callers reject that candidate.
        offsets = derive_offsets([W(60), D(120)], 10, clear_of=_clear((0, 30)))
        assert offsets[0] == 0.0

    def test_auto_firing_step_also_waits(self):
        # Dryer re-run with no changeover (confirm_before False) is fired by
        # SmartStart, so it can wait for the blackout too.
        offsets = derive_offsets([D(60), D(60, confirm=False)], 10, clear_of=_clear((60, 120)))
        assert offsets == [0.0, 120.0]

    def test_clear_of_and_changeover_settle_together(self):
        # Leaving the blackout (at 100) lands in "sleep" until 300; the dry
        # must satisfy both, so it ends up at 300, not 100.
        def asleep_until_300(off):
            return 300.0 if off < 300 else off
        offsets = derive_offsets(
            [W(60), D(60)], 10, changeover_at=asleep_until_300, clear_of=_clear((60, 100)),
        )
        assert offsets == [0.0, 300.0]

    def test_wet_wait_reflects_the_split(self):
        from custom_components.smartstart.laundry_plan import max_wet_wait
        steps = [W(60), D(120)]
        offsets = derive_offsets(steps, 10, clear_of=_clear((100, 160)))
        assert max_wet_wait(steps, offsets) == pytest.approx(100.0)  # washed at 60, dry at 160
