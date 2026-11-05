# Laundry pair (wash → dry chaining)

[← Back to the SmartStart README](../README.md)


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
