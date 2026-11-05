"""SmartStart sidebar panel: static JS, panel registration, websocket API.

The panel is a read-mostly view over every device entry: run history with
power curves, per-cycle stats and rhythm, recommended windows. Its only
writes are tagging/deleting runs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTRY_TYPE,
    CURVE_MIN_SAMPLES,
    DEFAULT_DELAY_HANDLING,
    DEFAULT_MIN_DELAY_MINUTES,
    DELAY_HANDLING_END_TIME,
    DOMAIN,
    ENTRY_TYPE_LAUNDRY,
    EXTERNAL_CYCLE_KEY,
    EXTERNAL_CYCLE_NAME,
    PLANNER_HORIZON_HOURS,
    ROW_NONE_LABEL,
)
from .rhythm import cycle_rhythm, due_tolerance, start_heatmap
from .window_scorer import find_window_for_deadline

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "smartstart"
PANEL_ELEMENT  = "smartstart-panel"
PANEL_JS_NAME  = "smartstart-panel.js"
PANEL_JS_URL   = f"/smartstart_panel/{PANEL_JS_NAME}"
_REGISTERED    = f"{DOMAIN}_panel_registered"
_WS_REGISTERED = f"{DOMAIN}_ws_registered"


# ── Registration ──────────────────────────────────────────────────────────────

async def async_setup_panel(hass: HomeAssistant) -> None:
    """Register WS commands + sidebar panel once per HA run."""
    if not hass.data.get(_WS_REGISTERED):
        for cmd in (
            ws_overview, ws_runs, ws_run, ws_windows, ws_tag_run, ws_delete_run,
            ws_laundry_continue, ws_laundry_set_repeat,
            ws_laundry_schedule, ws_laundry_accept_offer, ws_laundry_away,
            ws_laundry_start_now, ws_laundry_due,
            ws_laundry_flow_overnight, ws_laundry_sleep,
        ):
            websocket_api.async_register_command(hass, cmd)
        hass.data[_WS_REGISTERED] = True

    if hass.data.get(_REGISTERED):
        return
    # Claim before the first await: entries set up concurrently, and a second
    # static-path registration raises "method GET is already registered".
    hass.data[_REGISTERED] = True
    src = Path(__file__).parent / "frontend" / PANEL_JS_NAME
    try:
        from homeassistant.components import frontend
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(PANEL_JS_URL, str(src), False)]
        )
        mtime = await hass.async_add_executor_job(lambda: int(src.stat().st_mtime))
        frontend.async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title="SmartStart",
            sidebar_icon="mdi:lightning-bolt-circle",
            frontend_url_path=PANEL_URL_PATH,
            config={
                "_panel_custom": {
                    "name": PANEL_ELEMENT,
                    "module_url": f"{PANEL_JS_URL}?v={mtime}",
                    "embed_iframe": False,
                    "trust_external": False,
                }
            },
            require_admin=False,
        )
    except Exception:  # panel is optional — never fail entry setup over it
        hass.data.pop(_REGISTERED, None)
        _LOGGER.exception("SmartStart: sidebar panel registration failed")


# ── Data access ───────────────────────────────────────────────────────────────

def _device_stores(hass: HomeAssistant) -> List[tuple[Dict, Dict]]:
    """[(entry store, device cfg)] for every loaded device entry."""
    out = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        config = entry.options if entry.options else entry.data
        if config.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_LAUNDRY:
            continue
        store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not store or "coordinator" not in store:
            continue
        devices = store["coordinator"]._devices
        for device in devices:
            out.append((store, device))
    return out


def _find_device(hass: HomeAssistant, slug: str) -> tuple[Optional[Dict], Optional[Dict]]:
    for store, device in _device_stores(hass):
        if device["device_slug"] == slug:
            return store, device
    return None, None


def _find_run(hass: HomeAssistant, run_id: str) -> tuple[Optional[Dict], Optional[Dict]]:
    for entry_store in hass.data.get(DOMAIN, {}).values():
        history = entry_store.get("run_history") if isinstance(entry_store, dict) else None
        if history and (run := history.get(run_id)):
            return entry_store, run
    return None, None


def _cycle_name(device: Dict, key: str) -> str:
    if key == EXTERNAL_CYCLE_KEY:
        return EXTERNAL_CYCLE_NAME
    cyc = next((c for c in device.get("cycles", []) if c["cycle_key"] == key), None)
    return cyc["name"] if cyc else key


def _run_summary(run: Dict, device: Dict) -> Dict:
    out = {k: v for k, v in run.items() if k not in ("samples", "rates")}
    out["cycle_name"] = _cycle_name(device, run["cycle_key"])
    out["has_curve"] = bool(run.get("samples"))
    cost, base = run.get("cost"), run.get("baseline_cost")
    out["saved"] = round(base - cost, 4) if cost is not None and base is not None else None
    kwh = run.get("kwh")
    out["avg_rate"] = round(cost / kwh, 4) if cost is not None and kwh else None
    return out


def _scoring_params(store: Dict, device: Dict, cid: str) -> Optional[Dict]:
    """The same duration/profile/delay inputs the coordinator scored with."""
    coord = store["coordinator"]
    cdata = (coord.data or {}).get(device["device_slug"], {}).get("cycles", {}).get(cid)
    if not cdata:
        return None
    curve_store = store["curve_store"]
    profile = (
        curve_store.get_profile(device["device_slug"], cid)
        if cdata.get("profile_active") else None
    )
    delay_handling = device.get("delay_handling", DEFAULT_DELAY_HANDLING)
    return {
        "duration_minutes":   float(cdata["duration_minutes"]),
        "kwh":                float(cdata["cycle"].get("total_kwh") or 0.0),
        "profile":            profile,
        "min_delay_minutes":  int(device.get("min_delay_minutes", DEFAULT_MIN_DELAY_MINUTES)),
        "delay_step_minutes": int(device.get("delay_step_minutes", 1)),
        "anchor":             "end" if delay_handling == DELAY_HANDLING_END_TIME else "start",
    }


def _rhythm_suggestion(store: Dict, device: Dict, cid: str, rhythm: Dict, now: datetime) -> Optional[Dict]:
    """Cheapest window inside the cycle's due band, if rates reach it."""
    if not rhythm.get("next_due") or rhythm["status"] not in ("upcoming", "due", "overdue"):
        return None
    params = _scoring_params(store, device, cid)
    coord = store["coordinator"]
    rates = coord.planner_rates(now)
    if not params or not rates:
        return None
    due = datetime.fromisoformat(rhythm["next_due"])
    tol = due_tolerance(rhythm["interval_days"])
    earliest = max(now, due - tol)
    horizon_end = rates[-1].end
    if earliest + timedelta(minutes=params["duration_minutes"]) > horizon_end:
        return {"beyond_horizon": True, "earliest": earliest.isoformat()}
    deadline = min(due + tol, horizon_end)
    if rhythm["status"] == "overdue":
        # Already late — just the cheapest within the next day.
        deadline = min(now + timedelta(hours=24), horizon_end)
    min_delay = max(
        params["min_delay_minutes"],
        int((earliest - now).total_seconds() // 60),
    )
    win = find_window_for_deadline(
        rates, now, params["duration_minutes"], deadline,
        min_delay_minutes=min_delay,
        power_profile=params["profile"],
        delay_step_minutes=params["delay_step_minutes"],
        anchor=params["anchor"],
    )
    if win is None:
        return None
    return {
        "start":     win.start.isoformat(),
        "end":       win.end.isoformat(),
        "rate":      round(win.avg_rate_cost_per_kwh, 4),
        "cost":      round(win.avg_rate_cost_per_kwh * params["kwh"], 3),
        "predicted": coord.is_predicted_window(win),
    }


def _reach(sched: Dict, device: Dict) -> Optional[Dict]:
    """How far a scheduled run can still move without the user: latest start
    inside its committed envelope (a deadline or end-anchored cap bounds the
    finish, so subtract the run's length)."""
    if sched.get("status") != "scheduled" or not sched.get("reschedule_cap"):
        return None
    try:
        cap = datetime.fromisoformat(sched["reschedule_cap"])
        start = datetime.fromisoformat(sched["scheduled_start"])
        finish = datetime.fromisoformat(sched["estimated_finish"])
    except (KeyError, TypeError, ValueError):
        return None
    bounds_finish = (
        sched.get("reschedule_cap_kind") == "deadline"
        or device.get("delay_handling") == DELAY_HANDLING_END_TIME
    )
    latest = cap - (finish - start) if bounds_finish else cap
    return {
        "latest_start": latest.isoformat(),
        "cap": cap.isoformat(),
        "bounds_finish": bounds_finish,
        "auto": bool(device.get("auto_reschedule")),
    }


def _entity_ids(hass: HomeAssistant, kinds: Dict[str, tuple]) -> Dict[str, Optional[str]]:
    """{role: entity_id} from (domain, unique_id) — the panel drives the same
    entities as dashboards, so everything stays in sync."""
    from homeassistant.helpers import entity_registry as er
    reg = er.async_get(hass)
    return {
        role: reg.async_get_entity_id(domain, DOMAIN, uid)
        for role, (domain, uid) in kinds.items()
    }


def _avg(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _device_overview(hass: HomeAssistant, store: Dict, device: Dict, now: datetime) -> Dict:
    slug = device["device_slug"]
    coord, scheduler, curve_store = store["coordinator"], store["scheduler"], store["curve_store"]
    history = store.get("run_history")
    runs = history.runs(slug) if history else []
    month_ago = now - timedelta(days=30)

    def _local_start(r: Dict) -> Optional[datetime]:
        try:
            return dt_util.as_local(datetime.fromisoformat(r["started_at"]))
        except (TypeError, ValueError):
            return None

    now_local = dt_util.as_local(now)
    cycles = []
    keys = [c["cycle_key"] for c in device.get("cycles", [])]
    if any(r["cycle_key"] == EXTERNAL_CYCLE_KEY for r in runs):
        keys.append(EXTERNAL_CYCLE_KEY)
    for cid in keys:
        cfg = next((c for c in device.get("cycles", []) if c["cycle_key"] == cid), {})
        mine = [r for r in runs if r["cycle_key"] == cid]
        starts = [s for s in (_local_start(r) for r in mine) if s]
        measured = [r for r in mine if r.get("kwh") is not None]
        rhythm = cycle_rhythm(starts, now_local)
        row = {
            "cycle_key":          cid,
            "name":               _cycle_name(device, cid),
            "configured_minutes": cfg.get("duration_minutes"),
            "configured_kwh":     cfg.get("total_kwh"),
            "runs":               len(mine),
            "avg_kwh":            _avg([r["kwh"] for r in measured]),
            "avg_cost":           _avg([r.get("cost") for r in measured]),
            "avg_minutes":        _avg([r.get("duration_minutes") for r in mine]),
            "total_cost":         round(sum(r.get("cost") or 0 for r in measured), 4),
            "total_saved":        round(sum(
                (r["baseline_cost"] - r["cost"]) for r in measured
                if r.get("baseline_cost") is not None and r.get("cost") is not None
            ), 4),
            "rhythm":             rhythm,
        }
        if cid != EXTERNAL_CYCLE_KEY:
            info = curve_store.get_confidence_info(slug, cid)
            row["curve"] = {
                "n_curves":   info["n_curves"],
                "confidence": info["confidence"],
                "status":     info["status"],
                "profile_type": info["profile_type"],
                "learned_minutes": info["avg_duration_minutes"],
                "std_minutes": info["duration_std_minutes"],
                "profile":    curve_store.get_profile(slug, cid) or [],
            }
            try:
                row["suggestion"] = _rhythm_suggestion(store, device, cid, rhythm, now)
            except Exception:  # pragma: no cover — display-only
                _LOGGER.exception("SmartStart: rhythm suggestion failed for %s/%s", slug, cid)
                row["suggestion"] = None
        cycles.append(row)

    recent = [r for r in runs if r.get("ended_at", "") >= month_ago.isoformat()]
    sched = dict(scheduler.get_schedule(slug))
    sched.pop("service_payload", None)
    live = scheduler.get_current_stats(slug)
    recorder = scheduler._recorders.get(slug)
    if recorder and live:
        from .run_history import compact_samples
        live["samples"] = compact_samples(recorder.current_samples)
    if sched.get("cycle_id"):
        sched["cycle_name"] = _cycle_name(device, sched["cycle_id"])
    release = coord.next_rate_release(now) if device.get("auto_reschedule") else None

    return {
        "device_slug":   slug,
        "name":          device["name"],
        "has_power":     bool(device.get("power_sensor")),
        "schedule":      sched,
        "reach":         _reach(sched, device),
        "auto_reschedule": bool(device.get("auto_reschedule")),
        "rates_release": release.isoformat() if release else None,
        "offer":         coord.get_offer(slug),
        "entities":      _entity_ids(hass, {
            "cycle": ("select", f"smartstart_{slug}_cycle_select"),
            "mode":  ("select", f"smartstart_{slug}_scheduling_mode"),
            "power_down": ("select", f"smartstart_{slug}_power_down"),
            "start": ("button", f"smartstart_{slug}_start_anytime"),
            "stop":  ("button", f"smartstart_{slug}_cancel"),
        }),
        "live":          live,
        "selected_cycle": coord.get_selected_cycle_id(slug, device),
        "totals":        curve_store.get_totals(slug),
        "last_30d": {
            "runs":  len(recent),
            "kwh":   round(sum(r.get("kwh") or 0 for r in recent), 3),
            "cost":  round(sum(r.get("cost") or 0 for r in recent), 3),
            "saved": round(sum(
                (r["baseline_cost"] - r["cost"]) for r in recent
                if r.get("baseline_cost") is not None and r.get("cost") is not None
            ), 3),
        },
        "unknown_runs":  sum(1 for r in runs if r["cycle_key"] == EXTERNAL_CYCLE_KEY),
        "heatmap":       start_heatmap([s for s in (_local_start(r) for r in runs) if s]),
        "cycles":        cycles,
    }


# ── Laundry ───────────────────────────────────────────────────────────────────

# Flow rhythm drift worth suggesting a new interval for: off by at least a day
# and 20%, on a habit regular enough to trust.
DRIFT_MIN_DAYS = 1.0
DRIFT_MIN_FRACTION = 0.2
DRIFT_MIN_REGULARITY = 0.5


def _laundry_device_runs(hass: HomeAssistant) -> Dict[str, List[Dict]]:
    """Device run records grouped by laundry load id, across all entries."""
    out: Dict[str, List[Dict]] = {}
    for entry_store in hass.data.get(DOMAIN, {}).values():
        history = entry_store.get("run_history") if isinstance(entry_store, dict) else None
        if not history:
            continue
        for r in history.runs():
            lid = (r.get("laundry") or {}).get("run_id")
            if lid:
                out.setdefault(lid, []).append(r)
    return out


def _load_summary(load: Dict, runs: List[Dict], flow_names: Dict[str, str]) -> Dict:
    measured = [r for r in runs if r.get("kwh") is not None]
    kwh = round(sum(r["kwh"] for r in measured), 3) if measured else None
    cost = round(sum(r.get("cost") or 0 for r in measured), 4) if measured else None
    baseline = (
        round(kwh * load["now_rate"], 4)
        if kwh is not None and load.get("now_rate") is not None else None
    )
    return {
        "id":          load["id"],
        "started_at":  load["started_at"],
        "finished_at": load["finished_at"],
        "flows":       [flow_names.get(f, f) for f in load.get("flows", [])],
        "steps":       load.get("steps", []),
        "kwh":         kwh,
        # Measured when the device runs were recorded, else the plan estimate.
        "cost":        cost if cost is not None else load.get("plan_cost"),
        "cost_source": "measured" if cost is not None else "planned",
        "saved":       round(baseline - cost, 4) if baseline is not None and cost is not None else None,
        "run_ids":     [r["id"] for r in runs],
    }


def _laundry_overview(hass: HomeAssistant, mgr, now: datetime, by_load: Dict[str, List[Dict]]) -> Dict:
    now_local = dt_util.as_local(now)
    flow_names = {mgr._flow_slug(f): f["name"] for f in mgr.flows}
    loads = mgr.loads
    summaries = [_load_summary(l, by_load.get(l["id"], []), flow_names) for l in loads]

    flows = []
    due = {f["flow"]: f for f in mgr.flows_due()}
    for f in mgr.flows:
        slug = mgr._flow_slug(f)
        mine = [s for s, l in zip(summaries, loads) if slug in l.get("flows", [])]
        starts = []
        for s in mine:
            try:
                starts.append(dt_util.as_local(datetime.fromisoformat(s["started_at"])))
            except (TypeError, ValueError):
                pass
        rhythm = cycle_rhythm(starts, now_local)
        repeat = int(f.get("repeat_days") or 0)
        suggest = None
        iv = rhythm.get("interval_days")
        if iv and (rhythm.get("regularity") or 0) >= DRIFT_MIN_REGULARITY:
            gap = abs(iv - repeat)
            if repeat <= 0 or (gap >= DRIFT_MIN_DAYS and gap >= DRIFT_MIN_FRACTION * repeat):
                suggest = max(1, round(iv))
                if suggest == repeat:
                    suggest = None
        costs = [s["cost"] for s in mine if s["cost"] is not None]
        flows.append({
            "slug":          slug,
            "name":          f["name"],
            "steps":         [
                {"machine": st.get("machine"), "name": _cycle_name(
                    mgr.device_config(st.get("machine")) or {"cycles": []}, st.get("cycle_key", ""))}
                for st in f.get("steps", [])
            ],
            "repeat_days":   repeat,
            "overnight":     bool(f.get("overnight")),
            "last_done":     due.get(f["name"], {}).get("last_done"),
            "next_due":      due.get(f["name"], {}).get("next_due"),
            "loads":         len(mine),
            "avg_cost":      round(sum(costs) / len(costs), 4) if costs else None,
            "rhythm":        rhythm,
            "suggest_repeat_days": suggest,
        })

    month_ago = (now - timedelta(days=30)).isoformat()
    recent = [s for s in summaries if s["finished_at"] >= month_ago]
    return {
        "slug":        mgr.slug,
        "name":        mgr.name,
        "washer_slug": mgr.washer_slug,
        "dryer_slug":  mgr.dryer_slug,
        "rows":        [r for r in mgr.rows if r and r != ROW_NONE_LABEL],
        "bedtime":     mgr.bedtime.strftime("%H:%M"),
        "wake_time":   mgr.wake_time.strftime("%H:%M"),
        "mode":        mgr.mode,
        "due_at":      mgr.due_at.isoformat() if mgr.due_at else None,
        "snapshot":    mgr.snapshot(),
        "entities":    _entity_ids(hass, {
            "start":       ("button", f"smartstart_laundry_{mgr.slug}_start"),
            "cancel":      ("button", f"smartstart_laundry_{mgr.slug}_cancel"),
            "mode":        ("select", f"smartstart_laundry_{mgr.slug}_scheduling_mode"),
            "due_by":      ("select", f"smartstart_laundry_{mgr.slug}_due_by"),
            "changeover":  ("select", f"smartstart_laundry_{mgr.slug}_changeover_after"),
            "order":       ("select", f"smartstart_laundry_{mgr.slug}_load_order"),
            "power_down":  ("select", f"smartstart_laundry_{mgr.slug}_power_down"),
        }),
        "blackouts":   _blackouts_payload(mgr.washer_coordinator(), now),
        # Load N selects in row order (the panel's load pickers drive them).
        "load_entities": list(_entity_ids(hass, {
            str(i): ("select", f"smartstart_laundry_{mgr.slug}_load{i + 1}")
            for i in range(len(mgr.rows))
        }).values()),
        "flows":       flows,
        "loads":       list(reversed(summaries))[:50],
        "last_30d": {
            "loads": len(recent),
            "cost":  round(sum(s["cost"] or 0 for s in recent), 3),
            "saved": round(sum(s["saved"] or 0 for s in recent), 3),
        },
    }


def _find_laundry(hass: HomeAssistant, slug: str):
    from .laundry import all_laundry_managers
    return next((m for m in all_laundry_managers(hass) if m.slug == slug), None)


def _rates_payload(hass: HomeAssistant, now: datetime) -> Dict:
    """Last 6h + next 48h of slots: real, then forecast (central + band)."""
    stores = _device_stores(hass)
    if not stores:
        return {"slots": [], "real_until": None}
    coord = stores[0][0]["coordinator"]
    horizon = coord._real_horizon_end()
    since, until = now - timedelta(hours=6), now + timedelta(hours=PLANNER_HORIZON_HOURS)
    slots = [
        {"start": r.start.isoformat(), "end": r.end.isoformat(),
         "rate": round(r.rate_cost_per_kwh, 4), "predicted": False}
        for r in coord.rates if r.end > since and r.start < until
    ]
    if horizon is not None:
        slots += [
            {"start": r.start.isoformat(), "end": r.end.isoformat(),
             "rate": round(r.rate_cost_per_kwh, 4),
             "low": r.rate_low, "high": r.rate_high, "predicted": True}
            for r in coord._predicted_rates if r.start >= horizon and r.start < until
        ]
    return {
        "slots": slots,
        "real_until": horizon.isoformat() if horizon else None,
        "blackouts": _blackouts_payload(coord, now),
    }


def _blackouts_payload(coord, now: datetime) -> List[Dict]:
    """Power-down / saving-session spans for the charts to shade."""
    if coord is None:
        return []
    return [
        {"start": s.isoformat(), "end": e.isoformat()}
        for s, e in coord.blackout_spans(now)
    ]


# ── WebSocket commands ────────────────────────────────────────────────────────

@websocket_api.websocket_command({vol.Required("type"): "smartstart/overview"})
@callback
def ws_overview(hass: HomeAssistant, connection, msg) -> None:
    now = datetime.now(timezone.utc)
    stores = _device_stores(hass)
    coord = stores[0][0]["coordinator"] if stores else None
    connection.send_result(msg["id"], {
        "currency_symbol": coord.currency_symbol if coord else "£",
        "rate_unit":       coord.rate_unit if coord else "p",
        "now":             now.isoformat(),
        "rates":           _rates_payload(hass, now),
        "devices":         [_device_overview(hass, s, d, now) for s, d in stores],
        "laundry":         _all_laundry(hass, now),
    })


def _all_laundry(hass: HomeAssistant, now: datetime) -> List[Dict]:
    from .laundry import all_laundry_managers
    managers = all_laundry_managers(hass)
    if not managers:
        return []
    by_load = _laundry_device_runs(hass)
    out = []
    for mgr in managers:
        try:
            out.append(_laundry_overview(hass, mgr, now, by_load))
        except Exception:  # display-only — one bad pair mustn't blank the panel
            _LOGGER.exception("SmartStart: laundry overview failed for %s", mgr.slug)
    return out


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/runs",
    vol.Required("device_slug"): str,
    vol.Optional("cycle_key"): str,
    vol.Optional("limit", default=50): int,
    vol.Optional("offset", default=0): int,
})
@callback
def ws_runs(hass: HomeAssistant, connection, msg) -> None:
    store, device = _find_device(hass, msg["device_slug"])
    if store is None or "run_history" not in store:
        connection.send_error(msg["id"], "not_found", "Unknown device")
        return
    runs = store["run_history"].runs(msg["device_slug"])
    if msg.get("cycle_key"):
        runs = [r for r in runs if r["cycle_key"] == msg["cycle_key"]]
    runs = list(reversed(runs))
    page = runs[msg["offset"]: msg["offset"] + msg["limit"]]
    connection.send_result(msg["id"], {
        "total": len(runs),
        "runs":  [_run_summary(r, device) for r in page],
    })


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/run",
    vol.Required("run_id"): str,
})
@callback
def ws_run(hass: HomeAssistant, connection, msg) -> None:
    entry_store, run = _find_run(hass, msg["run_id"])
    if run is None:
        connection.send_error(msg["id"], "not_found", "Unknown run")
        return
    _, device = _find_device(hass, run["device_slug"])
    device = device or {"cycles": []}
    out = _run_summary(run, device)
    out["samples"] = run.get("samples") or []
    out["rates"] = run.get("rates") or []
    cid = run["cycle_key"]
    curve_store = entry_store.get("curve_store")
    if curve_store and cid != EXTERNAL_CYCLE_KEY:
        info = curve_store.get_confidence_info(run["device_slug"], cid)
        out["reference"] = {
            "profile": curve_store.get_profile(run["device_slug"], cid) or [],
            "avg_minutes": info["avg_duration_minutes"],
            "n_curves": info["n_curves"],
        }
    out["cycle_options"] = [
        {"cycle_key": c["cycle_key"], "name": c["name"]} for c in device.get("cycles", [])
    ]
    out["matches"] = (
        _match_cycles(curve_store, device, run)
        if curve_store and cid == EXTERNAL_CYCLE_KEY else []
    )
    connection.send_result(msg["id"], out)


