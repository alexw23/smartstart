DOMAIN = "smartstart"

# Global config keys
CONF_CURRENCY_SYMBOL = "currency_symbol"
CONF_RATE_UNIT = "rate_unit"
CONF_PROVIDER = "provider"
CONF_PROVIDER_CONFIG = "provider_config"
CONF_DEVICES = "devices"

# Device config keys
CONF_DEVICE_SLUG = "device_slug"
CONF_NAME = "name"
CONF_ICON = "icon"
CONF_START_SERVICE = "start_service"
CONF_STOP_SERVICE = "stop_service"
CONF_DELAY_MODE = "delay_mode"
CONF_MIN_DELAY_MINUTES = "min_delay_minutes"
CONF_MAX_DELAY_MINUTES = "max_delay_minutes"
CONF_CYCLES = "cycles"
CONF_RUNNING_ENTITY = "running_entity"
CONF_RUNNING_STATES = "running_states"
CONF_PAUSED_STATES  = "paused_states"

# Cycle config keys
CONF_CYCLE_ID = "cycle_id"
CONF_DURATION_MINUTES = "duration_minutes"
CONF_TOTAL_KWH = "total_kwh"

# Providers
PROVIDER_OCTOPUS_ENTITY = "octopus_entity"
PROVIDER_MANUAL = "manual"
CONF_CURRENT_DAY_ENTITY = "current_day_entity"
CONF_NEXT_DAY_ENTITY = "next_day_entity"

# Delay modes
DELAY_MODE_NATIVE = "native"    # legacy alias kept for migration logging
DELAY_MODE_MANAGED = "managed"  # legacy alias

# Three-way delay handling — replaces the old delay_mode (native|managed) pair.
#   start_delay: appliance handles delay via a "delay start by X" feature.
#                max_delay_minutes = latest start time. Script receives delay_seconds.
#   end_time:    appliance handles delay via a "finish by X" feature (LG-style).
#                max_delay_minutes = latest finish time; effective start cap =
#                max_delay - cycle_duration. Script receives start_time +
#                cycle_duration so it can build the appliance's end-time command.
#   managed:     appliance has no native scheduling. SmartStart owns the timer.
#                max_delay_minutes = user preference; 0 or blank = unbounded
#                (search the full loaded rate horizon).
DELAY_HANDLING_START_DELAY = "start_delay"
DELAY_HANDLING_END_TIME    = "end_time"
DELAY_HANDLING_MANAGED     = "managed"
DEFAULT_DELAY_HANDLING     = DELAY_HANDLING_START_DELAY

# Per-device delay step (minutes): the smallest increment of delay the
# appliance can accept. Scoring restricts candidate start times to multiples
# of this step from now, and the script payload's delay_seconds is rounded to
# a multiple of step*60 before dispatch.
#   1   → minute precision (smart appliances, plain scripts)
#   15  → 15-minute increments (some washers)
#   30  → 30-minute increments (other washers)
#   60  → whole-hour reservations (LG-style dryers)
# Legacy values "minutes" / "whole_hours" from earlier installs are migrated
# to 1 / 60 at setup; see __init__.py.
DEFAULT_DELAY_STEP_MINUTES = 1

# Per-device scheduling mode — how aggressively to optimise.
#   now          : never delay, always start immediately
#   balanced     : pick the soonest window whose rate is <= target_rate.
#                  Falls back to max_savings if no qualifying window exists.
#   max_savings  : pick the cheapest window in the search horizon (current
#                  behaviour for existing devices)
# The global prefer_sooner_diff threshold still applies on top of all modes
# as a "don't bother waiting for pennies" filter.
MODE_NOW          = "now"
MODE_BALANCED     = "balanced"
MODE_MAX_SAVINGS  = "max_savings"
DEFAULT_MODE      = MODE_MAX_SAVINGS
# Target rate for balanced mode (£/kWh). Below this, a window is "good enough".
DEFAULT_BALANCED_TARGET_RATE = 0.05  # 5p/kWh

