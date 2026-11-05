from datetime import datetime, timezone
from typing import Dict
import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    ROW_NONE_LABEL,
    SERVICE_ACCEPT_OFFER,
    SERVICE_LAUNDRY_WINDOWS,
    SERVICE_PUBLISH_PREDICTED_RATES,
    SERVICE_PUBLISH_RATES,
    SERVICE_START,
    SERVICE_START_LAUNDRY,
    SERVICE_STOP,
)
from .coordinator import SmartStartCoordinator
from .providers.base import RatePeriod
from .providers.manual import ManualProvider
from .scheduler import SmartStartScheduler

_LOGGER = logging.getLogger(__name__)

_PUBLISH_RATES_SCHEMA = vol.Schema(
    {
        vol.Required("rates"): [
            vol.Schema(
                {
                    vol.Required("start"): str,
                    vol.Required("end"): str,
                    vol.Required("rate_cost_per_kwh"): vol.Coerce(float),
                    # Forecast band — publish_predicted_rates only.
                    vol.Optional("rate_low_per_kwh"): vol.Coerce(float),
                    vol.Optional("rate_high_per_kwh"): vol.Coerce(float),
                }
            )
        ]
    }
)

_START_SCHEMA = vol.Schema(
    {
        vol.Required("device_slug"): cv.string,
        vol.Required("cycle_id"): cv.string,
        vol.Optional("finish_by"): str,   # ISO-8601 — find cheapest window done by this time
        vol.Optional("now", default=False): bool,  # bypass rate optimisation, start immediately
        vol.Optional("start_at"): str,    # ISO-8601 — run in the window starting here (a picked Best Windows row)
    }
)

_STOP_SCHEMA = vol.Schema({vol.Required("device_slug"): cv.string})

_LAUNDRY_WINDOWS_SCHEMA = vol.Schema(
    {
        vol.Optional("pair"): cv.string,
        vol.Required("loads"): vol.All(cv.ensure_list, [cv.string]),
    }
)

_START_LAUNDRY_SCHEMA = vol.Schema(
    {
        # Which pair to run (slug or name). Optional when only one pair exists.
        vol.Optional("pair"): cv.string,
        # Explicit step list — the escape hatch for shapes the row UI can't
        # express. Omit to run whatever the pair's plan-row selects hold.
        vol.Optional("steps"): [
            vol.Schema(
                {
                    vol.Required("machine"): vol.In(["wash", "dry"]),
                    vol.Required("cycle_key"): cv.string,
                    vol.Optional("confirm_before", default=True): bool,
                    vol.Optional("duration_minutes"): vol.Coerce(float),
                    vol.Optional("kwh"): vol.Coerce(float),
                }
            )
        ],
        vol.Optional("mode"): vol.In(["now", "balanced", "max_savings"]),
        vol.Optional("start_at"): str,   # ISO-8601 — pin the plan start (a picked window); moves a scheduled plan
        vol.Optional("finish_by"): str,  # ISO-8601 deadline for the plan END
    }
)