def _match_cycles(curve_store, device: Dict, run: Dict) -> List[Dict]:
    """Rank learned cycles by how well an untagged run resembles them.

    shape = 1 − max gap between cumulative energy curves (the same
    worst-point measure the curve confidence uses); duration = ratio of the
    shorter to the longer. Score is their product.
    """
    from .curve_store import _to_profile

    samples = run.get("samples") or []
    dur = run.get("duration_minutes")
    if len(samples) < CURVE_MIN_SAMPLES or not dur:
        return []
    prof = _to_profile([{"t_s": t, "power_w": w} for t, w in samples])
    cum = [sum(prof[: i + 1]) for i in range(len(prof))]
    out = []
    for c in device.get("cycles", []):
        ref = curve_store.get_profile(run["device_slug"], c["cycle_key"])
        info = curve_store.get_confidence_info(run["device_slug"], c["cycle_key"])
        ref_dur = info["avg_duration_minutes"] or c.get("duration_minutes")
        if not ref or len(ref) != len(prof) or not ref_dur:
            continue
        ref_cum = [sum(ref[: i + 1]) for i in range(len(ref))]
        shape = 1.0 - max(abs(a - b) for a, b in zip(cum, ref_cum))
        duration = min(dur, ref_dur) / max(dur, ref_dur)
        out.append({
            "cycle_key": c["cycle_key"],
            "name":      c["name"],
            "score":     round(shape * duration, 3),
            "shape":     round(shape, 3),
            "duration":  round(duration, 3),
        })
    out.sort(key=lambda m: m["score"], reverse=True)
    return out[:3]


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/windows",
    vol.Required("device_slug"): str,
    vol.Required("cycle_key"): str,
})
@callback
def ws_windows(hass: HomeAssistant, connection, msg) -> None:
    store, device = _find_device(hass, msg["device_slug"])
    params = _scoring_params(store, device, msg["cycle_key"]) if store else None
    if params is None:
        connection.send_error(msg["id"], "not_found", "Unknown device/cycle")
        return
    now = datetime.now(timezone.utc)
    windows = store["coordinator"].best_windows_info(
        now,
        duration_minutes=params["duration_minutes"],
        kwh=params["kwh"],
        profile=params["profile"],
        min_delay_minutes=params["min_delay_minutes"],
        delay_step_minutes=params["delay_step_minutes"],
        anchor=params["anchor"],
    )
    cdata = store["coordinator"].data[device["device_slug"]]["cycles"][msg["cycle_key"]]
    now_win = cdata.get("now_window")
    connection.send_result(msg["id"], {
        "windows":          windows,
        "duration_minutes": params["duration_minutes"],
        "kwh":              params["kwh"],
        "profile":          params["profile"],
        "now": {
            "start": now_win.start.isoformat(),
            "rate":  round(now_win.avg_rate_cost_per_kwh, 4),
            "cost":  round(now_win.avg_rate_cost_per_kwh * params["kwh"], 3),
        } if now_win else None,
    })


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/tag_run",
    vol.Required("run_id"): str,
    vol.Required("cycle_key"): str,
    vol.Optional("learn", default=True): bool,
})
@websocket_api.async_response
async def ws_tag_run(hass: HomeAssistant, connection, msg) -> None:
    entry_store, run = _find_run(hass, msg["run_id"])
    if run is None:
        connection.send_error(msg["id"], "not_found", "Unknown run")
        return
    _, device = _find_device(hass, run["device_slug"])
    new_key = msg["cycle_key"]
    valid = {c["cycle_key"] for c in (device or {}).get("cycles", [])} | {EXTERNAL_CYCLE_KEY}
    if new_key not in valid:
        connection.send_error(msg["id"], "invalid", "Unknown cycle for this device")
        return
    curve_store = entry_store["curve_store"]
    slug = run["device_slug"]
    # Un-learn from the old cycle first so a mis-tag doesn't linger in its curve.
    if run.get("learned") and run["cycle_key"] != EXTERNAL_CYCLE_KEY:
        await curve_store.async_remove_curve(slug, run["cycle_key"], run["ended_at"])
    learned = False
    samples = run.get("samples") or []
    if msg["learn"] and new_key != EXTERNAL_CYCLE_KEY and len(samples) >= CURVE_MIN_SAMPLES:
        learned = await curve_store.async_add_curve(
            slug, new_key,
            [{"t_s": t, "power_w": w} for t, w in samples],
            captured_at=run["ended_at"],
        )
    await entry_store["run_history"].async_update(
        run["id"], cycle_key=new_key, learned=learned,
        tagged_at=datetime.now(timezone.utc).isoformat(),
    )
    # Learned duration/profile feed the planner — rescore so sensors follow.
    entry_store["coordinator"].rescore_now()
    connection.send_result(msg["id"], {"learned": learned})


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/delete_run",
    vol.Required("run_id"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete_run(hass: HomeAssistant, connection, msg) -> None:
    entry_store, run = _find_run(hass, msg["run_id"])
    if run is None:
        connection.send_error(msg["id"], "not_found", "Unknown run")
        return
    if run.get("learned") and run["cycle_key"] != EXTERNAL_CYCLE_KEY:
        await entry_store["curve_store"].async_remove_curve(
            run["device_slug"], run["cycle_key"], run["ended_at"]
        )
        entry_store["coordinator"].rescore_now()
    await entry_store["run_history"].async_delete(run["id"])
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/laundry_continue",
    vol.Required("pair"): str,
    vol.Optional("start_now", default=False): bool,
})
@websocket_api.async_response
async def ws_laundry_continue(hass: HomeAssistant, connection, msg) -> None:
    mgr = _find_laundry(hass, msg["pair"])
    if mgr is None:
        connection.send_error(msg["id"], "not_found", "Unknown laundry pair")
        return
    if not mgr.continue_available():
        connection.send_error(msg["id"], "not_ready", "No changeover is waiting")
        return
    await mgr.async_continue(start_now=msg["start_now"])
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/laundry_set_repeat",
    vol.Required("pair"): str,
    vol.Required("flow"): str,
    vol.Required("repeat_days"): vol.All(int, vol.Range(min=0, max=365)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_laundry_set_repeat(hass: HomeAssistant, connection, msg) -> None:
    """Set a flow's repeat interval (config change — reloads the pair)."""
    from .const import CONF_FLOWS
    mgr = _find_laundry(hass, msg["pair"])
    entry = hass.config_entries.async_get_entry(mgr.entry_id) if mgr else None
    if entry is None:
        connection.send_error(msg["id"], "not_found", "Unknown laundry pair")
        return
    config = dict(entry.options if entry.options else entry.data)
    flows = [dict(f) for f in config.get(CONF_FLOWS, [])]
    flow = next((f for f in flows if mgr._flow_slug(f) == msg["flow"]), None)
    if flow is None:
        connection.send_error(msg["id"], "not_found", "Unknown flow")
        return
    flow["repeat_days"] = msg["repeat_days"]
    config[CONF_FLOWS] = flows
    hass.config_entries.async_update_entry(entry, options=config)
    connection.send_result(msg["id"], {})



async def _laundry_action(hass: HomeAssistant, connection, msg, action) -> None:
    """Run action(manager) for msg["pair"], reporting HomeAssistantError
    (no offer, already running, rates don't reach…) as a WS error."""
    from homeassistant.exceptions import HomeAssistantError
    mgr = _find_laundry(hass, msg["pair"])
    if mgr is None:
        connection.send_error(msg["id"], "not_found", "Unknown laundry pair")
        return
    try:
        await action(mgr)
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], {})


