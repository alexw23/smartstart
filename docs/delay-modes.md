# Delay modes

[← Back to the SmartStart README](../README.md)


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
