# SmartStart

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/alexw23/smartstart.svg)](https://github.com/alexw23/smartstart/releases)
[![License](https://img.shields.io/github/license/alexw23/smartstart.svg)](LICENSE)

**Run your appliances when electricity is cheapest.** SmartStart is a [Home Assistant](https://www.home-assistant.io/)
custom integration that schedules your dishwasher, washing machine, dryer, EV charger or anything else Home Assistant
can start around **dynamic energy pricing**: Octopus Agile and similar time-of-use tariffs, or any rate data you feed it.

> [!WARNING]
> **Beta software.** SmartStart is under active development: settings, entities and behaviour can change between
> releases, and it has not been tested across many homes, tariffs or appliances. It starts real appliances. Try it
> on one device first, and keep an eye on what it schedules.

> [!NOTE]
> **Built with AI assistance.** Much of this project was written with [Claude](https://claude.com/claude-code), an AI
> coding assistant. It runs in its author's own home, but treat it like any young open-source project: read what it
> does before you trust it, and report anything odd.

## What it does

- **Finds the cheapest window.** Give it a cycle's duration and energy use and it picks the cheapest contiguous slot
  inside your limits, then starts the appliance then, using the appliance's own delayed start or a timer SmartStart
  manages itself. Optional **Finish by** deadlines, a **Start now** override, and three modes: *Max Savings*,
  *Balanced* (soonest acceptable window) and *Now*.
- **Understands your tariff.** Octopus Energy rates are picked up automatically (see [Rate providers](#rate-providers));
  anything else can be pushed in with the `smartstart.publish_rates` service, and forecasts with
  `smartstart.publish_predicted_rates`. When new rates land, scheduled runs can move to a cheaper window by themselves,
  or near the daily release SmartStart can hold for tomorrow's prices.
- **Negative prices and solar.** Rates are just numbers per slot, so negative prices naturally become the best
  windows, and forecast negative prices can be published ahead of time. SmartStart has no solar integration of its own,
  but you can use the same service to publish a rate curve where sunny, surplus-solar slots are cheap (or negative)
  and it will pull cycles into them.
- **Keeps out of power-downs.** Octopus Octoplus power-down events and saving sessions are discovered automatically
  (any calendar entity works), and each appliance chooses to *Avoid* them or *Ignore* them. See
  [Power-down windows](#power-down-windows-octopus-octoplus-and-other-calendars).
- **Plans your laundry.** A [laundry pair](#laundry-pair-wash--dry-chaining) schedules wash then dry as one plan: it
  orders your loads to cost the least, waits for changeovers around your sleep and away times, and splits the plan
  around power-downs. Define **flows** (e.g. "Bed linen" = a wash plus a dry), set them to repeat every N days, and it
  fills your plan when one is due. It records how often you *actually* run each flow and shows that against the
  configured interval, so you can see whether your routine matches your plan and how often you can realistically run
  the machines.
- **Offers cheaper options.** If a much cheaper window exists that the current schedule can't reach (a forecast slot,
  or past the appliance's own delay limit) you get a one-tap offer.
- **Learns your appliances.** With an optional power sensor it learns each cycle's real duration and power curve,
  and records runs, cost and savings.
- **A sidebar panel** with rate charts, recommended windows, run history, cycle statistics and the laundry planner.

## Good to know

**Sleep windows are for laundry only.** A laundry pair never plans a changeover while you're asleep (default
23:00-07:00, configurable per pair). Single appliances ignore it: a dishwasher can run at 3 a.m. because it needs
nobody there.

**Power monitoring is optional.** A power sensor lets SmartStart learn real cycle durations and curves, but everything
works without one using the duration and kWh you enter. SmartStart only *reads* the sensor; it never switches
power to an appliance. It only calls the start (and optional cancel) service you configure.

> [!CAUTION]
> **Adding power monitoring to an appliance carries risk, and managing that risk is your responsibility.** Fitting a
> smart plug or inline meter adds a connection and, usually, a relay in the appliance's supply. Washing machines, dryers
> and dishwashers draw a lot of current: use a device rated well above the appliance's maximum load, and prefer
> **relay-free** monitoring (a clip-on CT clamp or a measurement-only meter) where you can. Do not use a relay to
> switch an appliance off while it is running. SmartStart does not need you to, and does not do it for you. If you
> are not sure, skip the power sensor.

---

## Power-down windows (Octopus Octoplus and other calendars)

Appliances like dishwashers can't be paused mid-cycle, so a cycle that straddles
a power-down hour either costs you the reward or has to be interrupted by hand.
SmartStart reads calendar entities and treats their events as **blackout
windows**: no scheduled window may overlap one. A cycle that finishes exactly as
a blackout starts, or starts exactly as it ends, is fine.

- **Calendars** — *Global settings → Power-down calendars*. Left unset, the
  Octopus Energy integration's Octoplus power-down and saving-session calendars
  are detected automatically; clear the list to switch the feature off. Any
  calendar entity works, so other tariffs' events can be mirrored into one.
- **Per appliance** — each device gets a **Power-down Windows** select (also on
  the panel): **Avoid** (default; never schedule over a blackout) or **Ignore**
  (the old behaviour). It can only be changed while the device is idle, like the
  other scheduling selects.
- **Already scheduled** — with *Auto-reschedule* on, a run whose window ends up
  overlapping a newly announced blackout is moved clear of it even if the new
  window costs more.
- **Mode Now** deliberately starts immediately even over a blackout (it is the
  explicit override); every other mode never lands on a blocked "now".
- **Not covered yet** — laundry pairs (their composite plans have idle
  changeover gaps and need per-step handling), and a *Pause* option for
  appliances that can pause remotely.

---

## Laundry pair (wash → dry chaining)

Pair a washer and dryer (add a new SmartStart entry and pick **Laundry pair**)
and SmartStart schedules multi-step laundry plans as **one composite cycle**:
the whole plan — wash, changeover gap, dry, possibly more loads — is scored
together, so the energy-hungry dryer pulls the start time toward its cheap
slots even though only the washer is ever machine-commanded.

A plan is a flat ordered list of wash/dry steps with two rules (you never wire
dependencies): same-machine steps queue, and a dry waits for every wash listed
before it. Parallelism is implicit — plan two full loads and load 2's wash
runs during load 1's dry automatically.

**Runtime UI** — up to 6 **Load** selects (`Load 1`…`Load 6`), each one of:

- a saved **flow** by name (`Linen Sheets`, `Dark Clothes`) — brings its own
  steps; this is the everyday choice,
- a single cycle — `Wash: Cotton 40` or `Dry: Cupboard Eco` (a one-off
  wash+dry is two Loads; a combo you use often belongs in a flow),
- or `None`.

Loads run in list order, so `Wash: Delicate 800` then a dry-first flow
(`2× Iron Eco`) composes exactly as before.

**Flows** are defined once in the pair's options (ordered steps: cycle,
confirm-before toggle, optional duration/energy overrides — e.g. "sheets run
~1 h, not the cycle's nominal 2 h 30"). A step with confirm-before off
auto-fires (re-running the dryer on the same load, no trip needed).

**Repeat every N days** (per flow, 0 = off): SmartStart records when each flow
last completed. Once one is due within the next 48 h it fills the next empty
Load (once per due date — clear it and it stays cleared) and its due date
becomes the plan's **Due By**, unless you've set an earlier one. Fires
`smartstart_laundry_load_due` (`flow`, `due_at`, `load`) for notifications;
the Status sensor's `flows_due` attribute lists every interval and next due.
A repeating flow doesn't linger in Load 1 after its run.

**Sleep window** (pair options: Bedtime / Wake time, default 23:00–07:00):
changeovers are never *planned* while you're asleep. Starting the wash at
02:30 is fine (the machine needs no one), but the dryer then waits for 07:00 —
and the scorer prices each candidate with its real timeline, so a cheap night
wash whose dryer would land in the expensive morning loses to, say, an
evening plan you can finish before bed. **Max wet wait** (default 4 h, 0 =
off) rules out starts that leave washed laundry sitting longer than that; an
immediate start (Mode Now) is exempt. **Changeover After** (per-plan select,
cleared after Start) overrides it ad hoc — a lie-in, or out until 9 pm.
Continue still works whenever you press it; the window only shapes planning.

**Running a plan**: press **Start**; only the washer is commanded. When a step
finishes, `smartstart_laundry_step_done` fires and the **Continue** button
becomes available — one press per physical trip, firing *everything* the trip
unblocked (e.g. dryer *and* the next wash). Missing a changeover is fine:
downstream slots were estimates, and Planned Finish recomputes from actual
completions. The pair supports Scheduling Mode, a **Due By** deadline for the
plan end, and pair-level auto-reschedule/hold (the plan moves as one until
step 1 starts, then locks).

**Load order** (`Load Order` select, default *Optimised*): Start runs your
Loads in whichever order scores best on **cost + Value of an hour × hours until
everything's done** (pair option `time_value_per_hour`, default 5p/h; Mode still
picks each order's start). E.g. Towels' long wash goes in the evening so its dry
fills the morning while the short Darks wash pipelines behind it. A "Wash: X" row
plus the "Dry: Y" rows after it are one load and move together. Exhaustive up to
4 loads, local search beyond; the listed order is kept unless the winner is ≥1p
better. *As listed* keeps your order but still reports what optimising would gain.

**Away periods** (panel → Away; `smartstart/laundry_away`): no changeovers are
planned inside them — the step waits for your return, so a load can be "broken"
around lunch. Leave *Back* blank for "not back today" (until the next wake time).
Washed laundry may sit up to the wet-wait limit (only Mode Now exempts an
immediate start from that cap). Periods clear once they pass.

**Changeover After** is now just an away period ("not around until 09:00") —
the select still works for dashboards and writes into the Away list. On the panel,
**Done by** is a date-time picker.

**Smarter Continue** (auto-reschedule on, not Mode Now): at a changeover, what's
left is rescored — if loading now but delay-starting at a later 30-min slot saves
≥10p and still wins on cost + time (within each machine's delay limits, the
wet-wait cap and real rates), Continue loads now and starts then ("Loaded — starts
23:00"); **Start now instead** (`smartstart/laundry_start_now`) overrides, and
"Continue & start now" skips it. The auto-continue automation gets this too.
**Stalled steps**: a fired step not running 15 min after it should have started
fires `smartstart_laundry_step_stalled` (once) and shows on the panel.
**Before you leave**: 45 min before an away period, `smartstart_laundry_leaving`
carries the last changeover before you go and the next one after you're back.
Notifications for changeover, offer, leaving and stalled steps are built from
these events in your own automations.

**Suggestions**: when a load costs ≥10p and ≥25% more in this plan than on its
own after it (within 48 h), or when nothing fits your away times without dropping
one, the panel says so with a one-tap *Remove*.

**Rescheduling parity with appliances**: the reschedule envelope is now
`max_wait_hours` (pair option, default 36) from the Start press; **offers**
(`smartstart_laundry_offer_found`, Accept Offer button, `smartstart.accept_offer`
with the pair slug) on the same ≥10p/≥25% rules; and tapping a Recommended
window schedules (or moves) the plan there (`start_laundry` `start_at`).

Pair entities: Load 1–6, Scheduling Mode, Due By, Changeover After, Load Order, Start/Continue/Cancel/Accept Offer, and
sensors Status (full step plan in attributes), Planned Start/Finish/Cost/Rate,
Next Action, Current Step, Best Windows. The Planned
sensors update **live as you pick cycles** — the prospective plan is scored
on every selection, mode/deadline change and rate update, so you see when
the plan would start (and what it would cost) before pressing Start
(`preview: true` in the Status attributes marks uncommitted figures). Events:
`smartstart_laundry_scheduled` / `_step_done` / `_completed` / `_cancelled`.
While a run is live the appliances are *claimed* — their own Start buttons
refuse until the run finishes or is cancelled.

Exotic shapes (or automations) can bypass the row UI via the service:

```yaml
service: smartstart.start_laundry
data:
  steps:
    - {machine: wash, cycle_key: delicate, duration_minutes: 60}
    - {machine: wash, cycle_key: rinse_spin}
    - {machine: dry,  cycle_key: iron_eco}
    - {machine: wash, cycle_key: rinse_spin}
    - {machine: dry,  cycle_key: iron_eco}
  finish_by: "2026-07-12T08:00:00+01:00"
```

---

## Sidebar panel

A **SmartStart** entry appears in the HA sidebar (`/smartstart`), with one tab per device:

- **Status + KPIs** — what's scheduled or running now (with a live power curve while running),
  plus runs, cost, kWh and savings over the last 30 days and all time.
- **Recommended windows** — the 3 cheapest distinct windows for any cycle, drawn on a 48h rate
  chart (confirmed rates, then forecast slots with their low–high band).
- **Cycles** — per-cycle runs, average time, kWh and cost, total saved, curve confidence with the
  learned profile, and **rhythm**: the typical interval between runs, runs per week, usual start
  time, when it's next due, and the cheapest window inside that due period (once rates reach it).
- **When it runs** — heatmap of run starts by weekday × hour.
- **Runs** — every completed run. Click one to see its power curve against the cycle's learned
  curve and the tariff during the run.
- **Tagging** — Unknown (manually started) runs can be assigned to a cycle. The panel suggests
  the closest match by curve shape and duration. "Learn its curve" feeds the run into that
  cycle's learned profile and duration. Re-tagging or deleting a learned run removes it again
  (only admins can delete).

- **Laundry tab** (one per laundry pair) — what the pair is doing now, with a **Continue**
  button when a changeover is waiting; a wash/dry timeline of the plan (or the preview for
  the selected loads) with changeovers and the sleep window marked; recommended windows for
  the whole plan; **Flows**, showing each flow's configured repeat against how often you
  actually run it, with a one-click "Set to every N d" when the two drift apart (admin; reloads
  the pair); and **Loads**, completed wash → dry runs with the measured kWh, cost and saving
  per load (click one for each step's power curve).

Laundry steps carry `{run_id, pair, step, flow}` into their device run records, so a load's
wash and dry runs can be grouped. Completed loads are kept in the pair's own store (latest 200).
Loads from before this change can't be grouped.

Run history is stored per entry in `.storage/smartstart_run_history.<entry_id>` (500 runs per
device; the power trace is kept for the latest 200). On first load it is seeded from the
learned-curve timestamps so rhythm works straight away. Those seeded runs have no cost or trace.
"Saved" compares each run's cost against starting at the rate in effect when it was scheduled.

Websocket API (used by the panel): `smartstart/overview`, `smartstart/runs`, `smartstart/run`,
`smartstart/windows`, `smartstart/tag_run`, `smartstart/delete_run`,
`smartstart/laundry_continue`, `smartstart/laundry_set_repeat`, `smartstart/laundry_schedule`,
`smartstart/laundry_accept_offer`, `smartstart/laundry_away`.

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** → menu (⋮) → **Custom repositories**
3. Add `https://github.com/alexw23/smartstart` with category **Integration**
4. Search for **SmartStart** and click **Download**
5. Restart Home Assistant

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=alexw23&repository=smartstart&category=integration)

### Manual

1. Copy the `custom_components/smartstart` directory into your HA `custom_components/` folder
2. Restart Home Assistant

---

## Setup

[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=smartstart)

Or go to **Settings → Devices & Services → Add Integration** and search for **SmartStart**.

The setup wizard walks you through:

1. **Rate provider** — Octopus Energy (auto-detected if present) or Manual
2. **Currency / rate unit** — e.g. `£` / `p`
3. **Device** — appliance name, the HA service to call to start it, optional cancel service
4. **Cycle(s)** — duration and energy consumption of each programme

You can add multiple devices and multiple cycles per device via **Settings → Devices & Services → SmartStart → Configure**.

---

## Configuration reference

### Device fields

| Field | Description | Default |
|---|---|---|
| `name` | Display name | — |
| `icon` | MDI icon | `mdi:lightning-bolt` |
| `start_service` | HA service to start the appliance, e.g. `script.start_washing_machine` | — |
| `cancel_service` | HA service to cancel/stop the appliance (optional) | — |
| `delay_mode` | `native` — pass `delay_seconds` to the start service; `managed` — SmartStart owns the timer | `native` |
| `min_delay_minutes` | Earliest the appliance may start (minutes from now) | `0` |
| `max_delay_minutes` | The appliance's own delay reach (e.g. Home Connect: 24 h). Initial scheduling stays inside it; later targets past it are sent at the reach and re-sent once in range | `1440` |
| `max_wait_hours` | **Wait at most**: a run may be pushed back as far as finishing this long after you pressed Start, by auto-reschedule, offers or picked windows, never later. Fixed at the press, so reschedules can't creep. `0` = legacy (press + `max_delay_minutes`) | `36` |
| `auto_reschedule` | Automatically move a scheduled cycle to a cheaper window when new rates arrive (e.g. Octopus next-day rates landing at ~4 pm). The new window must fit the envelope of the original decision: finish within `max_wait_hours` of the Start press, or by the finish-by deadline if one was set. Fires `smartstart_cycle_rescheduled` on each move. | `off` |
| `hold_for_next_day_rates` | (Shown only when auto-reschedule is on.) Near the daily rate release (global "rates expected at" setting, default 16:00), a Start commits to a *placeholder* — the cheapest known window starting after release + 15 min — instead of gambling on today's data. The appliance gets its delayed-start command immediately, and when the rates land auto-reschedule re-optimises (possibly into tomorrow). Late/missing release needs no timeout: the placeholder was already the best call on known data and simply runs. Bypassed by Mode = Now and by Cycle Due By deadlines. | `off` |
| `offers` | Surface cheaper windows the schedule can't reach by itself as an offer (see [Offers](#offers)). | `on` |
| `running_entity` | Entity to watch to auto-detect running/idle state | — |
| `running_state` | State value that means the appliance is running | `on` |
| `stopped_state` | State value that means the appliance has stopped | `off` |

### Cycle fields

| Field | Description |
|---|---|
| `name` | Programme name (e.g. "Eco 40", "Quick 30") |
| `cycle_id` | Slug identifier — auto-generated from name if left blank |
| `duration_minutes` | How long the cycle takes |
| `total_kwh` | Estimated energy consumption |

---

## Rate providers

### Octopus Energy (auto-detected)

If the [Octopus Energy integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy) is installed, SmartStart will detect your `current_day_rates` and `next_day_rates` event entities automatically and pre-fill them in setup. Nothing is tied to a particular account: it reads whichever Octopus entities your Home Assistant has. If you have more than one meter, it prefers electricity import over export and ignores gas, and the setup form lets you pick any other pair. The same integration's Octoplus power-down and saving-session calendars are detected too (see [Power-down windows](#power-down-windows-octopus-octoplus-and-other-calendars)).

### Manual / any other provider

Use the `smartstart.publish_rates` service to push rate periods in from any source — a Node-RED flow, an automation, an AppDaemon script, or another custom component.

```yaml
service: smartstart.publish_rates
data:
  rates:
    - start: "2026-06-27T00:00:00Z"
      end: "2026-06-27T00:30:00Z"
      rate_cost_per_kwh: 0.07
    - start: "2026-06-27T00:30:00Z"
      end: "2026-06-27T01:00:00Z"
      rate_cost_per_kwh: 0.19
```

Call this service whenever new rate data is available — SmartStart immediately re-scores windows and updates all sensors.
Rates are plain numbers in your currency per kWh, so any tariff or signal works: other suppliers' time-of-use rates,
wholesale prices, carbon intensity scaled to a pseudo-price, or a curve you build from a solar forecast.

**Negative prices and solar.** Negative `rate_cost_per_kwh` values are fine and rank as the cheapest windows. To chase
surplus solar, publish a forecast-driven curve (for example, make slots with expected export-limited sunshine cheap or
negative, and the rest your normal import price) and SmartStart will schedule cycles into the sunny slots. This is a
technique built on the generic service, not a built-in solar feature.

---

## Buttons

Each device (and each cycle if a device has more than one) gets two buttons:

| Button | Behaviour |
|---|---|
| **Start Now** | Starts the appliance immediately, bypassing rate optimisation |
| **Start Anytime** | Schedules the appliance in the cheapest available window |

---

## Services

### `smartstart.start`

Schedule a device to run. The three modes are determined by optional parameters:

| Parameters | Behaviour |
|---|---|
| `device_slug` + `cycle_id` only | Cheapest window within the configured horizon |
| + `finish_by` | Cheapest window that finishes before the given datetime |
| + `now: true` | Start immediately (no rate optimisation) |

```yaml
# Cheapest window
service: smartstart.start
data:
  device_slug: washing_machine
  cycle_id: eco_40

# Must be done before 7 am
service: smartstart.start
data:
  device_slug: washing_machine
  cycle_id: eco_40
  finish_by: "2026-06-28T07:00:00+01:00"

# Start right now
service: smartstart.start
data:
  device_slug: washing_machine
  cycle_id: eco_40
  now: true
```

### `smartstart.cancel`

Cancel a scheduled run and call the device's `cancel_service` if one is configured.

```yaml
service: smartstart.cancel
data:
  device_slug: washing_machine
```

### `smartstart.publish_rates`

Push rate periods from any external source (see [Rate providers](#rate-providers) above).

### `smartstart.publish_predicted_rates`

Push *forecast* rate periods (same payload shape as `publish_rates`, plus
optional `rate_low_per_kwh` / `rate_high_per_kwh` for the forecast band).
Predictions are used in two places:

- **Holds** (device or laundry pair holding for tomorrow's rates): they extend
  the horizon past the real rates so the placeholder can be pre-positioned on a
  likely-cheap slot, priced at the band's *high* bound. Real rates always win —
  when they publish, the schedule is re-checked against the real cost of the
  committed slot and rescheduled within the usual envelope.
- **Best Windows** lists (display only): the central estimate, with the band
  shown per window, out to 48 h — forecasts beyond that can't see cheap spells.

Predictions never drive normal scoring or events, and are not persisted across
restarts.

Example feed from [agileforecast.co.uk](https://agileforecast.co.uk) (no API key;
region code matches your Octopus tariff region) via a scheduled automation:

```yaml
# rest_command:
#   agile_forecast:
#     url: "https://agileforecast.co.uk/api/H/?days=3"   # H = your region letter
# then an automation (hourly is fine) maps the forecast slots — agile_pred /
# agile_low / agile_high are p/kWh, divide by 100; skip is_published slots —
# into smartstart.publish_predicted_rates.
service: smartstart.publish_predicted_rates
data:
  rates:
    - start: "2026-06-28T03:00:00Z"
      end: "2026-06-28T03:30:00Z"
      rate_cost_per_kwh: -0.01
      rate_low_per_kwh: -0.05
      rate_high_per_kwh: 0.04
```

---

## Sensors

Each device exposes the following sensors:

### Schedule sensors (update when schedule changes)

| Sensor | Unit | Description |
|---|---|---|
| `{Device} Status` | — | `idle` · `scheduled` · `running` · `error` |
| `{Device} Scheduled Start` | timestamp | When the appliance will start |
| `{Device} Estimated Finish` | timestamp | When the cycle is expected to complete |
| `{Device} Scheduled Cycle` | — | The cycle ID that was scheduled |
| `{Device} Savings` | % | Rate saving vs starting right now |
| `{Device} Estimated Cost` | currency | Projected cost for the scheduled run |

### Rate sensors (update on coordinator refresh)

| Sensor | Unit | Description |
|---|---|---|
| `{Device} Current Rate` | rate/kWh | Electricity rate right now |
| `{Device} Best Window Start` | timestamp | Start of the cheapest upcoming window |
| `{Device} Best Window Rate` | rate/kWh | Average rate in the cheapest window |
| `{Device} Best Window Savings` | % | Saving of best window vs starting now |

The **Status** sensor also exposes all schedule fields as attributes, useful for template sensors or custom dashboards.

### Best Windows (planning list)

`{Device} Best Windows` (selected cycle) and the laundry pair's `Best Windows`
(current Load plan) list the 3 cheapest *distinct* windows (≥ 6 h apart) over
real rates plus published forecasts, out to 48 h. State = the best start;
`windows` attribute rows carry `start`, `end`, `rate`, `rate_low`/`rate_high`
(the forecast band; equal to `rate` for real prices), `cost` and `confirmed`
(true when the window is entirely on published rates). Nothing is booked.

### Picking a window

`smartstart.start` takes `start_at` (ISO time of a Best Windows row): the run goes
in that window (forecast-priced if it's on forecast slots), and its finish
becomes the run's limit. Auto-reschedule then only moves it somewhere cheaper
finishing no later. The sidebar panel's window rows and chart call this.

### Offers

While a cycle is scheduled, SmartStart keeps looking for a window it *can't*
take on its own: one on forecast slots (`publish_predicted_rates`, priced at the
central estimate; the band is in the attributes), or past the run's envelope (`max_delay_minutes` from the
Start press, or its deadline). With auto-reschedule on, windows it *can* reach
are simply taken, so they never become offers. With it off, those count too.

A window is offered when it saves at least 10p per cycle **and** is at least 25%
cheaper than the committed plan, and it finishes within the device's
**`max_wait_hours` (default 36 h) of the original Start press**.

- `{Device} Offer`: state = offered start (unknown when none). Attributes: `start`,
  `end`, `rate`, `rate_low`/`rate_high`, `cost`, `saving`, `saving_pct`,
  `predicted`, `planned_start`, `planned_rate`, `horizon_end`, and `message`
  (ready-made notification text).
- `{Device} Accept Offer` button: available only while there's an offer.
- `smartstart.accept_offer` (`device_slug`): the same, for automations and
  actionable notifications.
- `smartstart_offer_found` event: fires on a new offer, when a forecast offer
  becomes confirmed by real rates (`kind: confirmed`), or when the offered start
  moves ≥ 2 h. It does not fire on every forecast refresh.

**No endless creep.** Accepting is the only thing that widens a run's envelope:
its deadline becomes the offer's finish, and auto-reschedule keeps optimising
inside that. Offers are measured from the original press, not from now, so
however many you accept the run still finishes within `max_wait_hours` of when
you started it.

**Appliance reach.** A native-delay appliance can't take a delay longer than its
`max_delay_minutes` (e.g. Home Connect's 24 h). An accepted target further out
is sent at the appliance's furthest reach, then re-sent with the real delay once
it fits (`reschedule: true, recommand: true` in the payload; the target is
unchanged). If HA is down at that moment the appliance just runs at the interim
time.

---

## Example automation

Run the dishwasher overnight at the cheapest rate, but always done before 7 am:

```yaml
automation:
  - alias: "Dishwasher smart schedule"
    trigger:
      - platform: time
        at: "22:00:00"
    action:
      - service: smartstart.start
        data:
          device_slug: dishwasher
          cycle_id: normal
          finish_by: >
            {{ (now().replace(hour=7, minute=0, second=0, microsecond=0)
               + timedelta(days=1)).isoformat() }}
```

---

## Delay modes

| Mode | How it works | Use when |
|---|---|---|
| `native` | SmartStart calls `start_service` immediately with `delay_seconds` in the payload — the script/device handles the countdown | Your start script accepts a `delay_seconds` parameter (e.g. a washing machine with a built-in delay-start) |
| `managed` | SmartStart holds a timer and fires `start_service` at the optimal start time with `delay_seconds: 0` | Your script has no delay support; you want HA to manage the timing |

In `native` mode the service payload contains:

```json
{
  "delay_seconds": 3600,
  "cycle_id": "eco_40",
  "rate_cost_per_kwh": 0.07,
  "estimated_finish": "2026-06-28T03:30:00+00:00"
}
```

---

## Contributing

Pull requests are welcome. Please open an issue first for significant changes. SmartStart is beta software, so bug
reports with your tariff/provider, appliance type and the relevant Home Assistant log lines are especially useful.

## Licence

See [LICENSE](LICENSE).
