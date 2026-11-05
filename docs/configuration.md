# Configuration reference

[← Back to the SmartStart README](../README.md)


## Device fields

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
| `offers` | Surface cheaper windows the schedule can't reach by itself as an offer (see [Offers](sensors.md#offers)). | `on` |
| `running_entity` | Entity to watch to auto-detect running/idle state | — |
| `running_state` | State value that means the appliance is running | `on` |
| `stopped_state` | State value that means the appliance has stopped | `off` |

## Cycle fields

| Field | Description |
|---|---|
| `name` | Programme name (e.g. "Eco 40", "Quick 30") |
| `cycle_id` | Slug identifier — auto-generated from name if left blank |
| `duration_minutes` | How long the cycle takes |
| `total_kwh` | Estimated energy consumption |
