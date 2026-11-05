# Buttons, services and automations

[← Back to the SmartStart README](../README.md)

## Buttons

Each device (and each cycle if a device has more than one) gets two buttons:

| Button | Behaviour |
|---|---|
| **Start Now** | Starts the appliance immediately, bypassing rate optimisation |
| **Start Anytime** | Schedules the appliance in the cheapest available window |

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

Push rate periods from any external source (see [Rate providers](rate-providers.md)).

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
