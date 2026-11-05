# SmartStart — TODO

Captured during design conversations. Not all are committed; each entry is a
proposal until prioritised.

## Offers — v1 core SHIPPED 2026-10-23

Scheduled run + a much cheaper window it can't reach alone (forecast, or past
its envelope) → `{Device} Offer` sensor, Accept Offer button,
`smartstart.accept_offer`, `smartstart_offer_found`. Decisions (user,
2026-10-23): a per-run offer rather than a new mode (modes are sticky, and the
dishwasher auto-start requires Max Savings); ≥10p and ≥25% cheaper, priced on
the forecast's central estimate (user chose this over the high bound 2026-10-23:
agileforecast bands are wide, so the high bound buried the motivating case;
real-rate fallback after acceptance limits the downside); 36 h from the *original press* (bounds repeated
accepts); accept = the only envelope-widening path; appliance reach
handled by sending the interim delay and re-sending later. `better_window_found` is kept
(not deprecated).

Shipped the same day: panel (`/smartstart?device=<slug>` deep link, offer card
with Accept, shading for how far the schedule can still move + "out of reach" labels, "re-checks
when rates land" line); `packages/dishwasher.yaml` (offer folded into the
auto-start Sonos line; `dishwasher_offer_found` push with Wait/Keep, body
opens the panel, Sonos only in DAY/EVENING; `dishwasher_offer_accept`);
`start_dishwasher` push says the new time and is silent on `recommand`;
announced-offer marker persisted so restarts don't re-announce.