# Defaults
DEFAULT_MAX_DELAY_MINUTES = 1440
DEFAULT_MIN_DELAY_MINUTES = 0
DEFAULT_CURRENCY_SYMBOL = "£"
DEFAULT_RATE_UNIT = "p"

# ISO 4217 → display symbol used for £-formatted text (cost figures, reasons).
# Falls back to the ISO code itself if unknown — better than crashing or
# silently defaulting to "£" for, say, a Japanese yen user.
CURRENCY_SYMBOLS = {
    "GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥",
    "AUD": "$", "CAD": "$", "NZD": "$", "CHF": "Fr",
    "SEK": "kr", "NOK": "kr", "DKK": "kr", "INR": "₹",
    "PLN": "zł", "CZK": "Kč", "HUF": "Ft",
}
# Per-currency subunit label for rate-formatted strings ("p/kWh", "¢/kWh").
# Empty string when there's no natural subunit (or we don't know it) — sensor
# code falls back to the main symbol with 4 decimals.
RATE_UNITS_FOR_CURRENCY = {
    "GBP": "p", "USD": "¢", "EUR": "c",
}
DEFAULT_DELAY_MODE = DELAY_MODE_NATIVE
DEFAULT_RUNNING_STATES = ["on"]
PREFER_SOONER_RATE_DIFF = 0.03  # default — don't defer if saving < this per kWh
CONF_PREFER_SOONER_DIFF = "prefer_sooner_diff"

CONF_CHEAP_WINDOW_THRESHOLD   = "cheap_window_threshold"
DEFAULT_CHEAP_WINDOW_THRESHOLD = 0.05  # £/kWh — fire smartstart_cheap_window below this

CONF_RESCHEDULE_MIN_SAVING     = "reschedule_min_saving"
DEFAULT_RESCHEDULE_MIN_SAVING  = 0.05  # £/kWh — fire smartstart_better_window_found below this

# Per-device auto-reschedule (default off). When enabled, a SCHEDULED cycle is
# automatically moved to a cheaper window as new rates arrive (e.g. Octopus
# next-day rates landing at 4pm after a 2pm scheduling decision). The new
# window must fit the envelope committed at schedule time: the finish-by
# deadline if one was set, otherwise schedule-time + max_delay. Any improvement
# beyond RESCHEDULE_RATE_EPSILON triggers a move — the epsilon only exists to
# stop float noise re-commanding the appliance. The reschedule_min_saving
# threshold applies only to the notify-only smartstart_better_window_found path.
CONF_AUTO_RESCHEDULE     = "auto_reschedule"
DEFAULT_AUTO_RESCHEDULE  = False
RESCHEDULE_RATE_EPSILON  = 0.001  # £/kWh (0.1p)

# Hold-for-next-day-rates (per-device, default off; requires auto_reschedule).
# Within the engage window before the tariff's next-day publication (Agile:
# ~16:00), a Start commits to a *placeholder* — the cheapest KNOWN window
# starting after publication + buffer — instead of gambling on today's data.
# The appliance receives its delayed-start command immediately (no remote-
# start sleep issue), and when the rates actually land the auto-reschedule
# path re-optimises, possibly into tomorrow. If publication is late, the
# placeholder simply runs: it was already the best choice on known data,
# so no timeout/fallback timer is needed.
# Blackout windows (global): calendar entities whose events are periods a cycle
# must not overlap — Octopus Octoplus power-down / saving sessions by default
# (auto-detected when this is unset; an explicit empty list turns it off).
# Per device, a "Power-down" select picks what happens to a cycle that would
# overlap one: Avoid (never schedule over it) or Ignore (today's behaviour).
# Pause-and-resume for appliances that can pause is a planned third option.
CONF_BLACKOUT_CALENDARS    = "blackout_calendars"
POWER_DOWN_AVOID           = "avoid"
POWER_DOWN_IGNORE          = "ignore"
DEFAULT_POWER_DOWN         = POWER_DOWN_AVOID

