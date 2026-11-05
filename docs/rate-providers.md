# Rate providers

[← Back to the SmartStart README](../README.md)


## Octopus Energy (auto-detected)

If the [Octopus Energy integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy) is installed, SmartStart will detect your `current_day_rates` and `next_day_rates` event entities automatically and pre-fill them in setup. Nothing is tied to a particular account: it reads whichever Octopus entities your Home Assistant has. If you have more than one meter, it prefers electricity import over export and ignores gas, and the setup form lets you pick any other pair. The same integration's Octoplus power-down and saving-session calendars are detected too (see [Power-down windows](power-down.md)).

## Manual / any other provider

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
