# Sensors

[← Back to the SmartStart README](../README.md)


Each device exposes the following sensors:

## Schedule sensors (update when schedule changes)

| Sensor | Unit | Description |
|---|---|---|
| `{Device} Status` | — | `idle` · `scheduled` · `running` · `error` |
| `{Device} Scheduled Start` | timestamp | When the appliance will start |
| `{Device} Estimated Finish` | timestamp | When the cycle is expected to complete |
| `{Device} Scheduled Cycle` | — | The cycle ID that was scheduled |
| `{Device} Savings` | % | Rate saving vs starting right now |
| `{Device} Estimated Cost` | currency | Projected cost for the scheduled run |

## Rate sensors (update on coordinator refresh)

| Sensor | Unit | Description |
|---|---|---|
| `{Device} Current Rate` | rate/kWh | Electricity rate right now |
| `{Device} Best Window Start` | timestamp | Start of the cheapest upcoming window |
| `{Device} Best Window Rate` | rate/kWh | Average rate in the cheapest window |
| `{Device} Best Window Savings` | % | Saving of best window vs starting now |

The **Status** sensor also exposes all schedule fields as attributes, useful for template sensors or custom dashboards.

## Best Windows (planning list)

`{Device} Best Windows` (selected cycle) and the laundry pair's `Best Windows`
(current Load plan) list the 3 cheapest *distinct* windows (≥ 6 h apart) over
real rates plus published forecasts, out to 48 h. State = the best start;
`windows` attribute rows carry `start`, `end`, `rate`, `rate_low`/`rate_high`
(the forecast band; equal to `rate` for real prices), `cost` and `confirmed`
(true when the window is entirely on published rates). Nothing is booked.

## Picking a window

`smartstart.start` takes `start_at` (ISO time of a Best Windows row): the run goes
in that window (forecast-priced if it's on forecast slots), and its finish
becomes the run's limit. Auto-reschedule then only moves it somewhere cheaper
finishing no later. The sidebar panel's window rows and chart call this.

## Offers

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