Also 2026-10-23: **wait limit** `max_wait_hours` (default 36, per device)
replaces press + max_delay as the auto-reschedule envelope (user: "should be able
to schedule now + 24h"; chose a fixed 36h from the press over rolling now+24h so it
can't creep). max_delay is now only the appliance's reach (re-send handles the
rest). Offers use the same horizon. **Panel controls** (user asked, reversing
"display-only"): cycle/mode selects + Start/Stop via the device's own entities,
laundry Mode/Start/Cancel (loads still on the dashboard), tap a Recommended
window → `smartstart.start start_at` ("Schedule" / "Move here"), scheduled run
drawn on the chart.

**Laundry parity SHIPPED 2026-10-23**: wait limit, offers, picked windows for
pairs; plus the load-order optimiser (user chose auto-on-Start with an "As listed"
escape, and a cost+time blend over either alone), away periods (user chose a
from–until list; laundry may sit up to the wet-wait cap rather than delay-start),
money-gated "wash another day" suggestions, panel load pickers + per-load pill
colours/labels. Wet-cap "now" exemption narrowed to Mode Now (was any mode).

Next steps:
- (Done 2026-10-23 eve: laundry notifications, Done-by picker, Changeover After
  folded into away periods, smarter Continue with delay-start + Start now,
  stalled-step events.)
- Edit flows in the panel (user didn't pick it yet).
- Rates cache across restarts (providers/octopus_entity.py) shipped 2026-10-23 —
  verify after the next restart that previews appear immediately.
- Optimiser runs on the event loop (~70 ms for 3 loads, ~400 ms worst at 6);
  move to an executor if it ever shows up in HA's slow-callback warnings.
- Initial scheduling still scores within max_delay; consider scoring within the
  wait limit too (rarely matters: real rates end ~23:00 tomorrow).
- Live-verify the full loop: 4pm confirmation (`kind: confirmed`) and the
  re-send of the same target once it's within the 24h reach, on a real >24h accept.

## Sidebar panel — v1 SHIPPED 2026-10-23

WashData-style sidebar panel (panel.py + frontend/smartstart-panel.js, vanilla
web component). It needs run_history.py (per-run records with a 60 s power trace and
the rate segments) and rhythm.py (median interval and due date). Unknown runs can be
tagged, with a curve-match suggestion, and tagging can learn the curve. See the README.
Laundry tab added the same day. Decisions: display-only plus Continue (Start/Cancel and
load pickers stay on the dashboard card); loads are grouped through the step `laundry` meta;
flow rhythm drift is shown with a one-click interval update, never applied automatically.

Follow-ups:
- Backfill older runs from recorder history of the power sensor and running entity
  (only the curve timestamps are seeded today).
- "Schedule this" button on a recommended / rhythm-suggested window (calls start
  with finish_by), so the panel can act as well as advise.
- Rhythm-aware default Due By (ties into the parked default-due-by idea).
- Auto-tag Unknown runs when the best match is very confident (≥0.85?), with the panel
  showing what got auto-tagged.

## Laundry pair (wash → dry chaining) — v1 SHIPPED 2026-07-11

Decided 2026-07-11: purpose-built for laundry, **not** a generic workflow
engine. Washer→dryer is effectively the only common cross-device chain, so
specialising buys a laundry-native model and UX. Non-laundry chains, if ever
demanded, get a service-based escape hatch (start with an explicit step
list), not UI.

Build decisions 2026-07-11: own config entry (new "Laundry" setup type);
flows defined via config-flow step builder; auto_reschedule + hold reused at
pair level (window can move until step 1 starts, then locks); scope = core +
flows, card view in a follow-up.

**Shipped**: laundry_plan.py (pure model: offsets, composite profiles,
unblocked-step rules), laundry.py (LaundryManager: cross-entry device
resolution, concurrent step state machine, Continue-fires-unblocked,
persistence, pair-level reschedule + hold), scorer constraint/scoring
duration split, config flow (pair setup + flow step builder), entities
(Load 1–3 row selects with flows-in-dropdowns, Mode, Due By,
Start/Continue/Cancel, Status/Planned */Next Action/Current Step sensors),
`smartstart.start_laundry`, device claiming, `smartstart_laundry_*` events.
Live plan preview (2026-07-11 fix): selections/mode/due-by/rate updates
rescore the prospective plan while idle, so Planned Start/Finish/Cost/Rate
show before Start is pressed (`preview: true` in Status attributes).
56 new tests (141 total).

**Remaining (follow-ups)**:

- `smartstart-card.js` laundry view — per-load / per-step timeline from the
  Status sensor's `steps` attribute; Continue button inline.
- Rescore-at-Continue polish: "dry now at X — as planned / rates moved".
- Stalled-step surfacing: step fired but device never reported running
  (currently just sits in Next Action as running; could fire an event after
  N minutes).
- Per-flow-step learned curves (curve key = flow + step index) — see Load
  size below.

### Core model — flat ordered step list, one decision variable

- Config: pick the **washer device** and **dryer device** (existing
  SmartStart devices) + changeover gap (default 15 min). Registers a virtual
  "Laundry" HA device.
- A run is an ordered list of steps, each `(machine: wash|dry, cycle,
  confirm_before: bool, duration/energy override?)`. Two implicit rules
  derive the whole schedule — no user-facing dependency wiring:
  1. Same-machine steps queue (one washer, one dryer).
  2. A dry step waits for every wash step listed before it.
  Parallelism is implicit: `[W, D, W, D]` pipelines load 2's wash during
  load 1's dry because the washer is free and rule 2 doesn't block it.
- Scored as one composite virtual cycle: offsets from t₀ per the rules above,
  each step's curve buckets scaled by its kWh share, zeros in gaps,
  overlapping steps **sum** their fractions. `_profile_weighted_rate` already
  takes arbitrary profiles → still a 1-D search over t₀.
- Scorer change required: split "constraint duration" from "scoring duration"
  in `score_windows`. Candidate constraints (`delay_step_minutes`,
  `anchor="end"`) belong to the **first wash** only; scoring spans the full
  plan; Due By applies to the last step's end.
- Only the first step is machine-commanded at schedule time. Every later
  start is human-confirmed: **Continue = "physical trip done"** and fires
  *everything unblocked at that moment* (e.g. one press starts the dryer AND
  the next wash). Steps with `confirm_before: false` auto-fire (re-run
  chains, e.g. 2× dry of the same contents — no trip needed).
- Missing a changeover is inherently fine — downstream slots were only ever
  estimates. Polish: on Continue, rescore "next step now vs planned" and
  surface if rates moved. Estimated finish recomputes from actual
  completions.
- State machine must support **concurrent active steps from day one** (a set,
  not a current-step pointer) — the flagship Linen Sheets flow overlaps
  washer and dryer, so concurrency can't be deferred. Phase by UI instead:
  load rows first, flows right behind.

### Runtime UI — plan rows (manual pairs and flows compose)

- Up to 3 rows build the run's plan. Each row is either:
  - **manual pair** — Wash Cycle (or None) + Dry Cycle (or None), or
  - **a saved flow** — flows appear as dropdown entries (`Flow: Linen
    Sheets`) in the Wash Cycle select (or the Dry select, if the flow's first
    step is a dry — e.g. a `2× Iron Eco` split-dry chain, composable with a
    manual wash in the same row). A flow entry brings its own steps; the
    row's other select is ignored/greyed.
- Rows concatenate their step lists in row order; the two queueing rules do
  the rest. E.g. row 1 = `Flow: Linen Sheets`, row 2 = All in One 59 +
  Cupboard Eco: the standard wash slots into the washer the moment the bottom
  sheet's Rinse&Spin frees it (while sheets still dry), and its dry queues
  behind both sheet dries — no explicit wiring.
- None-semantics cover the space: Wash+None = line-dry load; None+Dry =
  second half of a split or a refresh run; None+None = row unused.
- Row 1 restores its last-used selection; rows 2–3 reset to None after a
  completed run — the everyday single wash+dry stays zero-config, Linen
  Sheets day is one dropdown pick.

### Flows — config-defined named step lists (the power path)

- Defined once in the pair's config: name + ordered steps (machine, cycle,
  confirm_before, optional duration/energy override). At runtime they are
  **plan items**, not whole-run plans — composable with manual rows and other
  flows.
- Example — "Linen Sheets": Delicate 800 (both sheets) → Rinse&Spin (top) →
  Iron Eco dry (top) ∥ Rinse&Spin (bottom) → Iron Eco dry (bottom). The
  overlap and the bottom sheet waiting for the dryer fall out of the two
  rules; per-step overrides encode that sheet cycles run short (~1 h, not the
  cycle's nominal 2 h 30).
- Boundary: flows are **flat lists only** — no branching, conditions, or
  per-run parameters. That's the line that keeps this from becoming a
  workflow engine.
- Flows subsume dropdown "custom chains" (a 2× dry is just a two-step flow) —
  two concepts total: manual rows and flows, one composition mechanism.
- Setup could seed an example flow or two from the user's cycles ("defaulted
  flows"), so the dropdown isn't empty on day one.
- Future: per-flow-step learned curves (curve key = flow + step index).
  "Linen Sheets step 1" is far more repeatable than "Delicate, any load", so
  flows learn better than plain cycles and partly solve load-size attribution
  where it hurts most.

### Load size (future)

Durations/energy are per-cycle constants, so a half load claims a full load's
window and corrupts slot choice. Staged fix: flow-step overrides now (user
knows their flow); later a Load Size select (S/M/L scale factors) for ad-hoc
load rows; per-flow-step curves make it self-learning for flows.

### Entities (on the virtual Laundry device)

- Selects: Row 1–3 Wash Cycle + Dry Cycle (flow entries appear inside these
  dropdowns — no separate Flow select), Scheduling Mode, Due By.
- Buttons: Start, Continue (`available` only while a trip is awaited), Cancel
  (running cycle just finishes as a normal cycle).
- Sensors: Status (idle / scheduled / running / awaiting changeover /
  complete), Planned Start, Planned Finish (live), Planned Cost + Planned
  Rate (composite), Next Action (human text — notification automations can
  send its state verbatim; "waiting for dryer" states need no Continue).
- Status attributes carry the full step plan (per-step machine, cycle,
  planned start/end, rate) so `smartstart-card.js` can render a per-load /
  per-step timeline.
- Device claiming: an appliance's own Start button refuses (or warns) while
  claimed by an active laundry run; its other entities behave normally and
  curves keep learning.

### Events

`smartstart_laundry_scheduled` / `_step_done` (changeover prompt, carries
next-action text) / `_completed` / `_cancelled`.

### Structure

- Entries are per-appliance, but the pair spans two entries → the laundry
  manager must be a hub-level singleton in `hass.data` resolving devices
  across entries; the pair gets its own config entry (or subentry).
- Run state persisted like the scheduler (active steps, markers, committed
  start) — survives restart.
- Escape hatch: `smartstart.start_laundry` service accepting an explicit step
  list, for automations and exotic one-offs.

### Explicitly out of scope

- Generic DAG workflows, branching, or conditional steps.
- Slack between steps (deliberately waiting for a cheaper dry slot — wet
  clothes sitting in the drum). Fixed gaps keep the 1-D search valid.
- Co-scheduling *independent* appliances under a joint household power cap —
  a different problem (joint constraint, independent starts); don't stretch
  the laundry model to absorb it.

## Predictive rates — v1 SHIPPED (predictive hold)

`smartstart.publish_predicted_rates` (user-supplied feed, e.g.
agileforecast.co.uk — keeps forecast maintenance out of scope for us).
Predictions extend the horizon only while a hold is active: the placeholder
can be pre-positioned on a likely-cheap slot tomorrow; when real rates land,
the reschedule compares candidates against the *real* cost of the committed
slot (predicted baseline discarded) and moves within the usual envelope.
Cheap-window events never fire on predicted slots.

Laundry pairs got the same treatment 2026-10-22 (they originally copied only
the hold clamp, not the forecast horizon): held plans score on real+forecast
slots, a predicted commit is flagged `rate_predicted`, and once real rates
cover the plan its baseline (and displayed Planned Rate/Cost) is replaced by
the real profile-weighted cost before any reschedule comparison.

Shipped 2026-10-22 (v1.5): forecast bands (`rate_low/high_per_kwh`; holds use
the high bound) and **Best Windows** planning lists per device + laundry pair
(3 distinct windows, real + forecast, 48 h cap — agileforecast's own accuracy
page shows 0% of cheap spells caught beyond 48 h, so a longer list adds
nothing). Laundry rows merged into one `Load N` select (6 rows) and flows
gained **repeat every N days** → auto-fill + due-date deadline.

### v2 ideas (not built)

- Use predictions *outside* holds for *scheduling*: EV(now) vs EV(best
  predicted window) so a 10 am Max Savings start can rationally wait for a
  predicted cheap overnight slot. Best Windows now surfaces this for the user;
  acting on it automatically still needs confidence weighting.
- Tap a Best Windows row to pin the plan to it (sets Due By / start).
- Silently correct the displayed Planned Rate on a predicted commit once real
  rates cover the window and no reschedule was needed (comparison already
  uses the real baseline; only the display lags).
- Auto-derive the Octopus region for a built-in fetcher — deliberately
  avoided for now so we don't own a third-party API dependency.
- Ties into solar forecasting the same way: any predictor just publishes
  forecast slots.

## Future scheduling modes

Once the Now / Balanced / Max Savings framework is in place, additional
modes are essentially one-screen-fillers:

- **Negative Only** — pick soonest window with `avg_rate < 0`. Fall through
  to Max Savings if no negative slot in horizon. Useful for grid-export
  enthusiasts.
- **Solar Match** — align cycle with peak predicted solar generation.
  Requires a solar forecast input entity (Forecast.Solar / Solcast).
- **Quiet Hours** — pick cheapest window OUTSIDE user-defined noisy hours
  (e.g. don't run the dryer 22:00–07:00).

## Whole-hour reservation polish

Currently SmartStart picks minute-precise windows and lets the user's script
round to whole hours where required. Works fine. Could be tighter:

- Sub-mode where SmartStart pre-aligns the chosen window's end time to a
  whole hour from now. Removes script-side rounding entirely.
- Already partly done via `delay_granularity = "whole_hours"`. The remaining
  polish would be: when delay_handling = `end_time` AND granularity =
  `whole_hours`, the candidate generation should anchor to end (currently
  shipping). Verify with real captures from LG dryers.

## Cycle auto-discovery

When the user picks a `select.*` / enum sensor as the running_entity, scan
its `options` attribute and offer to pre-create cycles:

1. Detect — running_entity is select/enum.
2. Surface "✨ Auto-discover cycles" action in the device menu.
3. Multi-select shows entity options with checkboxes.
4. On submit, create cycles with placeholder defaults:
   - `name` = option label
   - `cycle_key` = slug of label
   - `cycle_id` = exact option string (sent to script)
   - `duration_minutes` = 60 (placeholder)
   - `total_kwh` = 1.0 (placeholder)
5. Optionally walk user through each new cycle's edit form to set real
   durations.

### Future polish

- Brand-aware default-duration lookup (LG Eco Cotton+ = 150 min, etc.).
- Auto-extract durations from the appliance's own `remaining_time` / cycle
  history if available.

## Reschedule v2 — auto-reschedule ✅ SHIPPED

Per-device `auto_reschedule` config toggle (default off). When enabled and a
cheaper window appears (any improvement beyond a 0.1p/kWh anti-churn epsilon),
the schedule is moved automatically and `smartstart_cycle_rescheduled` fires.
The search is bounded by the envelope committed at schedule time
(`reschedule_cap` in the schedule dict): finish-by deadline if set, else
schedule-time + max_delay — so repeated reschedules can't creep later.
No separate `reschedule_service` was needed — the existing `start_service`
is re-fired with `reschedule: true` in the payload (managed devices just get
their timer moved). With the toggle off, the notify-only
`smartstart_better_window_found` event behaves as before.

## Per-cycle Cycle Due By default

Per-run deadline (`Cycle Due By`) is great, but some cycles have a natural
"done by" time (Linen sheets needed by 9 AM). Per-cycle default:

- Cycle config gets optional `default_finish_by` (24h clock, optional).
- Start Anytime: if cycle has default_finish_by → compute the next occurrence
  → use as deadline.
- Per-run helper still overrides per cycle.

## Misc cleanup

- Legacy `delay_mode` constants in `const.py` can be removed once we're
  confident no third-party tools still reference them.
- The 5-minute too-short cycle filter is a magic number — consider making
  per-cycle configurable.
- `prefer_sooner_diff` and `cheap_window_threshold` could be reframed as
  pence-per-kWh for the form (we'd convert internally).