def _ws_datetime(raw: str) -> datetime:
    when = datetime.fromisoformat(raw)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_utc(when)


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/laundry_schedule",
    vol.Required("pair"): str,
    vol.Required("start_at"): str,
})
@websocket_api.async_response
async def ws_laundry_schedule(hass: HomeAssistant, connection, msg) -> None:
    """Schedule the plan at a picked window (Start when idle, move when scheduled)."""
    try:
        start_at = _ws_datetime(msg["start_at"])
    except ValueError:
        connection.send_error(msg["id"], "invalid_format", "Bad start_at")
        return
    await _laundry_action(hass, connection, msg, lambda m: m.async_schedule_at(start_at))


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/laundry_accept_offer",
    vol.Required("pair"): str,
})
@websocket_api.async_response
async def ws_laundry_accept_offer(hass: HomeAssistant, connection, msg) -> None:
    await _laundry_action(hass, connection, msg, lambda m: m.async_accept_offer())


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/laundry_away",
    vol.Required("pair"): str,
    vol.Required("action"): vol.In(["add", "remove"]),
    vol.Optional("start"): str,
    vol.Optional("end"): vol.Any(str, None),
    vol.Optional("index"): int,
})
@websocket_api.async_response
async def ws_laundry_away(hass: HomeAssistant, connection, msg) -> None:
    """Add an away period (no changeovers; end omitted = not back until the
    next wake time) or remove one by index."""
    if msg["action"] == "remove":
        if "index" not in msg:
            connection.send_error(msg["id"], "invalid_format", "index required")
            return
        await _laundry_action(hass, connection, msg, lambda m: m.async_remove_away(msg["index"]))
        return
    try:
        start = _ws_datetime(msg["start"]) if msg.get("start") else dt_util.utcnow()
        end = _ws_datetime(msg["end"]) if msg.get("end") else None
    except ValueError:
        connection.send_error(msg["id"], "invalid_format", "Bad start/end")
        return
    await _laundry_action(hass, connection, msg, lambda m: m.async_add_away(start, end))


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/laundry_start_now",
    vol.Required("pair"): str,
})
@websocket_api.async_response
async def ws_laundry_start_now(hass: HomeAssistant, connection, msg) -> None:
    """Start loaded, delay-started steps now (override a Continue delay)."""
    await _laundry_action(hass, connection, msg, lambda m: m.async_start_now())


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/laundry_due",
    vol.Required("pair"): str,
    vol.Optional("due_at"): vol.Any(str, None),
})
@websocket_api.async_response
async def ws_laundry_due(hass: HomeAssistant, connection, msg) -> None:
    """Set (or clear, with no due_at) the plan's finish-by deadline."""
    try:
        due = _ws_datetime(msg["due_at"]) if msg.get("due_at") else None
    except ValueError:
        connection.send_error(msg["id"], "invalid_format", "Bad due_at")
        return

    async def _set(m) -> None:
        m.set_due_at(due)
    await _laundry_action(hass, connection, msg, _set)


