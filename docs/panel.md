# Sidebar panel

[← Back to the SmartStart README](../README.md)


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
