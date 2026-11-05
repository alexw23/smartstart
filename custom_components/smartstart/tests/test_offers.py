"""Offers: cheaper windows the schedule can't reach by itself, end to end."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.smartstart.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _device(**over):
    d = {
        "device_slug": "dishwasher",
        "name": "Dishwasher",
        "start_service": "script.start_dw",
        "stop_service": "",
        "running_entity": "",
        "running_states": ["on"],
        "power_sensor": "",
        "delay_handling": "start_delay",
        "min_delay_minutes": 0,
        "max_delay_minutes": 1440,
        "auto_reschedule": True,
        "cycles": [
            {"cycle_key": "eco", "cycle_id": "eco", "name": "Eco",
             "duration_minutes": 60, "total_kwh": 1.2},
        ],
    }
    d.update(over)
    return d


T0 = datetime.now(timezone.utc).replace(second=0, microsecond=0)
T0 = T0.replace(minute=0 if T0.minute < 30 else 30)


def _slots(first: int, last: int, rate_at, band=None):
    out = []
    for i in range(first, last):
        s = T0 + timedelta(minutes=30 * i)
        slot = {"start": s.isoformat(), "end": (s + timedelta(minutes=30)).isoformat(),
                "rate_cost_per_kwh": rate_at(i)}
        if band:
            slot["rate_low_per_kwh"], slot["rate_high_per_kwh"] = band(i)
        out.append(slot)
    return out


def _real(i):  # tonight's dip at +8h (14.9p), confirmed rates end at +10h
    return 0.149 if 16 <= i < 18 else 0.30


def _forecast(dip_at_slot):
    def rate(i):
        return 0.03 if dip_at_slot <= i < dip_at_slot + 2 else 0.30

    def band(i):
        return (0.02, 0.05) if dip_at_slot <= i < dip_at_slot + 2 else (0.25, 0.32)
    return rate, band


async def _setup(hass, device=None, dip_at_slot=50):
    calls = []

    async def _script(call):
        calls.append(dict(call.data))

    hass.services.async_register("script", "start_dw", _script)
    entry = MockConfigEntry(
        domain=DOMAIN, data={"provider": "manual", "devices": [device or _device()]}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        DOMAIN, "publish_rates", {"rates": _slots(-1, 20, _real)}, blocking=True
    )
    rate, band = _forecast(dip_at_slot)
    await hass.services.async_call(
        DOMAIN, "publish_predicted_rates", {"rates": _slots(20, 80, rate, band)},
        blocking=True,
    )
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id], calls


async def _start(hass):
    await hass.services.async_call(
        DOMAIN, "start", {"device_slug": "dishwasher", "cycle_id": "eco"}, blocking=True
    )
    await hass.async_block_till_done()


async def test_forecast_offer_beyond_reach_then_accept(hass):
    events = async_capture_events(hass, "smartstart_offer_found")
    store, calls = await _setup(hass, dip_at_slot=50)  # +25h: past the 24h reach
    await _start(hass)

    sched = store["scheduler"].get_schedule("dishwasher")
    assert sched["scheduled_start"] == (T0 + timedelta(hours=8)).isoformat()

    state = hass.states.get("sensor.dishwasher_offer")
    assert state.state == (T0 + timedelta(hours=25)).isoformat()
    assert state.attributes["predicted"] is True
    assert state.attributes["rate"] == 0.03          # central estimate…
    assert state.attributes["rate_high"] == 0.05     # …with the band alongside
    assert state.attributes["saving"] == pytest.approx((0.149 - 0.03) * 1.2, abs=1e-3)
    assert "instead of the planned" in state.attributes["message"]
    assert hass.states.get("button.dishwasher_accept_offer").state != "unavailable"
    assert len(events) == 1 and events[0].data["kind"] == "forecast"

    # Same offer on the next tick — not re-announced.
    store["coordinator"].rescore_now()
    await hass.async_block_till_done()
    assert len(events) == 1

    calls.clear()
    await hass.services.async_call(
        DOMAIN, "accept_offer", {"device_slug": "dishwasher"}, blocking=True
    )
    await hass.async_block_till_done()

    sched = store["scheduler"].get_schedule("dishwasher")
    target = T0 + timedelta(hours=25)
    assert sched["scheduled_start"] == target.isoformat()
    assert sched["reschedule_cap_kind"] == "deadline"
    assert sched["reschedule_cap"] == (target + timedelta(hours=1)).isoformat()
    assert sched["rate_predicted"] is True
    # Beyond the appliance's 24h reach: sent at the reach, re-sent later.
    assert calls[-1]["delay_seconds"] <= 24 * 3600
    assert calls[-1]["reschedule"] is True
    assert sched["recommand_at"] is not None
    assert hass.states.get("sensor.dishwasher_offer").state == "unknown"
    assert hass.states.get("button.dishwasher_accept_offer").state == "unavailable"


async def test_offer_bounded_from_the_start_press(hass):
    """A dip past press + 36h is never offered, however cheap."""
    store, _ = await _setup(hass, dip_at_slot=76)  # +38h
    await _start(hass)
    assert store["coordinator"].get_offer("dishwasher") is None


async def test_no_offer_for_what_auto_reschedule_can_reach(hass):
    events = async_capture_events(hass, "smartstart_offer_found")
    store, _ = await _setup(hass, dip_at_slot=200)  # no forecast dip
    await _start(hass)
    # A new confirmed dip inside the envelope — auto-reschedule just takes it.
    await hass.services.async_call(
        DOMAIN, "publish_rates",
        {"rates": _slots(-1, 20, lambda i: 0.05 if 10 <= i < 12 else _real(i))},
        blocking=True,
    )
    await hass.async_block_till_done()
    sched = store["scheduler"].get_schedule("dishwasher")
    assert sched["scheduled_start"] == (T0 + timedelta(hours=5)).isoformat()
    assert events == []


async def test_reachable_window_is_an_offer_when_auto_reschedule_off(hass):
    store, _ = await _setup(hass, device=_device(auto_reschedule=False), dip_at_slot=200)
    await _start(hass)
    await hass.services.async_call(
        DOMAIN, "publish_rates",
        {"rates": _slots(-1, 20, lambda i: 0.05 if 10 <= i < 12 else _real(i))},
        blocking=True,
    )
    await hass.async_block_till_done()
    offer = store["coordinator"].get_offer("dishwasher")
    assert offer["start"] == (T0 + timedelta(hours=5)).isoformat()
    assert offer["predicted"] is False


async def test_offers_toggle_off(hass):
    store, _ = await _setup(hass, device=_device(offers=False), dip_at_slot=50)
    await _start(hass)
    assert store["coordinator"].get_offer("dishwasher") is None


async def test_accept_without_offer_raises(hass):
    from homeassistant.exceptions import HomeAssistantError
    await _setup(hass, dip_at_slot=200)
    await _start(hass)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "accept_offer", {"device_slug": "dishwasher"}, blocking=True
        )


async def test_panel_overview_carries_offer_and_reach(hass, hass_ws_client):
    await _setup(hass, dip_at_slot=50)
    await _start(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "smartstart/overview"})
    dev = (await client.receive_json())["result"]["devices"][0]
    assert dev["offer"]["start"] == (T0 + timedelta(hours=25)).isoformat()
    # Wait limit 36h from the press, minus the 1h cycle → latest start ~35h out
    latest = datetime.fromisoformat(dev["reach"]["latest_start"])
    assert abs((latest - datetime.now(timezone.utc)).total_seconds() - 35 * 3600) < 60
    assert dev["auto_reschedule"] is True


async def test_accepted_forecast_falls_back_when_real_rates_disappoint(hass):
    store, _ = await _setup(hass, dip_at_slot=50)
    await _start(hass)
    await hass.services.async_call(
        DOMAIN, "accept_offer", {"device_slug": "dishwasher"}, blocking=True
    )
    await hass.async_block_till_done()
    # Tomorrow's real rates land: the forecast dip never happened.
    await hass.services.async_call(
        DOMAIN, "publish_rates", {"rates": _slots(-1, 60, _real)}, blocking=True
    )
    await hass.async_block_till_done()
    sched = store["scheduler"].get_schedule("dishwasher")
    assert sched["scheduled_start"] == (T0 + timedelta(hours=8)).isoformat()
    assert sched["rate_cost_per_kwh"] == pytest.approx(0.149)


async def test_announced_offer_not_repeated_after_restart(hass):
    events = async_capture_events(hass, "smartstart_offer_found")
    store, _ = await _setup(hass, dip_at_slot=50)
    await _start(hass)
    assert len(events) == 1
    coord = store["coordinator"]
    coord._offer_fired.clear()  # what a restart loses
    coord.rescore_now()
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_start_at_picked_forecast_window_beyond_reach(hass):
    """Picking a Best Windows row: runs there, caps the run at its finish,
    and sends the appliance's reach first if it's past 24h."""
    store, calls = await _setup(hass, dip_at_slot=60)  # forecast dip at +30h
    target = T0 + timedelta(hours=30)
    await hass.services.async_call(
        DOMAIN, "start",
        {"device_slug": "dishwasher", "cycle_id": "eco", "start_at": target.isoformat()},
        blocking=True,
    )
    await hass.async_block_till_done()
    sched = store["scheduler"].get_schedule("dishwasher")
    start = datetime.fromisoformat(sched["scheduled_start"])
    assert abs((start - target).total_seconds()) <= 60
    assert sched["reschedule_cap_kind"] == "deadline"
    assert sched["reschedule_cap"] == sched["estimated_finish"]
    assert sched["rate_predicted"] is True
    assert sched["recommand_at"] is not None
    assert calls[-1]["delay_seconds"] <= 24 * 3600


async def test_start_at_in_reach_sends_real_delay(hass):
    store, calls = await _setup(hass, dip_at_slot=200)
    target = T0 + timedelta(hours=3)
    await hass.services.async_call(
        DOMAIN, "start",
        {"device_slug": "dishwasher", "cycle_id": "eco", "start_at": target.isoformat()},
        blocking=True,
    )
    await hass.async_block_till_done()
    sched = store["scheduler"].get_schedule("dishwasher")
    assert sched["recommand_at"] is None
    assert sched["rate_predicted"] is False
    assert abs(calls[-1]["delay_seconds"] - (target - datetime.now(timezone.utc)).total_seconds()) < 90


async def test_panel_overview_exposes_control_entities(hass, hass_ws_client):
    await _setup(hass, dip_at_slot=200)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "smartstart/overview"})
    ents = (await client.receive_json())["result"]["devices"][0]["entities"]
    assert ents["start"].startswith("button.") and ents["stop"].startswith("button.")
    assert ents["mode"].startswith("select.")
    assert ents["cycle"] is None  # single-cycle devices have no cycle select


async def test_wait_limit_lets_auto_reschedule_past_24h(hass):
    """Tomorrow's real rates reveal a dip 30h after the press: inside the 36h
    wait limit, so auto-reschedule takes it (re-sent once in the 24h reach)."""
    events = async_capture_events(hass, "smartstart_offer_found")
    store, calls = await _setup(hass, dip_at_slot=200)
    await _start(hass)
    await hass.services.async_call(
        DOMAIN, "publish_rates",
        {"rates": _slots(-1, 66, lambda i: 0.03 if 60 <= i < 62 else _real(i))},
        blocking=True,
    )
    await hass.async_block_till_done()
    sched = store["scheduler"].get_schedule("dishwasher")
    assert sched["scheduled_start"] == (T0 + timedelta(hours=30)).isoformat()
    assert sched["recommand_at"] is not None
    assert calls[-1]["delay_seconds"] <= 24 * 3600
    assert events == []  # reachable → just moved, no offer