def _update_pair_options(hass: HomeAssistant, connection, msg, change) -> None:
    """Apply change(config) to the pair's entry options (reloads the pair)."""
    mgr = _find_laundry(hass, msg["pair"])
    entry = hass.config_entries.async_get_entry(mgr.entry_id) if mgr else None
    if entry is None:
        connection.send_error(msg["id"], "not_found", "Unknown laundry pair")
        return
    config = dict(entry.options if entry.options else entry.data)
    problem = change(config, mgr)
    if problem:
        connection.send_error(msg["id"], "not_found", problem)
        return
    hass.config_entries.async_update_entry(entry, options=config)
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/laundry_flow_overnight",
    vol.Required("pair"): str,
    vol.Required("flow"): str,
    vol.Required("overnight"): bool,
})
@websocket_api.require_admin
@callback
def ws_laundry_flow_overnight(hass: HomeAssistant, connection, msg) -> None:
    """Mark a flow as in use overnight (first wash must start while you're up)."""
    from .const import CONF_FLOWS

    def change(config, mgr):
        flows = [dict(f) for f in config.get(CONF_FLOWS, [])]
        flow = next((f for f in flows if mgr._flow_slug(f) == msg["flow"]), None)
        if flow is None:
            return "Unknown flow"
        flow["overnight"] = msg["overnight"]
        config[CONF_FLOWS] = flows
        return None
    _update_pair_options(hass, connection, msg, change)


@websocket_api.websocket_command({
    vol.Required("type"): "smartstart/laundry_sleep",
    vol.Required("pair"): str,
    vol.Required("bedtime"): vol.Match(r"^\d{1,2}:\d{2}$"),
    vol.Required("wake_time"): vol.Match(r"^\d{1,2}:\d{2}$"),
})
@websocket_api.require_admin
@callback
def ws_laundry_sleep(hass: HomeAssistant, connection, msg) -> None:
    """Set the nightly no-changeover window (bedtime → wake)."""
    from .const import CONF_BEDTIME, CONF_WAKE_TIME

    def change(config, _mgr):
        config[CONF_BEDTIME] = msg["bedtime"].zfill(5)
        config[CONF_WAKE_TIME] = msg["wake_time"].zfill(5)
        return None
    _update_pair_options(hass, connection, msg, change)
