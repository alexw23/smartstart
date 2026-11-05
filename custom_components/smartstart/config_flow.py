"""Config flow and options flow for SmartStart."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_BEDTIME,
    CONF_MAX_WET_WAIT,
    CONF_TIME_VALUE,
    CONF_WAKE_TIME,
    DEFAULT_BEDTIME,
    DEFAULT_TIME_VALUE,
    DEFAULT_MAX_WET_WAIT,
    DEFAULT_WAKE_TIME,
    CONF_DRYER_SLUG,
    CONF_ENTRY_TYPE,
    CONF_FLOWS,
    CONF_GAP_MINUTES,
    CONF_HOLD_ENGAGE_LEAD,
    CONF_RATES_EXPECTED_AT,
    CONF_WASHER_SLUG,
    DEFAULT_GAP_MINUTES,
    ENTRY_TYPE_LAUNDRY,
    DEFAULT_AUTO_RESCHEDULE,
    DEFAULT_OFFERS,
    DEFAULT_MAX_WAIT_HOURS,
    DEFAULT_BALANCED_TARGET_RATE,
    DEFAULT_DELAY_HANDLING,
    DEFAULT_HOLD_ENGAGE_LEAD,
    DEFAULT_HOLD_FOR_NEXT_DAY_RATES,
    DEFAULT_RATES_EXPECTED_AT,
    DEFAULT_DELAY_STEP_MINUTES,
    DEFAULT_MODE,
    DELAY_HANDLING_END_TIME,
    DELAY_HANDLING_MANAGED,
    DELAY_HANDLING_START_DELAY,
    MODE_BALANCED,
    MODE_MAX_SAVINGS,
    MODE_NOW,
    DEFAULT_MAX_DELAY_MINUTES,
    DEFAULT_MIN_DELAY_MINUTES,
    DOMAIN,
    CONF_CHEAP_WINDOW_THRESHOLD,
    CONF_PREFER_SOONER_DIFF,
    CONF_RESCHEDULE_MIN_SAVING,
    DEFAULT_CHEAP_WINDOW_THRESHOLD,
    DEFAULT_RESCHEDULE_MIN_SAVING,
    PREFER_SOONER_RATE_DIFF,
    PROVIDER_MANUAL,
    PROVIDER_OCTOPUS_ENTITY,
)
from .laundry_plan import MACHINE_DRY, MACHINE_WASH
from .blackout import autodetect_calendars
from .const import CONF_BLACKOUT_CALENDARS

_LOGGER = logging.getLogger(__name__)

_BAR_BLOCKS = " ▁▂▃▄▅▆▇█"


def _profile_bars(profile: list[float]) -> str:
    """Render a profile (list of fractions) as a unicode bar string."""
    if not profile:
        return "—"
    peak = max(profile) or 1.0
    return "".join(
        _BAR_BLOCKS[min(len(_BAR_BLOCKS) - 1, int(v / peak * (len(_BAR_BLOCKS) - 1)))]
        for v in profile
    )


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _octopus_candidates(hass, suffix: str) -> list[str]:
    """Octopus Energy rate entities ending in `suffix`, best first: electricity
    import before export, gas last. Sorted so the pick is stable. The setup
    form still lets the user choose any other entity."""
    found = sorted(
        s.entity_id for s in hass.states.async_all()
        if "octopus" in s.entity_id and suffix in s.entity_id
    )

    def rank(entity_id: str) -> int:
        if "_gas_" in entity_id:
            return 2
        if "export" in entity_id:
            return 1
        return 0

    return sorted(found, key=rank)


def _detect_octopus(hass) -> Optional[Dict[str, str]]:
    if hass is None:
        return None
    current = next(iter(_octopus_candidates(hass, "current_day_rates")), None)
    nxt = next(iter(_octopus_candidates(hass, "next_day_rates")), None)
    return {"current": current, "next": nxt} if current and nxt else None


def _entity_state_suggestions(hass: Optional[HomeAssistant], entity_id: str) -> list[str]:
    """Return discoverable state options for an entity, or [] if none.

    Used to pre-fill the running/paused state chips the first time a user
    picks an entity. binary_sensor / switch / input_boolean → on/off.
    select / input_select / enum sensor → entity's `options` attribute.
    Anything else → [] (user types their own).
    """
    if not entity_id or not hass:
        return []
    state = hass.states.get(entity_id)
    if state is None:
        return []
    domain = entity_id.split(".", 1)[0]
    if domain in ("binary_sensor", "switch", "input_boolean"):
        return ["on", "off"]
    opts = state.attributes.get("options")
    if isinstance(opts, list) and opts:
        return [str(o) for o in opts]
    return []


def _to_state_list(val) -> list[str]:
    """Normalise a legacy string or existing list to a list, stripping blanks."""
    if isinstance(val, list):
        return [v for v in val if v]
    if val:
        return [val]
    return []


def _opt(key: str, default):
    """vol.Optional that drops the default kwarg when the value is empty.

    EntitySelector rejects empty-string defaults with "Entity is neither a valid
    entity ID nor a valid UUID", so optional entity fields need no default at
    all rather than ''. Same for any optional field whose validator can't
    accept '' / [] / None.
    """
    if default in (None, "", [], {}):
        return vol.Optional(key)
    return vol.Optional(key, default=default)


# ── Schema helpers ────────────────────────────────────────────────────────────

def _device_schema(
    defaults: Dict = None,
    hass: Optional[HomeAssistant] = None,
    extra_actions: Dict = None,
) -> vol.Schema:
    d = defaults or {}
    running_entity = d.get("running_entity", "")
    paused_entity  = d.get("paused_entity", "") or running_entity
    running_state_suggestions = _entity_state_suggestions(hass, running_entity)
    paused_state_suggestions  = _entity_state_suggestions(hass, paused_entity)

    # Default chips: existing config values, or auto-suggested options on first use
    running_default = _to_state_list(d.get("running_states") or d.get("running_state")) \
        or running_state_suggestions
    paused_default  = _to_state_list(d.get("paused_states")) or []

    schema: Dict = {
        vol.Required("name", default=d.get("name", "")): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        vol.Optional("icon", default=d.get("icon", "mdi:lightning-bolt")): selector.IconSelector(),
        # ── Start / cancel ─────────────────────────────────────────────────
        # Free text — accepts script.name, button.entity_id, or any service string.
        # Entity selector can't be used here because scripts defined in scripts.yaml
        # may not appear in the entity registry.
        vol.Required("start_service", default=d.get("start_service", "")): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        vol.Optional("stop_service", default=d.get("stop_service", "")): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        # ── Delay handling ──────────────────────────────────────────────────
        vol.Required(
            "delay_handling",
            default=d.get("delay_handling", DEFAULT_DELAY_HANDLING),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(
                        value=DELAY_HANDLING_START_DELAY,
                        label="It has its own delay timer (e.g. 'start in 3 hours')",
                    ),
                    selector.SelectOptionDict(
                        value=DELAY_HANDLING_END_TIME,
                        label="It schedules by finish time (e.g. 'finish by 9 AM')",
                    ),
                    selector.SelectOptionDict(
                        value=DELAY_HANDLING_MANAGED,
                        label="It can't be delayed — SmartStart will trigger the start at start time",
                    ),
                ]
            )
        ),
        # Scheduling mode (Max Savings / Balanced / Now) is a SelectEntity
        # exposed at runtime, not a config field — see select.py. Only the
        # Balanced target rate lives here, as it's a calibration value rather
        # than a day-to-day toggle.
        vol.Optional(
            "balanced_target_rate",
            default=float(d.get("balanced_target_rate", DEFAULT_BALANCED_TARGET_RATE)),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=-1.0, max=1.0, step=0.001,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        # ── Delay step ─────────────────────────────────────────────────────
        vol.Required(
            "delay_step_minutes",
            default=int(d.get("delay_step_minutes", DEFAULT_DELAY_STEP_MINUTES)),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=240, step=1, mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            "min_delay_minutes",
            default=d.get("min_delay_minutes", DEFAULT_MIN_DELAY_MINUTES),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=1440, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(
            "max_delay_minutes",
            default=d.get("max_delay_minutes", DEFAULT_MAX_DELAY_MINUTES),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=1440, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(
            "max_wait_hours",
            default=d.get("max_wait_hours", DEFAULT_MAX_WAIT_HOURS),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=96, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(
            "auto_reschedule",
            default=bool(d.get("auto_reschedule", DEFAULT_AUTO_RESCHEDULE)),
        ): selector.BooleanSelector(),
        vol.Optional(
            "offers",
            default=bool(d.get("offers", DEFAULT_OFFERS)),
        ): selector.BooleanSelector(),
    }

    # Hold-for-rates only makes sense with auto-reschedule (the reschedule when
    # rates land IS the payoff), so the toggle only appears once auto_reschedule
    # has been enabled and saved. Enable auto-reschedule, save, re-open to see it.
    if d.get("auto_reschedule"):
        schema[vol.Optional(
            "hold_for_next_day_rates",
            default=bool(d.get("hold_for_next_day_rates", DEFAULT_HOLD_FOR_NEXT_DAY_RATES)),
        )] = selector.BooleanSelector()

    schema.update({
        # ── Running entity ─────────────────────────────────────────────────
        _opt("running_entity", running_entity): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["binary_sensor", "sensor", "input_boolean", "switch",
                        "select", "input_select"],
            )
        ),
        _opt("running_states", running_default): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=running_state_suggestions,
                custom_value=True,
                multiple=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        # ── Paused detection (optional) ────────────────────────────────────
        _opt("paused_entity", d.get("paused_entity")): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["binary_sensor", "sensor", "input_boolean", "switch",
                        "select", "input_select"],
            )
        ),
        _opt("paused_states", paused_default): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=paused_state_suggestions,
                custom_value=True,
                multiple=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        # ── Power sensor ────────────────────────────────────────────────────
        _opt("power_sensor", d.get("power_sensor")): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["sensor"])
        ),
    })

    if extra_actions:
        # mode=LIST renders as radio rows (one tap to pick + submit) instead
        # of a dropdown stacked above its own collapsed field. Matches the
        # navigation-menu look without restructuring the flow into a
        # multi-step async_show_menu chain.
        schema[vol.Required("action", default="save")] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=k, label=v)
                    for k, v in extra_actions.items()
                ],
                mode=selector.SelectSelectorMode.LIST,
            )
        )

    return vol.Schema(schema)


def _cycle_schema(defaults: Dict = None, show_add_another: bool = True) -> vol.Schema:
    d = defaults or {}
    schema: Dict = {
        vol.Required("name", default=d.get("name", "")): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        vol.Optional("cycle_id", default=d.get("cycle_id", "")): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        vol.Required(
            "duration_minutes",
            default=d.get("duration_minutes", 60),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=480, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Required(
            "total_kwh",
            default=d.get("total_kwh", 1.0),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.001, max=30.0, step=0.001, mode=selector.NumberSelectorMode.BOX)
        ),
    }
    if show_add_another:
        schema[vol.Optional("add_another", default=False)] = selector.BooleanSelector()
    return vol.Schema(schema)


def _parse_device(
    user_input: dict,
    slug_from: str = "",
    existing: Optional[dict] = None,
) -> dict:
    """Convert raw form values into stored device dict.

    For each field, prefer user_input if present, then fall back to the existing
    saved value, then the default. This way a partial form submission (HA can
    drop selector values from user_input in long forms with mixed schemas)
    never silently overwrites your saved choices with defaults.
    """
    e = existing or {}

    def _get(key, default):
        if key in user_input and user_input[key] is not None and user_input[key] != "":
            return user_input[key]
        if key in e:
            return e[key]
        return default

    _LOGGER.warning(
        "SmartStart parse for %r — user_input delay_handling=%r delay_step_minutes=%r; existing delay_handling=%r delay_step_minutes=%r",
        user_input.get("name"),
        user_input.get("delay_handling"),
        user_input.get("delay_step_minutes"),
        e.get("delay_handling"),
        e.get("delay_step_minutes"),
    )

    return {
        "device_slug":      _slug(user_input["name"]) if slug_from == "" else slug_from,
        "name":             user_input["name"],
        "icon":             user_input.get("icon") or e.get("icon") or "mdi:lightning-bolt",
        "start_service":    _get("start_service",   e.get("start_service", "")),
        "stop_service":     _get("stop_service",    e.get("stop_service", "")),
        "delay_handling":    _get("delay_handling",    DEFAULT_DELAY_HANDLING),
        "delay_step_minutes": int(_get("delay_step_minutes", DEFAULT_DELAY_STEP_MINUTES)),
        "balanced_target_rate": float(_get(
            "balanced_target_rate",
            e.get("balanced_target_rate", DEFAULT_BALANCED_TARGET_RATE),
        )),
        "min_delay_minutes": int(_get("min_delay_minutes", DEFAULT_MIN_DELAY_MINUTES)),
        "max_delay_minutes": int(_get("max_delay_minutes", DEFAULT_MAX_DELAY_MINUTES)),
        "auto_reschedule":  bool(_get("auto_reschedule", DEFAULT_AUTO_RESCHEDULE)),
        "offers":           bool(_get("offers", DEFAULT_OFFERS)),
        "max_wait_hours":   int(_get("max_wait_hours", DEFAULT_MAX_WAIT_HOURS)),
        # Hold requires auto_reschedule — force off if reschedule was disabled
        # (the form hides the hold toggle in that case, but a previously saved
        # True could otherwise linger).
        "hold_for_next_day_rates": (
            bool(_get("auto_reschedule", DEFAULT_AUTO_RESCHEDULE))
            and bool(_get("hold_for_next_day_rates", DEFAULT_HOLD_FOR_NEXT_DAY_RATES))
        ),
        "running_entity":   _get("running_entity",  e.get("running_entity", "")),
        "running_states":   _to_state_list(user_input.get("running_states")) or e.get("running_states", []),
        "paused_entity":    _get("paused_entity",   e.get("paused_entity", "")),
        "paused_states":    _to_state_list(user_input.get("paused_states")) or e.get("paused_states", []),
        "power_sensor":     _get("power_sensor",    e.get("power_sensor", "")),
        "cycles":           [],
    }


def _parse_cycle(user_input: dict, existing_cycle: Optional[dict] = None) -> dict:
    """Build a cycle dict.

    cycle_key: internal slug, set once at creation from the cycle name. Never
        edited — all storage and unique IDs key off this.
    cycle_id: optional, user-editable string sent to the appliance's start
        script as the `cycle_id` payload. If blank, the cycle name is sent.
    """
    existing = existing_cycle or {}
    cycle_key = existing.get("cycle_key") or existing.get("cycle_id") or _slug(user_input["name"])
    return {
        "cycle_key":        cycle_key,
        "cycle_id":         user_input.get("cycle_id", "").strip(),
        "name":             user_input["name"],
        "duration_minutes": int(user_input["duration_minutes"]),
        "total_kwh":        float(user_input["total_kwh"]),
    }


# ── Laundry pair helpers ──────────────────────────────────────────────────────

def _all_smartstart_devices(hass: HomeAssistant) -> List[Dict]:
    """Every appliance device across all non-laundry SmartStart entries."""
    devices: List[Dict] = []
    if hass is None:
        return devices
    for entry in hass.config_entries.async_entries(DOMAIN):
        config = entry.options if entry.options else entry.data
        if config.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_LAUNDRY:
            continue
        devices.extend(config.get("devices", []))
    return devices


def _device_select(hass: HomeAssistant) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=d["device_slug"], label=d["name"])
                for d in _all_smartstart_devices(hass)
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _laundry_pair_schema(hass: HomeAssistant, defaults: Dict = None) -> vol.Schema:
    d = defaults or {}
    schema: Dict = {
        vol.Required("name", default=d.get("name", "Laundry")): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        vol.Required(CONF_WASHER_SLUG, default=d.get(CONF_WASHER_SLUG, vol.UNDEFINED)): _device_select(hass),
        vol.Required(CONF_DRYER_SLUG, default=d.get(CONF_DRYER_SLUG, vol.UNDEFINED)): _device_select(hass),
        vol.Required(
            CONF_GAP_MINUTES, default=int(d.get(CONF_GAP_MINUTES, DEFAULT_GAP_MINUTES)),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=120, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Required(
            CONF_BEDTIME, default=d.get(CONF_BEDTIME, DEFAULT_BEDTIME),
        ): selector.TimeSelector(),
        vol.Required(
            CONF_WAKE_TIME, default=d.get(CONF_WAKE_TIME, DEFAULT_WAKE_TIME),
        ): selector.TimeSelector(),
        vol.Required(
            CONF_MAX_WET_WAIT,
            default=float(d.get(CONF_MAX_WET_WAIT, DEFAULT_MAX_WET_WAIT)),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=12, step=0.5, mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="h",
            )
        ),
        vol.Required(
            CONF_TIME_VALUE,
            default=float(d.get(CONF_TIME_VALUE, DEFAULT_TIME_VALUE)),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=2, step=0.01, mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Required(
            "max_wait_hours",
            default=int(d.get("max_wait_hours", DEFAULT_MAX_WAIT_HOURS)),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=72, step=1, mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="h",
            )
        ),
        vol.Optional(
            "auto_reschedule", default=bool(d.get("auto_reschedule", False)),
        ): selector.BooleanSelector(),
    }
    # Same dependent-hidden pattern as appliances: hold only appears once
    # auto_reschedule has been enabled and saved.
    if d.get("auto_reschedule"):
        schema[vol.Optional(
            "hold_for_next_day_rates",
            default=bool(d.get("hold_for_next_day_rates", False)),
        )] = selector.BooleanSelector()
    return vol.Schema(schema)


def _flow_step_cycle_options(hass: HomeAssistant, pair_cfg: Dict) -> List[selector.SelectOptionDict]:
    """Combined machine+cycle dropdown for the flow step builder."""
    devices = {d["device_slug"]: d for d in _all_smartstart_devices(hass)}
    options: List[selector.SelectOptionDict] = []
    for machine, slug, label in (
        (MACHINE_WASH, pair_cfg.get(CONF_WASHER_SLUG), "Washer"),
        (MACHINE_DRY, pair_cfg.get(CONF_DRYER_SLUG), "Dryer"),
    ):
        for cycle in devices.get(slug, {}).get("cycles", []):
            options.append(selector.SelectOptionDict(
                value=f"{machine}|{cycle['cycle_key']}",
                label=f"{label}: {cycle['name']}",
            ))
    return options


def _flow_step_schema(hass: HomeAssistant, pair_cfg: Dict) -> vol.Schema:
    return vol.Schema({
        vol.Required("cycle"): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=_flow_step_cycle_options(hass, pair_cfg),
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional("confirm_before", default=True): selector.BooleanSelector(),
        vol.Optional("duration_override", default=0): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=480, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional("kwh_override", default=0.0): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=30.0, step=0.001, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional("add_another", default=False): selector.BooleanSelector(),
    })


def _parse_flow_step(user_input: dict) -> dict:
    machine, cycle_key = user_input["cycle"].split("|", 1)
    duration = float(user_input.get("duration_override") or 0)
    kwh = float(user_input.get("kwh_override") or 0)
    return {
        "machine":          machine,
        "cycle_key":        cycle_key,
        "confirm_before":   bool(user_input.get("confirm_before", True)),
        # 0 = no override → use the cycle's configured/learned values
        "duration_minutes": duration if duration > 0 else None,
        "kwh":              kwh if kwh > 0 else None,
    }


def _repeat_days_field(flow: Dict) -> Dict:
    """Optional "repeat every N days" for a laundry flow (0 = off). When due,
    the flow auto-fills the next empty Load row with the due date as deadline."""
    return {
        vol.Optional("repeat_days", default=int(flow.get("repeat_days") or 0)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=0, max=60, step=1, mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="days",
            )),
        # Bed linen etc.: the first wash must start while you're up — never
        # loaded the night before for a night-time start.
        vol.Optional("overnight", default=bool(flow.get("overnight", False))):
            selector.BooleanSelector(),
    }


def _flow_summary(flow: Dict, devices_by_slug: Dict, pair_cfg: Dict) -> str:
    parts = []
    for s in flow.get("steps", []):
        slug = pair_cfg.get(CONF_WASHER_SLUG) if s["machine"] == MACHINE_WASH else pair_cfg.get(CONF_DRYER_SLUG)
        cycles = devices_by_slug.get(slug, {}).get("cycles", [])
        name = next((c["name"] for c in cycles if c["cycle_key"] == s["cycle_key"]), s["cycle_key"])
        marker = "" if s.get("confirm_before", True) else "*"  # * = auto-fires
        parts.append(f"{'W' if s['machine'] == MACHINE_WASH else 'D'}:{name}{marker}")
    return " → ".join(parts)


# ── Config flow ───────────────────────────────────────────────────────────────

class SmartStartConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._cfg: Dict = {}
        self._device: Dict = {}
        self._cycles: List[Dict] = []

    async def async_step_user(self, user_input=None):
        # Once appliances exist, a new entry can also be a laundry pair.
        if _all_smartstart_devices(self.hass):
            if user_input is not None and "setup_type" in user_input:
                if user_input["setup_type"] == ENTRY_TYPE_LAUNDRY:
                    return await self.async_step_laundry()
                return await self.async_step_provider()
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({
                    vol.Required("setup_type", default="appliance"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value="appliance",
                                    label="Appliance — schedule a washer, dryer, dishwasher…",
                                ),
                                selector.SelectOptionDict(
                                    value=ENTRY_TYPE_LAUNDRY,
                                    label="Laundry pair — chain wash → dry across two appliances",
                                ),
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }),
            )
        return await self.async_step_provider(user_input)

    async def async_step_laundry(self, user_input=None):
        if user_input is not None:
            data = {
                CONF_ENTRY_TYPE:  ENTRY_TYPE_LAUNDRY,
                "name":           user_input["name"],
                CONF_WASHER_SLUG: user_input[CONF_WASHER_SLUG],
                CONF_DRYER_SLUG:  user_input[CONF_DRYER_SLUG],
                CONF_GAP_MINUTES: int(user_input[CONF_GAP_MINUTES]),
                CONF_BEDTIME:     str(user_input.get(CONF_BEDTIME, DEFAULT_BEDTIME))[:5],
                CONF_WAKE_TIME:   str(user_input.get(CONF_WAKE_TIME, DEFAULT_WAKE_TIME))[:5],
                CONF_MAX_WET_WAIT: float(user_input.get(CONF_MAX_WET_WAIT, DEFAULT_MAX_WET_WAIT)),
                CONF_TIME_VALUE:  float(user_input.get(CONF_TIME_VALUE, DEFAULT_TIME_VALUE)),
                "max_wait_hours": int(user_input.get("max_wait_hours", DEFAULT_MAX_WAIT_HOURS)),
                "auto_reschedule": bool(user_input.get("auto_reschedule", False)),
                "hold_for_next_day_rates": False,
                CONF_FLOWS:       [],
            }
            return self.async_create_entry(title=user_input["name"], data=data)
        return self.async_show_form(
            step_id="laundry",
            data_schema=_laundry_pair_schema(self.hass),
        )

    async def async_step_provider(self, user_input=None):
        octopus = _detect_octopus(self.hass)
        providers: Dict[str, str] = {}
        if octopus:
            providers[PROVIDER_OCTOPUS_ENTITY] = "Octopus Energy (auto-detected ✓)"
        providers[PROVIDER_MANUAL] = "Manual — push rates via smartstart.publish_rates"

        if user_input is not None:
            # Currency + rate_unit are derived from HA's locale at setup time
            # (see __init__.py:_derive_currency_symbol). No need to ask.
            self._cfg = {
                "provider":        user_input["provider"],
                "provider_config": {},
                "devices":         [],
            }
            if user_input["provider"] == PROVIDER_OCTOPUS_ENTITY:
                return await self.async_step_octopus_config()
            return await self.async_step_add_device()

        return self.async_show_form(
            step_id="provider",
            data_schema=vol.Schema({
                vol.Required(
                    "provider",
                    default=PROVIDER_OCTOPUS_ENTITY if octopus else PROVIDER_MANUAL,
                ): vol.In(providers),
            }),
        )

    async def async_step_octopus_config(self, user_input=None):
        octopus = _detect_octopus(self.hass) or {}
        if user_input is not None:
            self._cfg["provider_config"] = {
                "current_day_entity": user_input["current_day_entity"],
                "next_day_entity":    user_input["next_day_entity"],
            }
            return await self.async_step_add_device()

        return self.async_show_form(
            step_id="octopus_config",
            data_schema=vol.Schema({
                vol.Required("current_day_entity", default=octopus.get("current", "")): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["event", "sensor"])
                ),
                vol.Required("next_day_entity", default=octopus.get("next", "")): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["event", "sensor"])
                ),
            }),
        )

    async def async_step_add_device(self, user_input=None):
        if user_input is not None:
            self._device = _parse_device(user_input)
            self._cycles = []
            return await self.async_step_add_cycle()

        return self.async_show_form(
            step_id="add_device",
            data_schema=_device_schema(self._device, hass=self.hass),
        )

    async def async_step_add_cycle(self, user_input=None):
        if user_input is not None:
            self._cycles.append(_parse_cycle(user_input))
            if user_input.get("add_another"):
                return await self.async_step_add_cycle()
            self._device["cycles"] = self._cycles
            self._cfg["devices"].append(self._device)
            return self.async_create_entry(title="SmartStart", data=self._cfg)

        return self.async_show_form(
            step_id="add_cycle",
            data_schema=_cycle_schema(),
            description_placeholders={"device": self._device.get("name", "")},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SmartStartOptionsFlow(config_entry)


# ── Options flow ──────────────────────────────────────────────────────────────

class SmartStartOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry) -> None:
        self._entry           = entry
        self._cfg             = dict(entry.options if entry.options else entry.data)
        self._device_idx:     Optional[int] = None
        self._device:         Dict = {}
        self._cycles:         List[Dict] = []
        self._editing_cycle_idx: Optional[int] = None
        self._flow_idx:       Optional[int] = None
        self._flow:           Dict = {}
        self._flow_steps:     List[Dict] = []

    def _finalise_and_reload(self, source: str):
        """Single exit point for options-flow saves. Logs what's being saved,
        triggers an explicit reload (HA's update-listener path is unreliable),
        then returns the create-entry result."""
        device_count = len(self._cfg.get("devices", []))
        _LOGGER.warning(
            "SmartStart: options-flow finalise (%s) — %d devices, scheduling reload",
            source, device_count,
        )
        # async_reload is async; schedule and let it run after we return
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self._entry.entry_id)
        )
        return self.async_create_entry(title="", data=self._cfg)

    async def async_step_init(self, user_input=None):
        if self._cfg.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_LAUNDRY:
            return await self.async_step_laundry_init(user_input)
        devices = self._cfg.get("devices", [])
        actions: Dict[str, str] = {
            "add_device":  "➕ Add new device",
            "edit_global": "⚙️ Edit global settings",
        }
        for i, d in enumerate(devices):
            actions[f"edit_{i}"]   = f"✏️ Edit: {d['name']}"
            actions[f"delete_{i}"] = f"🗑️ Delete: {d['name']}"

        if user_input is not None:
            choice = user_input["action"]
            if choice == "add_device":
                self._device    = {}
                self._cycles    = []
                self._device_idx = None
                return await self.async_step_add_device()
            if choice == "edit_global":
                return await self.async_step_edit_global()
            if choice.startswith("delete_"):
                idx = int(choice.split("_", 1)[1])
                devices.pop(idx)
                self._cfg["devices"] = devices
                return self._finalise_and_reload("delete_device")
            if choice.startswith("edit_"):
                idx = int(choice.split("_", 1)[1])
                self._device_idx = idx
                self._device     = dict(devices[idx])
                self._cycles     = list(self._device.get("cycles", []))
                return await self.async_step_edit_device()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required("action"): vol.In(actions)}),
        )

    # ── Laundry pair options ─────────────────────────────────────────────────

    async def async_step_laundry_init(self, user_input=None):
        flows = self._cfg.get(CONF_FLOWS, [])
        actions: Dict[str, str] = {
            "edit_pair": "⚙️ Edit pair settings",
            "add_flow":  "➕ Add flow",
        }
        for i, f in enumerate(flows):
            actions[f"edit_flow_{i}"]   = f"✏️ Edit flow: {f['name']}"
            actions[f"delete_flow_{i}"] = f"🗑️ Delete flow: {f['name']}"

        if user_input is not None:
            choice = user_input["action"]
            if choice == "edit_pair":
                return await self.async_step_laundry_pair()
            if choice == "add_flow":
                self._flow_idx = None
                self._flow = {}
                self._flow_steps = []
                return await self.async_step_laundry_flow_name()
            if choice.startswith("delete_flow_"):
                idx = int(choice.rsplit("_", 1)[1])
                if 0 <= idx < len(flows):
                    flows.pop(idx)
                self._cfg[CONF_FLOWS] = flows
                return self._finalise_and_reload("delete_flow")
            if choice.startswith("edit_flow_"):
                self._flow_idx = int(choice.rsplit("_", 1)[1])
                self._flow = dict(flows[self._flow_idx])
                self._flow_steps = list(self._flow.get("steps", []))
                return await self.async_step_laundry_edit_flow()

        devices_by_slug = {d["device_slug"]: d for d in _all_smartstart_devices(self.hass)}
        lines = [f"### {self._cfg.get('name', 'Laundry')}"]
        for f in flows:
            repeat = f" · every {f['repeat_days']}d" if f.get("repeat_days") else ""
            repeat += " · in use overnight" if f.get("overnight") else ""
            lines.append(f"- **{f['name']}**{repeat} — {_flow_summary(f, devices_by_slug, self._cfg)}")
        if flows:
            lines.append("")
            lines.append("_* = step auto-fires without a changeover confirm_")

        return self.async_show_form(
            step_id="laundry_init",
            data_schema=vol.Schema({
                vol.Required("action"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=k, label=v)
                            for k, v in actions.items()
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
            description_placeholders={"flows": "\n".join(lines)},
        )

    async def async_step_laundry_pair(self, user_input=None):
        if user_input is not None:
            auto = bool(user_input.get("auto_reschedule", False))
            self._cfg.update({
                "name":           user_input["name"],
                CONF_WASHER_SLUG: user_input[CONF_WASHER_SLUG],
                CONF_DRYER_SLUG:  user_input[CONF_DRYER_SLUG],
                CONF_GAP_MINUTES: int(user_input[CONF_GAP_MINUTES]),
                CONF_BEDTIME:     str(user_input.get(CONF_BEDTIME, DEFAULT_BEDTIME))[:5],
                CONF_WAKE_TIME:   str(user_input.get(CONF_WAKE_TIME, DEFAULT_WAKE_TIME))[:5],
                CONF_MAX_WET_WAIT: float(user_input.get(CONF_MAX_WET_WAIT, DEFAULT_MAX_WET_WAIT)),
                CONF_TIME_VALUE:  float(user_input.get(CONF_TIME_VALUE, DEFAULT_TIME_VALUE)),
                "max_wait_hours": int(user_input.get("max_wait_hours", DEFAULT_MAX_WAIT_HOURS)),
                "auto_reschedule": auto,
                # Hold requires auto_reschedule — same forced-off rule as devices
                "hold_for_next_day_rates": (
                    auto and bool(user_input.get(
                        "hold_for_next_day_rates",
                        self._cfg.get("hold_for_next_day_rates", False),
                    ))
                ),
            })
            return self._finalise_and_reload("laundry_pair")
        return self.async_show_form(
            step_id="laundry_pair",
            data_schema=_laundry_pair_schema(self.hass, self._cfg),
        )

    async def async_step_laundry_flow_name(self, user_input=None):
        if user_input is not None:
            self._flow = {
                "slug":  self._flow.get("slug") or _slug(user_input["name"]),
                "name":  user_input["name"],
                "steps": self._flow_steps,
                "repeat_days": int(user_input.get("repeat_days") or 0),
                "overnight": bool(user_input.get("overnight", False)),
            }
            return await self.async_step_laundry_flow_step()
        return self.async_show_form(
            step_id="laundry_flow_name",
            data_schema=vol.Schema({
                vol.Required("name", default=self._flow.get("name", "")): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                **_repeat_days_field(self._flow),
            }),
        )

    def _save_flow(self):
        self._flow["steps"] = self._flow_steps
        flows = self._cfg.get(CONF_FLOWS, [])
        if self._flow_idx is not None and self._flow_idx < len(flows):
            flows[self._flow_idx] = self._flow
        else:
            flows.append(self._flow)
        self._cfg[CONF_FLOWS] = flows
        return self._finalise_and_reload("save_flow")

    async def async_step_laundry_flow_step(self, user_input=None):
        if user_input is not None:
            self._flow_steps.append(_parse_flow_step(user_input))
            if user_input.get("add_another"):
                return await self.async_step_laundry_flow_step()
            return self._save_flow()
        return self.async_show_form(
            step_id="laundry_flow_step",
            data_schema=_flow_step_schema(self.hass, self._cfg),
            description_placeholders={
                "flow": self._flow.get("name", ""),
                "position": str(len(self._flow_steps) + 1),
            },
        )

    async def async_step_laundry_edit_flow(self, user_input=None):
        if user_input is not None:
            self._flow["repeat_days"] = int(user_input.get("repeat_days") or 0)
            self._flow["overnight"] = bool(user_input.get("overnight", False))
            choice = user_input["action"]
            if choice == "save":
                return self._save_flow()
            if choice == "add_step":
                return await self.async_step_laundry_flow_step()
            if choice.startswith("delete_step_"):
                idx = int(choice.rsplit("_", 1)[1])
                if 0 <= idx < len(self._flow_steps):
                    self._flow_steps.pop(idx)
                return await self.async_step_laundry_edit_flow()

        devices_by_slug = {d["device_slug"]: d for d in _all_smartstart_devices(self.hass)}
        summary = _flow_summary(
            {"steps": self._flow_steps}, devices_by_slug, self._cfg
        ) or "_no steps yet_"
        actions: Dict[str, str] = {"save": "✅ Save", "add_step": "➕ Add step"}
        for i, s in enumerate(self._flow_steps):
            actions[f"delete_step_{i}"] = f"🗑️ Delete step {i + 1}"
        return self.async_show_form(
            step_id="laundry_edit_flow",
            data_schema=vol.Schema({
                vol.Required("action", default="save"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=k, label=v)
                            for k, v in actions.items()
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                **_repeat_days_field(self._flow),
            }),
            description_placeholders={
                "flow": self._flow.get("name", ""),
                "steps": summary,
            },
        )

    async def async_step_edit_global(self, user_input=None):
        if user_input is not None:
            self._cfg.update({
                CONF_PREFER_SOONER_DIFF:     float(user_input[CONF_PREFER_SOONER_DIFF]),
                CONF_CHEAP_WINDOW_THRESHOLD: float(user_input[CONF_CHEAP_WINDOW_THRESHOLD]),
                CONF_RESCHEDULE_MIN_SAVING:  float(user_input[CONF_RESCHEDULE_MIN_SAVING]),
                CONF_RATES_EXPECTED_AT:      str(user_input[CONF_RATES_EXPECTED_AT]),
                CONF_HOLD_ENGAGE_LEAD:       int(user_input[CONF_HOLD_ENGAGE_LEAD]),
                CONF_BLACKOUT_CALENDARS:     list(user_input.get(CONF_BLACKOUT_CALENDARS) or []),
            })
            return self._finalise_and_reload("edit_global")

        return self.async_show_form(
            step_id="edit_global",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_PREFER_SOONER_DIFF,
                    default=float(
                        self._cfg.get(CONF_PREFER_SOONER_DIFF, PREFER_SOONER_RATE_DIFF)
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
                vol.Required(
                    CONF_CHEAP_WINDOW_THRESHOLD,
                    default=float(
                        self._cfg.get(CONF_CHEAP_WINDOW_THRESHOLD, DEFAULT_CHEAP_WINDOW_THRESHOLD)
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=-1.0, max=1.0)),
                vol.Required(
                    CONF_RESCHEDULE_MIN_SAVING,
                    default=float(
                        self._cfg.get(CONF_RESCHEDULE_MIN_SAVING, DEFAULT_RESCHEDULE_MIN_SAVING)
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
                vol.Required(
                    CONF_RATES_EXPECTED_AT,
                    default=str(
                        self._cfg.get(CONF_RATES_EXPECTED_AT, DEFAULT_RATES_EXPECTED_AT)
                    ),
                ): selector.TimeSelector(),
                vol.Required(
                    CONF_HOLD_ENGAGE_LEAD,
                    default=int(
                        self._cfg.get(CONF_HOLD_ENGAGE_LEAD, DEFAULT_HOLD_ENGAGE_LEAD)
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=720)),
                vol.Optional(
                    CONF_BLACKOUT_CALENDARS,
                    default=list(
                        self._cfg.get(CONF_BLACKOUT_CALENDARS)
                        if self._cfg.get(CONF_BLACKOUT_CALENDARS) is not None
                        else autodetect_calendars(self.hass)
                    ),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="calendar", multiple=True)
                ),
            }),
        )

    async def async_step_add_device(self, user_input=None):
        if user_input is not None:
            self._device = _parse_device(user_input)
            self._cycles = []
            return await self.async_step_add_cycle()

        return self.async_show_form(
            step_id="add_device",
            data_schema=_device_schema(self._device, hass=self.hass),
        )

    async def async_step_edit_device(self, user_input=None):
        d = self._device
        
        if not hasattr(self, "_cycles"):
            self._cycles = self._device.get("cycles", [])
    
        if user_input is not None:
            action = user_input.pop("action", "save")

            if action == "add_cycle":
                return await self.async_step_add_cycle()

            if action.startswith("edit_cycle_"):
                self._editing_cycle_idx = int(action.rsplit("_", 1)[1])
                return await self.async_step_edit_cycle()

            if action.startswith("delete_cycle_"):
                idx = int(action.rsplit("_", 1)[1])
                if 0 <= idx < len(self._cycles):
                    self._cycles.pop(idx)
                self._device["cycles"] = self._cycles
                return await self.async_step_edit_device()

            if action == "view_debug":
                return await self.async_step_view_debug()

            # save
            updated = _parse_device(
                user_input,
                slug_from=d.get("device_slug", _slug(user_input["name"])),
                existing=d,
            )
            updated["cycles"] = self._cycles
            self._device = updated

            devices = self._cfg.get("devices", [])
            if self._device_idx is not None and self._device_idx < len(devices):
                devices[self._device_idx] = self._device
            else:
                devices.append(self._device)
            self._cfg["devices"] = devices
            return self._finalise_and_reload("edit_device")

        # Build action menu
        cycle_actions: Dict[str, str] = {}
        for i, c in enumerate(self._cycles):
            cycle_actions[f"edit_cycle_{i}"]   = f"✏️ Edit cycle: {c['name']}"
            cycle_actions[f"delete_cycle_{i}"] = f"🗑️ Delete cycle: {c['name']}"
        all_actions = {
            "save":       "✅ Save",
            "add_cycle":  "➕ Add cycle",
            **cycle_actions,
            "view_debug": "🔍 Debug / inspect curves",
        }

        return self.async_show_form(
            step_id="edit_device",
            data_schema=_device_schema(d, hass=self.hass, extra_actions=all_actions),
        )

    async def async_step_edit_cycle(self, user_input=None):
        idx   = self._editing_cycle_idx
        cycle = self._cycles[idx]
        if user_input is not None:
            self._cycles[idx] = _parse_cycle(user_input, existing_cycle=cycle)
            self._device["cycles"] = self._cycles
            return await self.async_step_edit_device()

        return self.async_show_form(
            step_id="edit_cycle",
            data_schema=_cycle_schema(cycle, show_add_another=False),
            description_placeholders={"device": self._device.get("name", "")},
        )

    async def async_step_view_debug(self, user_input=None):
        """Read-only inspection of curve_store data for the current device.

        Also handles per-cycle "clear curves" and device-level "clear stats" actions.
        """
        slug = self._device.get("device_slug", "")
        store = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        curve_store = store.get("curve_store")
        cycles = self._cycles or []

        if user_input is not None:
            action = user_input.get("action", "back")
            if action.startswith("clear_curves_") and curve_store:
                ck = action[len("clear_curves_"):]
                await curve_store.async_clear_curves(slug, ck)
            elif action == "clear_device_stats" and curve_store:
                await curve_store.async_clear_device_stats(slug)
            if action == "back":
                return await self.async_step_edit_device()
            return await self.async_step_view_debug()

        # ── Build the Markdown description ────────────────────────────────────
        lines: List[str] = [f"### {self._device.get('name', slug)}"]

        if not curve_store:
            lines.append("_curve_store unavailable_")
        else:
            for c in cycles:
                ck   = c.get("cycle_key") or c.get("cycle_id") or ""
                info = curve_store.get_confidence_info(slug, ck)
                prof = curve_store.get_profile(slug, ck) or []
                lines.append("")
                lines.append(f"**{c.get('name', ck)}**  `{ck}`")
                lines.append(
                    f"- curves: {info.get('n_curves', 0)}  "
                    f"· confidence: {int(round((info.get('confidence') or 0) * 100))}% "
                    f"({info.get('status', '—')})"
                )
                avg_d = info.get("avg_duration_minutes")
                std_d = info.get("duration_std_minutes")
                if avg_d is not None:
                    if std_d is not None:
                        lines.append(f"- avg duration: {avg_d:.1f} min (±{std_d:.1f})")
                    else:
                        lines.append(f"- avg duration: {avg_d:.1f} min")
                lines.append(f"- profile type: {info.get('profile_type', '—')}")
                if info.get("last_captured_at"):
                    lines.append(f"- last captured: {info['last_captured_at']}")
                if info.get("warning"):
                    lines.append("- ⚠️ inconsistent — see persistent notification")
                if prof:
                    lines.append(f"- profile: `{_profile_bars(prof)}`")

            last = curve_store.get_last_run(slug)
            if last:
                lines.append("")
                lines.append("**Last cycle**")
                lines.append(
                    f"- {last.get('cycle_name', '—')} · "
                    f"{last.get('kwh', 0)} kWh · "
                    f"£{last.get('cost', 0)} · "
                    f"{last.get('duration_minutes', 0)} min"
                )
                if last.get("completed_at"):
                    lines.append(f"- completed: {last['completed_at']}")

            totals = curve_store.get_totals(slug)
            if totals.get("total_run_count"):
                lines.append("")
                lines.append("**Totals**")
                lines.append(
                    f"- runs: {totals['total_run_count']} · "
                    f"{totals.get('total_kwh', 0)} kWh · "
                    f"£{totals.get('total_cost', 0)} · "
                    f"{totals.get('total_minutes', 0)} min"
                )

        description = "\n".join(lines)

        # ── Action menu ──────────────────────────────────────────────────────
        actions: Dict[str, str] = {"back": "← Back"}
        for c in cycles:
            ck = c.get("cycle_key") or c.get("cycle_id") or ""
            actions[f"clear_curves_{ck}"] = f"🗑️ Clear curves: {c.get('name', ck)}"
        actions["clear_device_stats"] = "🗑️ Clear last cycle + totals"

        return self.async_show_form(
            step_id="view_debug",
            data_schema=vol.Schema({
                vol.Required("action", default="back"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=k, label=v)
                            for k, v in actions.items()
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
            description_placeholders={"debug": description},
        )

    async def async_step_add_cycle(self, user_input=None):
        if user_input is not None:
            self._cycles.append(_parse_cycle(user_input))
            if user_input.get("add_another"):
                return await self.async_step_add_cycle()

            self._device["cycles"] = self._cycles
            devices = self._cfg.get("devices", [])
            if self._device_idx is not None and self._device_idx < len(devices):
                devices[self._device_idx] = self._device
            else:
                devices.append(self._device)
            self._cfg["devices"] = devices
            return self._finalise_and_reload("add_cycle")

        return self.async_show_form(
            step_id="add_cycle",
            data_schema=_cycle_schema(),
            description_placeholders={"device": self._device.get("name", "")},
        )
