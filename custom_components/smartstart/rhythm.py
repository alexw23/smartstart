"""How often each cycle runs, and when it's next likely to be wanted.

Pure functions over run start times (tz-aware, already in local time so
hour-of-day / weekday read naturally). No HA dependencies.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional

RHYTHM_MIN_RUNS = 3          # intervals need ≥2 gaps before we call it a rhythm
RHYTHM_RECENT_INTERVALS = 8  # habits drift — only the latest gaps count
RHYTHM_WEEKS = 8             # runs/week averaged over this many weeks
# Gaps shorter than this are "same session" (e.g. two loads back to back) and
# would drag the median interval toward zero.
RHYTHM_MIN_GAP_HOURS = 6


def _median(values: List[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _typical_hour(starts: List[datetime]) -> Optional[float]:
    """Circular mean of start hour (23:00 and 01:00 average to midnight)."""
    if not starts:
        return None
    x = sum(math.cos(2 * math.pi * (s.hour + s.minute / 60) / 24) for s in starts)
    y = sum(math.sin(2 * math.pi * (s.hour + s.minute / 60) / 24) for s in starts)
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return None  # spread evenly round the clock — no typical hour
    return round((math.atan2(y, x) * 24 / (2 * math.pi)) % 24, 2)


def due_tolerance(interval_days: float) -> timedelta:
    """How far either side of the due date still counts as "on rhythm"."""
    return timedelta(hours=max(12.0, interval_days * 24 * 0.3))


def cycle_rhythm(starts: List[datetime], now: datetime) -> Dict:
    """Summarise a cycle's run cadence.

    status: learning (too few runs) | irregular (no stable interval) |
            upcoming | due | overdue
    """
    starts = sorted(starts)
    n = len(starts)
    weeks_ago = now - timedelta(weeks=RHYTHM_WEEKS)
    recent = [s for s in starts if s >= weeks_ago]
    # Only average over weeks the cycle has existed, so a cycle first run a
    # fortnight ago isn't reported as once every four weeks.
    span_weeks = RHYTHM_WEEKS
    if starts and starts[0] > weeks_ago:
        span_weeks = max(1.0, (now - starts[0]).total_seconds() / 604800)
    out: Dict = {
        "run_count":        n,
        "last_run":         starts[-1].isoformat() if starts else None,
        "runs_per_week":    round(len(recent) / span_weeks, 2) if starts else 0.0,
        "typical_hour":     _typical_hour(starts[-20:]),
        "weekday_counts":   [0] * 7,
        "interval_days":    None,
        "regularity":       None,
        "next_due":         None,
        "status":           "learning",
    }
    for s in starts:
        out["weekday_counts"][s.weekday()] += 1

    gaps = [
        (b - a).total_seconds() / 86400
        for a, b in zip(starts, starts[1:])
        if (b - a) >= timedelta(hours=RHYTHM_MIN_GAP_HOURS)
    ][-RHYTHM_RECENT_INTERVALS:]
    if n < RHYTHM_MIN_RUNS or len(gaps) < 2:
        return out

    interval = _median(gaps)
    # Regularity: 1 − (median absolute deviation / median), clipped to 0–1.
    # Robust to the odd holiday gap that a std-dev would be dominated by.
    mad = _median([abs(g - interval) for g in gaps])
    regularity = max(0.0, min(1.0, 1.0 - mad / interval)) if interval > 0 else 0.0
    out["interval_days"] = round(interval, 2)
    out["regularity"] = round(regularity, 2)
    if regularity < 0.3:
        out["status"] = "irregular"
        return out

    due = starts[-1] + timedelta(days=interval)
    tol = due_tolerance(interval)
    out["next_due"] = due.isoformat()
    if now > due + tol:
        out["status"] = "overdue"
    elif now >= due - tol:
        out["status"] = "due"
    else:
        out["status"] = "upcoming"
    return out


def start_heatmap(starts: List[datetime]) -> List[List[int]]:
    """7×24 counts of run starts (Mon=0 … Sun=6, by local hour)."""
    grid = [[0] * 24 for _ in range(7)]
    for s in starts:
        grid[s.weekday()][s.hour] += 1
    return grid
