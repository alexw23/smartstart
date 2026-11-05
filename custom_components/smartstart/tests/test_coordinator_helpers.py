"""Tests for pure coordinator helpers (no hass instance required)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.smartstart.coordinator import SmartStartCoordinator
from custom_components.smartstart.providers.base import RatePeriod


# 16:05 local on the 7th — the window between Octopus's current-day tail
# update and the next-day entity update.
NOW = datetime(2026, 7, 7, 16, 5, tzinfo=timezone.utc)


class TestHorizonCoversTomorrow:
    def test_horizon_ending_tonight_is_missing(self):
        end = datetime(2026, 7, 7, 23, 0, tzinfo=timezone.utc)
        assert SmartStartCoordinator._horizon_covers_tomorrow(end, NOW) is False

    def test_midnight_tail_still_counts_as_missing(self):
        """Octopus appends today's 23:00-24:00 tail before the next-day entity
        updates — a horizon ending exactly at midnight is NOT tomorrow's data."""
        end = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)
        assert SmartStartCoordinator._horizon_covers_tomorrow(end, NOW) is False

    def test_half_hour_past_midnight_still_missing(self):
        end = datetime(2026, 7, 8, 0, 30, tzinfo=timezone.utc)
        assert SmartStartCoordinator._horizon_covers_tomorrow(end, NOW) is False

    def test_real_next_day_publication_covers(self):
        """Agile next-day rates run to 23:00 tomorrow — well past threshold."""
        end = datetime(2026, 7, 8, 23, 0, tzinfo=timezone.utc)
        assert SmartStartCoordinator._horizon_covers_tomorrow(end, NOW) is True

    def test_threshold_is_3am_tomorrow(self):
        just_under = datetime(2026, 7, 8, 2, 59, tzinfo=timezone.utc)
        at_threshold = datetime(2026, 7, 8, 3, 0, tzinfo=timezone.utc)
        assert SmartStartCoordinator._horizon_covers_tomorrow(just_under, NOW) is False
        assert SmartStartCoordinator._horizon_covers_tomorrow(at_threshold, NOW) is True


def _bare_coordinator(real, predicted):
    c = object.__new__(SmartStartCoordinator)
    c._rates = real
    c._predicted_rates = predicted
    return c


def _slots(start, values, bands=None):
    out = []
    for i, v in enumerate(values):
        s = start + timedelta(minutes=30 * i)
        lo, hi = bands[i] if bands else (None, None)
        out.append(RatePeriod(s, s + timedelta(minutes=30), v, lo, hi))
    return out


class TestPredictedRateViews:
    def test_hold_view_prices_forecast_at_high_bound(self):
        real = _slots(NOW, [0.30] * 4)
        pred = _slots(real[-1].end, [0.10, 0.12], bands=[(0.05, 0.20), (0.06, 0.22)])
        c = _bare_coordinator(real, pred)
        tail = c._rates_with_predictions(NOW)[len(real):]
        assert [r.rate_cost_per_kwh for r in tail] == [0.20, 0.22]

    def test_hold_view_without_bands_uses_estimate(self):
        real = _slots(NOW, [0.30] * 4)
        pred = _slots(real[-1].end, [0.10])
        c = _bare_coordinator(real, pred)
        assert c._rates_with_predictions(NOW)[-1].rate_cost_per_kwh == 0.10

    def test_planner_view_keeps_estimate_and_caps_at_48h(self):
        real = _slots(NOW, [0.30] * 4)
        pred = _slots(real[-1].end, [0.10] * 120, bands=[(0.05, 0.20)] * 120)
        c = _bare_coordinator(real, pred)
        rates = c.planner_rates(NOW)
        assert rates[len(real)].rate_cost_per_kwh == 0.10
        assert rates[-1].end <= NOW + timedelta(hours=48)

    def test_best_windows_info_flags_confirmed_vs_forecast(self):
        real = _slots(NOW, [0.30] * 8 + [0.10] * 4)        # 6h real, cheap at 4-6h
        pred = _slots(real[-1].end, [0.30] * 20 + [0.02] * 4 + [0.30] * 8,
                      bands=[(0.0, 0.40)] * 32)
        c = _bare_coordinator(real, pred)
        rows = c.best_windows_info(NOW, duration_minutes=120, kwh=2.0)
        assert rows[0]["confirmed"] is False                # forecast 2p spell wins
        assert rows[0]["rate"] == 0.02 and rows[0]["cost"] == 0.04
        assert rows[0]["rate_low"] <= rows[0]["rate"] <= rows[0]["rate_high"]
        assert any(r["confirmed"] for r in rows)            # the real 10p spell

