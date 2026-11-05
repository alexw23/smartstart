# Power-down windows (Octopus Octoplus and other calendars)

[← Back to the SmartStart README](../README.md)


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