def _resolve_device(hass: HomeAssistant, device_slug: str) -> tuple[Dict, Dict]:
    """Return (hass.data store, device config) for the entry owning device_slug.

    Users may run one config entry per appliance. Routing everything through
    entries[0] (the old behaviour) sent start/stop calls to the first entry's
    scheduler — an instance the target device's entities never listen to —
    so e.g. Stop appeared to do nothing until a reload re-read storage.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        config = entry.options if entry.options else entry.data
        device = next(
            (d for d in config.get("devices", []) if d.get("device_slug") == device_slug),
            None,
        )
        if device is not None:
            return hass.data.get(DOMAIN, {}).get(entry.entry_id, {}), device
    return {}, {}


def _all_entry_stores(hass: HomeAssistant) -> list[Dict]:
    return [
        hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        for entry in hass.config_entries.async_entries(DOMAIN)
    ]


async def async_setup_services(hass: HomeAssistant) -> None:

    def _parse_rates(raw: list) -> list[RatePeriod]:
        rates: list[RatePeriod] = []
        for r in raw:
            try:
                start = datetime.fromisoformat(r["start"])
                end = datetime.fromisoformat(r["end"])
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                rates.append(
                    RatePeriod(
                        start=start, end=end,
                        rate_cost_per_kwh=r["rate_cost_per_kwh"],
                        rate_low=r.get("rate_low_per_kwh"),
                        rate_high=r.get("rate_high_per_kwh"),
                    )
                )
            except (ValueError, KeyError) as exc:
                _LOGGER.warning("SmartStart: skipping malformed rate: %s", exc)
        return rates

    async def handle_publish_rates(call: ServiceCall) -> None:
        rates = _parse_rates(call.data["rates"])
        if not rates:
            return
        # Broadcast to every loaded entry — with one entry per appliance,
        # pushing to entries[0] only would leave the others rateless.
        pushed = 0
        for store in _all_entry_stores(hass):
            coordinator: SmartStartCoordinator = store.get("coordinator")
            if coordinator is None:
                continue
            provider = store.get("provider")
            if isinstance(provider, ManualProvider):
                provider.set_rates(rates)
            coordinator.push_rates(rates)
            pushed += 1
        _LOGGER.info(
            "SmartStart: received %d rate periods via service (pushed to %d entries)",
            len(rates), pushed,
        )

    async def handle_publish_predicted_rates(call: ServiceCall) -> None:
        """User-supplied forecast slots (e.g. agileforecast.co.uk). Used only
        to pre-position hold-for-rates placeholders beyond the real horizon —
        never for normal scoring, and always re-checked against actuals."""
        rates = _parse_rates(call.data["rates"])
        if not rates:
            return
        pushed = 0
        for store in _all_entry_stores(hass):
            coordinator: SmartStartCoordinator = store.get("coordinator")
            if coordinator is None:
                continue
            coordinator.push_predicted_rates(rates)
            pushed += 1
        _LOGGER.info(
            "SmartStart: received %d predicted rate periods (pushed to %d entries)",
            len(rates), pushed,
        )

    async def handle_start(call: ServiceCall) -> None:
        device_slug = call.data["device_slug"]
        # Service param historically named "cycle_id" — internally it now matches
        # cycle_key (auto-generated slug). Existing values keep working because
        # the migration backfilled cycle_key = old cycle_id.
        cycle_key   = call.data["cycle_id"]
        finish_by   = call.data.get("finish_by")
        start_now   = call.data.get("now", False)
        start_at    = call.data.get("start_at")

        store, device = _resolve_device(hass, device_slug)
        if not device:
            _LOGGER.error("SmartStart: unknown device %r", device_slug)
            return
        coordinator: SmartStartCoordinator = store.get("coordinator")
        scheduler: SmartStartScheduler = store.get("scheduler")
        if not coordinator or not scheduler:
            _LOGGER.error(
                "SmartStart: entry owning %r is not loaded — cannot start", device_slug
            )
            return

        cycle = next((c for c in device.get("cycles", []) if c["cycle_key"] == cycle_key), None)
        if cycle is None:
            _LOGGER.error("SmartStart: unknown cycle %r for device %r", cycle_key, device_slug)
            return

        cd = (
            (coordinator.data or {})
            .get(device_slug, {})
            .get("cycles", {})
            .get(cycle_key, {})
        )

        # Deadline that caps any later auto-reschedule of this run: an explicit
        # finish_by wins, else the device's Cycle Due By helper if set.
        resched_deadline = None
        hold_until = None  # hold-for-rates only applies to the default path
        picked_predicted = False
        if start_now:
            window = cd.get("now_window")
        elif start_at:
            try:
                at = datetime.fromisoformat(start_at)
            except ValueError:
                from homeassistant.exceptions import HomeAssistantError
                raise HomeAssistantError(f"Invalid start_at {start_at!r}")
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            window = coordinator.get_window_starting_at(device_slug, cycle_key, at)
            if window is not None:
                # The user picked this slot: it's the run's envelope, and a
                # forecast-priced pick re-baselines when real rates land.
                resched_deadline = window.end
                picked_predicted = coordinator.is_predicted_window(window)
        elif finish_by:
            try:
                deadline = datetime.fromisoformat(finish_by)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                window = coordinator.get_window_for_deadline(device_slug, cycle_key, deadline)
                resched_deadline = deadline
            except ValueError:
                _LOGGER.error("SmartStart: invalid finish_by %r", finish_by)
                return
        else:
            window = cd.get("chosen_window") or cd.get("best_window")
            resched_deadline = coordinator.get_cycle_due_at(device_slug)
            hold_until = cd.get("hold_until")

        if window is None:
            _LOGGER.error(
                "SmartStart: no valid window for %s/%s (finish_by=%s, now=%s)",
                device_slug, cycle_key, finish_by, start_now,
            )
            return

        now_rate = (coordinator.data or {}).get(device_slug, {}).get("current_rate")
        if now_rate is None:
            now_win = cd.get("now_window")
            now_rate = now_win.avg_rate_cost_per_kwh if now_win else None

        # Payload sent to the user's start_service: the editable cycle_id, or the
        # cycle name as a friendly fallback when the user left cycle_id blank.
        payload_cycle_id = cycle.get("cycle_id") or cycle.get("name", "")
        estimated_duration = cd.get("duration_minutes") or cycle.get("duration_minutes")

        # Reschedule flag: true when this start replaces an already-committed
        # schedule. User scripts (e.g. LG dryer) can branch on this to update
        # an existing reservation rather than start a new one.
        from .const import STATE_IDLE
        prior_status = scheduler.get_schedule(device_slug).get("status", STATE_IDLE)
        is_reschedule = prior_status != STATE_IDLE

        service_payload = {
            "delay_seconds":            window.delay_minutes * 60,
            "cycle_id":                 payload_cycle_id,
            "start_time":               window.start.isoformat(),
            "cycle_duration":           cycle.get("duration_minutes"),
            "estimated_cycle_duration": estimated_duration,
            "rate_cost_per_kwh":        window.avg_rate_cost_per_kwh,
            "estimated_finish":         window.end.isoformat(),
            "reschedule":               is_reschedule,
        }

        await scheduler.async_schedule(
            device_slug=device_slug,
            cycle_id=cycle_key,
            window=window,
            cycle_kwh=cycle["total_kwh"],
            now_rate=now_rate,
            start_service=device["start_service"],
            service_payload=service_payload,
            reschedule_deadline=resched_deadline,
            hold_until=hold_until,
            rate_predicted=(
                picked_predicted if start_at
                else bool(cd.get("chosen_is_predicted")) if not start_now and not finish_by
                else False
            ),
        )

    async def handle_start_laundry(call: ServiceCall) -> None:
        from .laundry import all_laundry_managers

        managers = all_laundry_managers(hass)
        if not managers:
            _LOGGER.error("SmartStart: start_laundry — no laundry pair configured")
            return
        pair = call.data.get("pair")
        if pair:
            manager = next(
                (m for m in managers if m.slug == pair or m.name == pair), None
            )
            if manager is None:
                _LOGGER.error("SmartStart: start_laundry — unknown pair %r", pair)
                return
        elif len(managers) == 1:
            manager = managers[0]
        else:
            _LOGGER.error(
                "SmartStart: start_laundry — multiple pairs configured, pass pair:"
            )
            return

        if "mode" in call.data:
            manager.set_mode(call.data["mode"])
        finish_by = call.data.get("finish_by")
        if finish_by:
            try:
                deadline = datetime.fromisoformat(finish_by)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                manager.set_due_at(deadline)
            except ValueError:
                _LOGGER.error("SmartStart: start_laundry — invalid finish_by %r", finish_by)
                return

        start_at = None
        if call.data.get("start_at"):
            try:
                start_at = datetime.fromisoformat(call.data["start_at"])
            except ValueError as err:
                raise HomeAssistantError(f"Invalid start_at {call.data['start_at']!r}") from err
            if start_at.tzinfo is None:
                start_at = dt_util.as_utc(start_at.replace(tzinfo=dt_util.get_default_time_zone()))
            if manager.status != "idle" and not call.data.get("steps"):
                await manager.async_schedule_at(start_at)
                return

        steps = None
        raw_steps = call.data.get("steps")
        if raw_steps:
            steps = [
                manager._make_step(
                    s["machine"],
                    s["cycle_key"],
                    confirm_before=bool(s.get("confirm_before", True)),
                    duration_override=s.get("duration_minutes"),
                    kwh_override=s.get("kwh"),
                )
                for s in raw_steps
            ]
        await manager.async_start(steps, start_at=start_at)

    async def handle_stop(call: ServiceCall) -> None:
        device_slug = call.data["device_slug"]
        store, device = _resolve_device(hass, device_slug)
        scheduler: SmartStartScheduler = store.get("scheduler")
        if not scheduler:
            _LOGGER.error(
                "SmartStart: handle_stop — no loaded entry owns device %r", device_slug
            )
            return

        _LOGGER.info("SmartStart: handle_stop — cancelling %s", device_slug)

        # Fire user-supplied stop_service first (gives the appliance a chance
        # to react — e.g. cancel a delayed start on the LG dryer) before we
        # clear SmartStart's own schedule state. Failure to fire the user's
        # stop_service is non-fatal — we still cancel the SmartStart-side
        # state — but we surface it via smartstart_action_failed so users can
        # be notified.
        stop_service = (device or {}).get("stop_service", "")
        if stop_service and "." in stop_service:
            from homeassistant.exceptions import (
                HomeAssistantError,
                ServiceNotFound,
                ServiceValidationError,
            )

            def _fail(reason: str, detail: str) -> None:
                _LOGGER.warning(
                    "SmartStart: %s stop_service %r failed (%s): %s",
                    device_slug, stop_service, reason, detail,
                )
                hass.bus.async_fire(
                    "smartstart_action_failed",
                    {
                        "device_slug": device_slug,
                        "action": "stop",
                        "service": stop_service,
                        "reason": reason,
                        "detail": detail,
                    },
                )

            domain, svc = stop_service.split(".", 1)
            # Pre-check button entities so we can report missing/unavailable
            if domain == "button":
                entity_state = hass.states.get(stop_service)
                if entity_state is None:
                    _fail("entity_missing", f"button {stop_service} not in state machine")
                elif entity_state.state in ("unavailable", "unknown"):
                    _fail("entity_unavailable", f"button {stop_service} state={entity_state.state}")
                else:
                    try:
                        await hass.services.async_call(
                            "button", "press", {"entity_id": stop_service}, blocking=False
                        )
                    except ServiceNotFound as exc:
                        _fail("service_not_found", str(exc))
                    except ServiceValidationError as exc:
                        _fail("validation_error", str(exc))
                    except HomeAssistantError as exc:
                        _fail("ha_error", str(exc))
                    except Exception as exc:  # noqa: BLE001
                        _fail("unexpected", f"{type(exc).__name__}: {exc}")
            else:
                try:
                    await hass.services.async_call(domain, svc, {}, blocking=False)
                except ServiceNotFound as exc:
                    _fail("service_not_found", str(exc))
                except ServiceValidationError as exc:
                    _fail("validation_error", str(exc))
                except HomeAssistantError as exc:
                    _fail("ha_error", str(exc))
                except Exception as exc:  # noqa: BLE001
                    _fail("unexpected", f"{type(exc).__name__}: {exc}")

        _LOGGER.info("SmartStart: handle_stop — calling async_cancel for %s", device_slug)
        await scheduler.async_cancel(device_slug)
        _LOGGER.info("SmartStart: handle_stop — async_cancel complete for %s", device_slug)

    hass.services.async_register(
        DOMAIN, SERVICE_PUBLISH_RATES, handle_publish_rates, _PUBLISH_RATES_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PUBLISH_PREDICTED_RATES, handle_publish_predicted_rates,
        _PUBLISH_RATES_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_START, handle_start, _START_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STOP, handle_stop, _STOP_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_START_LAUNDRY, handle_start_laundry, _START_LAUNDRY_SCHEMA
    )

    async def handle_accept_offer(call: ServiceCall) -> None:
        from homeassistant.exceptions import HomeAssistantError
        device_slug = call.data["device_slug"]
        from .laundry import all_laundry_managers
        pair = next((m for m in all_laundry_managers(hass) if m.slug == device_slug), None)
        if pair is not None:
            await pair.async_accept_offer()
            return
        store, _device = _resolve_device(hass, device_slug)
        coordinator: SmartStartCoordinator = store.get("coordinator")
        if not coordinator:
            raise HomeAssistantError(f"No loaded SmartStart entry owns device {device_slug!r}")
        await coordinator.async_accept_offer(device_slug)

    hass.services.async_register(
        DOMAIN, SERVICE_ACCEPT_OFFER, handle_accept_offer, _STOP_SCHEMA
    )

    def _match_load(manager, spoken: str) -> str:
        """Map a spoken/typed load to one of the pair's Load options: exact
        (case-insensitive), then a single option containing every word."""
        options = [o for o in manager.row_options() if o != ROW_NONE_LABEL]
        want = spoken.strip().lower()
        for o in options:
            if o.lower() == want:
                return o
        words = want.split()
        # Saved flows win over single "Wash:"/"Dry:" cycles, so "linen sheets"
        # means the Linen Bed Sheets flow, not the Linen Sheets wash cycle.
        flows = [o for o in options if not o.startswith(("Wash: ", "Dry: "))]
        hits = [o for o in flows if all(w in o.lower() for w in words)]
        if not hits:
            hits = [o for o in options if all(w in o.lower() for w in words)]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise HomeAssistantError(
                f"No laundry load matches {spoken!r}. Options: {', '.join(options)}"
            )
        raise HomeAssistantError(
            f"{spoken!r} matches several loads ({', '.join(hits)}); be more specific"
        )

    async def handle_laundry_windows(call: ServiceCall) -> Dict:
        """Best windows for an ad-hoc set of loads — read-only, returns a response."""
        from .laundry import all_laundry_managers

        managers = all_laundry_managers(hass)
        pair = call.data.get("pair")
        if pair:
            manager = next((m for m in managers if m.slug == pair or m.name == pair), None)
        else:
            manager = managers[0] if len(managers) == 1 else None
        if manager is None:
            raise HomeAssistantError("Unknown or ambiguous laundry pair; pass pair:")
        labels = [_match_load(manager, l) for l in call.data["loads"]]
        preview = manager.windows_for_loads(labels)
        if preview is None:
            raise HomeAssistantError("No electricity rates available yet")
        if preview.get("infeasible"):
            return {"loads": labels, "feasible": False,
                    "message": "No window fits these loads with the current away times and wet-wait limit.",
                    "suggestions": [s.get("message") for s in preview.get("suggestions", [])]}

        def local(iso: str) -> str:
            return dt_util.as_local(datetime.fromisoformat(iso)).strftime("%a %d %b %H:%M")

        def pence(rate) -> float:
            return round(float(rate) * 100, 1)

        total_kwh = preview["total_kwh"]

        def timetable(raw_steps):
            """Steps, when you're needed, and idle stretches for ONE window."""
            steps_out = [
                {
                    "machine": st["machine"],
                    "name": st["name"],
                    "load": st.get("load"),
                    "start": local(st["planned_start"]),
                    "end": local(st["planned_end"]),
                    # confirm_before = SmartStart waits for you to load the machine
                    # and press Continue (a changeover) instead of starting itself.
                    "needs_you": bool(st.get("confirm_before")),
                }
                for st in raw_steps
            ]
            # Idle stretches when no machine is running (wash and dry can overlap,
            # so track the latest end so far rather than comparing neighbours).
            gaps = []
            latest = None
            for st in sorted(raw_steps, key=lambda x: x["planned_start"]):
                if latest is not None:
                    idle = (
                        datetime.fromisoformat(st["planned_start"])
                        - datetime.fromisoformat(latest["planned_end"])
                    ).total_seconds() / 3600
                    if idle >= 2:
                        gaps.append(
                            f"about {round(idle)} hours with nothing running, from "
                            f"{local(latest['planned_end'])} until {local(st['planned_start'])}"
                        )
                if latest is None or st["planned_end"] > latest["planned_end"]:
                    latest = st
            needed = [
                f"{st['start']} to start {st['machine']} {st['name']} ({st['load']})"
                for st in steps_out if st["needs_you"]
            ]
            return steps_out, needed, gaps

        windows = []
        for w in preview.get("best_windows", []):
            steps_out, needed, gaps = timetable(w.get("steps", []))
            windows.append({
                "start": local(w["start"]),
                "end": local(w["end"]),
                "avg_pence_per_kwh": pence(w["rate"]),
                "cost_pounds": round(w["cost"], 2),
                "confirmed_rates": bool(w.get("confirmed")),
                "you_are_needed_at": needed,
                "waits_between_steps": gaps,
                "steps": steps_out,
            })
        order = preview.get("order") or {}
        out = {
            "loads": labels,
            "feasible": True,
            "run_order": order.get("order", labels),
            "total_kwh": round(total_kwh, 2),
            "best_windows": windows,
        }
        if preview.get("now_rate") is not None:
            out["if_started_now_pence_per_kwh"] = pence(preview["now_rate"])
            out["if_started_now_cost_pounds"] = round(float(preview["now_rate"]) * total_kwh, 2)
        out["suggestions"] = [s.get("message") for s in preview.get("suggestions", []) if s.get("message")]
        return out

    hass.services.async_register(
        DOMAIN, SERVICE_LAUNDRY_WINDOWS, handle_laundry_windows,
        _LAUNDRY_WINDOWS_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