CONF_HOLD_FOR_NEXT_DAY_RATES    = "hold_for_next_day_rates"
DEFAULT_HOLD_FOR_NEXT_DAY_RATES = False
CONF_RATES_EXPECTED_AT     = "rates_expected_at"   # local HH:MM, global setting
DEFAULT_RATES_EXPECTED_AT  = "16:00"
CONF_HOLD_ENGAGE_LEAD      = "hold_engage_lead_minutes"  # only hold this close to publication
DEFAULT_HOLD_ENGAGE_LEAD   = 90
HOLD_PUBLICATION_BUFFER_MIN = 15  # placeholder must start ≥ publication + this
PLANNER_HORIZON_HOURS = 48        # best-windows list: real + forecast this far ahead
PLANNER_WINDOWS = 3               # how many windows the list shows
PLANNER_MIN_SEPARATION_MIN = 360  # listed windows start ≥ this far apart (distinct troughs)

# Wait limit (per-device): how long a run may be pushed back, counted from the
# Start press — the auto-reschedule envelope (finish by press + this). Fixed at
# the press so successive reschedules can't creep; separate from
# max_delay_minutes, which is the appliance's own delay reach (targets past it
# are re-sent once in reach). 0 = legacy: envelope = press + max_delay.
CONF_MAX_WAIT_HOURS     = "max_wait_hours"
DEFAULT_MAX_WAIT_HOURS  = 36

# Offers (per-device, default on): while a run is scheduled, a window the
# schedule can't reach on its own — forecast slots, or beyond its envelope —
# that is meaningfully cheaper is surfaced as an offer (sensor, button,
# smartstart_offer_found) for the user to accept. Accepting is the only thing
# that widens a run's envelope, and offers only look max_wait_hours past the
# original Start press, so however often one is accepted the run is bounded
# from the moment it was started. Forecast slots are priced at their
# central estimate (the band travels with the offer): accepting is the user's
# call, and if real rates disappoint, auto-reschedule falls back to the
# cheapest confirmed window inside the accepted deadline.
CONF_OFFERS          = "offers"
DEFAULT_OFFERS       = True
OFFER_MIN_SAVING     = 0.10   # £ per cycle
OFFER_MIN_FRACTION   = 0.25   # and at least this much cheaper than the plan
OFFER_REFIRE_SHIFT_MIN = 120  # re-announce only if the offered start moves this far

# Device status states
STATE_IDLE = "idle"
STATE_SCHEDULED = "scheduled"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"

# Sentinel for cycles the appliance started without SmartStart scheduling them.
# Tracked as a separate bucket so learned curves / durations stay trustworthy.
EXTERNAL_CYCLE_KEY  = "_external"
EXTERNAL_CYCLE_NAME = "Unknown"
STATE_ERROR = "error"

# Services
SERVICE_PUBLISH_RATES = "publish_rates"
SERVICE_PUBLISH_PREDICTED_RATES = "publish_predicted_rates"
SERVICE_START = "start"
SERVICE_STOP = "stop"
SERVICE_START_LAUNDRY = "start_laundry"
SERVICE_ACCEPT_OFFER = "accept_offer"
SERVICE_LAUNDRY_WINDOWS = "laundry_windows"

# ── Laundry pair (wash → dry chaining) ────────────────────────────────────────
# A laundry entry pairs an existing washer + dryer device (possibly owned by
# other config entries) and schedules flat step plans across them as one
# composite virtual cycle. See laundry_plan.py / laundry.py.
CONF_ENTRY_TYPE   = "entry_type"
ENTRY_TYPE_LAUNDRY = "laundry"
CONF_WASHER_SLUG  = "washer_slug"
CONF_DRYER_SLUG   = "dryer_slug"
CONF_GAP_MINUTES  = "gap_minutes"
CONF_FLOWS        = "flows"
DEFAULT_GAP_MINUTES = 15
LAUNDRY_ROWS      = 6          # plan rows ("Load N" selects)
CONF_BEDTIME       = "bedtime"             # local HH:MM — no changeovers from here…
CONF_WAKE_TIME     = "wake_time"           # …until here (bedtime == wake → off)
CONF_MAX_WET_WAIT  = "max_wet_wait_hours"  # cap on washed laundry waiting for its dry (0 = off)
DEFAULT_BEDTIME    = "23:00"
DEFAULT_WAKE_TIME  = "07:00"
DEFAULT_MAX_WET_WAIT = 4.0
# Load-order optimiser: plans are compared on cost + this much per hour until
# the last load finishes (user chose a cost/time blend over either alone).
CONF_TIME_VALUE    = "time_value_per_hour"
DEFAULT_TIME_VALUE = 0.05                  # £ (major units) per hour
LAUNDRY_ORDER_OPTIMISED = "Optimised"
LAUNDRY_ORDER_AS_LISTED = "As listed"
# Exhaustive permutations up to this many loads; local search beyond.
LAUNDRY_ORDER_EXHAUSTIVE_MAX = 4
ROW_NONE_LABEL    = "None"
FLOW_LABEL_PREFIX = "Flow: "   # legacy flow label (pre-merge rows); flows now show by name
WASH_LABEL_PREFIX = "Wash: "   # single wash cycle in a Load dropdown
DRY_LABEL_PREFIX  = "Dry: "    # single dry cycle in a Load dropdown

# Laundry run statuses (the pair's Status sensor)
LAUNDRY_STATE_IDLE      = "idle"
LAUNDRY_STATE_SCHEDULED = "scheduled"
LAUNDRY_STATE_RUNNING   = "running"
LAUNDRY_STATE_AWAITING  = "awaiting_changeover"
LAUNDRY_STATE_COMPLETE  = "complete"

LAUNDRY_STORAGE_KEY = f"{DOMAIN}_laundry"

# Storage
STORAGE_KEY = f"{DOMAIN}_schedules"
STORAGE_VERSION = 1

# Coordinator update interval
UPDATE_INTERVAL_MINUTES = 30

# Power curve learning
CONF_POWER_SENSOR       = "power_sensor"
CURVE_N_BUCKETS         = 20    # time divisions for power profile
CURVE_SAMPLE_INTERVAL_S = 60    # seconds between power readings
CURVE_MIN_SAMPLES       = 5     # min samples required to learn a power curve
MIN_CYCLE_DURATION_S    = 300   # wall-clock seconds; shorter runs are not recorded
# A run far shorter than the cycle's own median duration is an aborted/interrupted
# run, not a representative curve — reject it from curve learning so it can't
# pollute the averaged profile. Only applied once ≥ CURVE_MIN_CURVES exist.
CURVE_MIN_DURATION_FRACTION = 0.5

# When the running entity goes unavailable/unknown while a cycle is RUNNING or
# PAUSED, hold the state for this many seconds before falling through to IDLE.
# Lets cycles survive a brief HA restart or sensor blip without the
# state-change watcher concluding the cycle ended. If the sensor comes back
# during the window we cancel the fall-through and resume tracking the same
# cycle. After this elapses without recovery, we end the cycle normally.
UNAVAILABLE_GRACE_SECONDS = 600  # 10 minutes
CURVE_MIN_CURVES        = 3     # curves needed before profile is used
CURVE_CONFIDENT_CURVES  = 5     # curves needed for full-confidence weighting
CURVE_USE_THRESHOLD     = 0.40  # confidence at which profile scoring activates
CURVE_WARN_THRESHOLD    = 0.25  # warn if ≥ CONFIDENT_CURVES but below this
CURVE_FLAT_CV           = 0.30  # mean-profile CV below this → "flat" (dryer-like)
