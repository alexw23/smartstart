"""End-to-end tests for the sidebar panel's websocket API on a real hass."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartstart.const import DOMAIN, EXTERNAL_CYCLE_KEY


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


DEVICE = {
    "device_slug": "washer",
    "name": "Washer",
    "start_service": "script.start_washer",
    "stop_service": "",
    "running_entity": "",
    "running_states": ["on"],
    "power_sensor": "sensor.washer_power",
    "delay_handling": "start_delay",
    "min_delay_minutes": 0,
    "max_delay_minutes": 1440,
    "cycles": [
        {"cycle_key": "eco", "cycle_id": "eco", "name": "Eco 40",
         "duration_minutes": 60, "total_kwh": 1.0},
        {"cycle_key": "quick", "cycle_id": "quick", "name": "Quick",
         "duration_minutes": 30, "total_kwh": 0.5},
    ],
}


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"provider": "manual", "devices": [DEVICE]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rates = [
        {"start": (now + timedelta(minutes=30 * i)).isoformat(),
         "end": (now + timedelta(minutes=30 * (i + 1))).isoformat(),
         "rate_cost_per_kwh": 0.05 if 10 <= i < 14 else 0.25}
        for i in range(-2, 60)
    ]
    await hass.services.async_call(DOMAIN, "publish_rates", {"rates": rates}, blocking=True)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


async def _add_runs(store):
    history = store["run_history"]
    now = datetime.now(timezone.utc)
    for d in (15, 11, 7, 3):
        start = now - timedelta(days=d)
        await history.async_add_run({
            "device_slug": "washer", "cycle_key": "eco",
            "started_at": start.isoformat(),
            "ended_at": (start + timedelta(hours=1)).isoformat(),
            "duration_minutes": 60, "kwh": 1.0, "cost": 0.10, "baseline_cost": 0.25,
            "triggered_by": "smartstart",
        })
    start = now - timedelta(days=1)
    return await history.async_add_run({
        "device_slug": "washer", "cycle_key": EXTERNAL_CYCLE_KEY,
        "started_at": start.isoformat(),
        "ended_at": (start + timedelta(minutes=30)).isoformat(),
        "duration_minutes": 30, "kwh": 0.5, "cost": 0.12,
        "triggered_by": "external",
        "samples": [[i * 60, 1000.0] for i in range(31)],
        "rates": [[0, 0.24]],
    })


async def test_overview_runs_windows_and_tagging(hass, hass_ws_client):
    store = await _setup(hass)
    unknown = await _add_runs(store)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "smartstart/overview"})
    res = await client.receive_json()
    assert res["success"], res
    dev = res["result"]["devices"][0]
    assert dev["device_slug"] == "washer"
    assert dev["unknown_runs"] == 1
    assert res["result"]["rates"]["slots"]
    eco = next(c for c in dev["cycles"] if c["cycle_key"] == "eco")
    assert eco["runs"] == 4
    assert eco["avg_cost"] == pytest.approx(0.10)
    assert eco["total_saved"] == pytest.approx(0.60)
    assert eco["rhythm"]["interval_days"] == pytest.approx(4.0, abs=0.01)
    assert eco["rhythm"]["status"] == "due"  # last run 3 days ago, every 4
    assert eco["suggestion"]["start"]
    assert any(c["cycle_key"] == EXTERNAL_CYCLE_KEY for c in dev["cycles"])

    await client.send_json({"id": 2, "type": "smartstart/windows",
                            "device_slug": "washer", "cycle_key": "quick"})
    res = await client.receive_json()
    assert res["success"], res
    assert res["result"]["windows"][0]["rate"] == pytest.approx(0.05)

    await client.send_json({"id": 3, "type": "smartstart/runs", "device_slug": "washer"})
    res = await client.receive_json()
    assert res["result"]["total"] == 5
    assert res["result"]["runs"][0]["id"] == unknown["id"]  # newest first
    assert res["result"]["runs"][0]["has_curve"]

    flat = [{"t_s": i * 60.0, "power_w": 900.0} for i in range(31)]
    await store["curve_store"].async_add_curve("washer", "quick", flat, "seed-q")
    await store["curve_store"].async_add_curve("washer", "eco", [{"t_s": i * 60.0, "power_w": 900.0} for i in range(61)], "seed-e")
    await client.send_json({"id": 4, "type": "smartstart/run", "run_id": unknown["id"]})
    res = await client.receive_json()
    assert len(res["result"]["samples"]) == 31
    # Same flat shape for both; Quick wins on duration (30 vs 60 min configured)
    assert res["result"]["matches"][0]["cycle_key"] == "quick"
    assert res["result"]["matches"][0]["score"] > res["result"]["matches"][1]["score"]
    assert {c["cycle_key"] for c in res["result"]["cycle_options"]} == {"eco", "quick"}

    await client.send_json({"id": 5, "type": "smartstart/tag_run",
                            "run_id": unknown["id"], "cycle_key": "quick"})
    res = await client.receive_json()
    assert res["success"], res
    assert res["result"]["learned"] is True
    assert store["run_history"].get(unknown["id"])["cycle_key"] == "quick"
    assert store["curve_store"].get_confidence_info("washer", "quick")["n_curves"] == 2

    # Re-tag back to Unknown un-learns it
    await client.send_json({"id": 6, "type": "smartstart/tag_run",
                            "run_id": unknown["id"], "cycle_key": EXTERNAL_CYCLE_KEY})
    res = await client.receive_json()
    assert res["success"]
    assert store["curve_store"].get_confidence_info("washer", "quick")["n_curves"] == 1

    await client.send_json({"id": 7, "type": "smartstart/tag_run",
                            "run_id": unknown["id"], "cycle_key": "nope"})
    res = await client.receive_json()
    assert not res["success"]

    await client.send_json({"id": 8, "type": "smartstart/delete_run", "run_id": unknown["id"]})
    res = await client.receive_json()
    assert res["success"], res
    assert store["run_history"].get(unknown["id"]) is None


async def test_laundry_tab_loads_flows_and_repeat(hass, hass_ws_client):
    from custom_components.smartstart.laundry import all_laundry_managers

    dev = {**DEVICE}
    dryer = {**DEVICE, "device_slug": "dryer", "name": "Dryer",
             "power_sensor": "sensor.dryer_power",
             "cycles": [{"cycle_key": "cupboard", "cycle_id": "cupboard", "name": "Cupboard",
                         "duration_minutes": 90, "total_kwh": 2.0}]}
    for cfg in (dev, dryer):
        e = MockConfigEntry(domain=DOMAIN, data={"provider": "manual", "devices": [cfg]})
        e.add_to_hass(hass)
        assert await hass.config_entries.async_setup(e.entry_id)
    flow = {"slug": "towels", "name": "Towels", "repeat_days": 7, "steps": [
        {"machine": "wash", "cycle_key": "eco", "confirm_before": True},
        {"machine": "dry", "cycle_key": "cupboard", "confirm_before": True},
    ]}
    laundry = MockConfigEntry(domain=DOMAIN, data={
        "entry_type": "laundry", "name": "Laundry", "washer_slug": "washer",
        "dryer_slug": "dryer", "gap_minutes": 15, "flows": [flow],
    })
    laundry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(laundry.entry_id)
    await hass.async_block_till_done()

    mgr = all_laundry_managers(hass)[0]
    now = datetime.now(timezone.utc)
    stores = {s["coordinator"]._devices[0]["device_slug"]: s
              for s in hass.data[DOMAIN].values() if "coordinator" in s}
    # Four Towels loads, every 4 days; each has a measured wash + dry run.
    for k, d in enumerate((14, 10, 6, 2)):
        start = now - timedelta(days=d)
        lid = f"load{k}"
        mgr._loads.append({
            "id": lid, "started_at": start.isoformat(),
            "finished_at": (start + timedelta(hours=3)).isoformat(),
            "flows": ["towels"], "now_rate": 0.30, "plan_cost": 0.5,
            "steps": [{"machine": "wash", "name": "Eco 40"}, {"machine": "dry", "name": "Cupboard"}],
        })
        for slug, kwh, cost in (("washer", 1.0, 0.10), ("dryer", 2.0, 0.20)):
            await stores[slug]["run_history"].async_add_run({
                "device_slug": slug, "cycle_key": "eco" if slug == "washer" else "cupboard",
                "started_at": start.isoformat(), "ended_at": start.isoformat(),
                "kwh": kwh, "cost": cost, "laundry": {"run_id": lid, "step": 0},
            })

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "smartstart/overview"})
    res = await client.receive_json()
    assert res["success"], res
    pair = res["result"]["laundry"][0]
    assert pair["slug"] == mgr.slug
    assert len(pair["loads"]) == 4
    newest = pair["loads"][0]
    assert newest["id"] == "load3"
    assert newest["kwh"] == pytest.approx(3.0)
    assert newest["cost"] == pytest.approx(0.30)
    assert newest["saved"] == pytest.approx(0.60)  # 3 kWh × 0.30 − 0.30
    assert newest["flows"] == ["Towels"]
    towels = pair["flows"][0]
    assert towels["steps"] == [{"machine": "wash", "name": "Eco 40"},
                               {"machine": "dry", "name": "Cupboard"}]
    assert towels["rhythm"]["interval_days"] == pytest.approx(4.0)
    assert towels["suggest_repeat_days"] == 4  # set to 7, actually every 4
    assert pair["last_30d"]["loads"] == 4

    await client.send_json({"id": 2, "type": "smartstart/laundry_continue", "pair": mgr.slug})
    res = await client.receive_json()
    assert not res["success"] and res["error"]["code"] == "not_ready"

    await client.send_json({"id": 3, "type": "smartstart/laundry_set_repeat",
                            "pair": mgr.slug, "flow": "towels", "repeat_days": 4})
    res = await client.receive_json()
    assert res["success"], res
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(laundry.entry_id)
    assert entry.options["flows"][0]["repeat_days"] == 4


async def test_concurrent_setup_registers_panel_once(hass, caplog):
    """Entries set up in parallel must not race the static-path registration."""
    import asyncio
    from unittest.mock import patch
    from custom_components.smartstart import panel

    routes = []

    class FakeHttp:
        async def async_register_static_paths(self, configs):
            await asyncio.sleep(0)  # yields, like the real executor hop
            for c in configs:
                if c.url_path in routes:
                    raise RuntimeError("method GET is already registered")
                routes.append(c.url_path)

    hass.http = FakeHttp()
    with patch("homeassistant.components.frontend.async_register_built_in_panel") as reg:
        await asyncio.gather(*(panel.async_setup_panel(hass) for _ in range(4)))
    assert "sidebar panel registration failed" not in caplog.text
    assert routes == [panel.PANEL_JS_URL]
    assert reg.call_count == 1


async def test_laundry_away_and_controls(hass, hass_ws_client):
    from custom_components.smartstart.laundry import all_laundry_managers

    dryer = {**DEVICE, "device_slug": "dryer", "name": "Dryer",
             "power_sensor": "sensor.dryer_power",
             "cycles": [{"cycle_key": "cupboard", "cycle_id": "cupboard", "name": "Cupboard",
                         "duration_minutes": 90, "total_kwh": 2.0}]}
    for cfg in ({**DEVICE}, dryer):
        e = MockConfigEntry(domain=DOMAIN, data={"provider": "manual", "devices": [cfg]})
        e.add_to_hass(hass)
        assert await hass.config_entries.async_setup(e.entry_id)
    laundry = MockConfigEntry(domain=DOMAIN, data={
        "entry_type": "laundry", "name": "Laundry", "washer_slug": "washer",
        "dryer_slug": "dryer", "gap_minutes": 15, "flows": [],
    })
    laundry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(laundry.entry_id)
    await hass.async_block_till_done()
    mgr = all_laundry_managers(hass)[0]
    client = await hass_ws_client(hass)

    start = datetime.now(timezone.utc) + timedelta(hours=2)
    await client.send_json({"id": 1, "type": "smartstart/laundry_away", "pair": mgr.slug,
                            "action": "add", "start": start.isoformat(),
                            "end": (start + timedelta(hours=9)).isoformat()})
    assert (await client.receive_json())["success"]
    await client.send_json({"id": 2, "type": "smartstart/laundry_away", "pair": mgr.slug,
                            "action": "add", "start": start.isoformat(),
                            "end": (start - timedelta(hours=1)).isoformat()})
    res = await client.receive_json()
    assert not res["success"] and res["error"]["code"] == "failed"

    await client.send_json({"id": 3, "type": "smartstart/overview"})
    pair = (await client.receive_json())["result"]["laundry"][0]
    assert len(pair["snapshot"]["away"]) == 1
    assert len(pair["load_entities"]) == 6 and all(pair["load_entities"])
    assert pair["entities"]["order"] and pair["entities"]["due_by"]
    assert hass.states.get(pair["entities"]["order"]).state == "Optimised"

    await client.send_json({"id": 4, "type": "smartstart/laundry_away", "pair": mgr.slug,
                            "action": "remove", "index": 0})
    assert (await client.receive_json())["success"]
    assert mgr.away == []

    await client.send_json({"id": 5, "type": "smartstart/laundry_accept_offer", "pair": mgr.slug})
    res = await client.receive_json()
    assert not res["success"] and res["error"]["code"] == "failed"
    await client.send_json({"id": 6, "type": "smartstart/laundry_schedule", "pair": "nope",
                            "start_at": start.isoformat()})
    res = await client.receive_json()
    assert not res["success"] and res["error"]["code"] == "not_found"


async def test_laundry_sleep_and_overnight_settings(hass, hass_ws_client):
    from custom_components.smartstart.laundry import all_laundry_managers

    dryer = {**DEVICE, "device_slug": "dryer", "name": "Dryer", "power_sensor": "sensor.dryer_power",
             "cycles": [{"cycle_key": "cupboard", "cycle_id": "cupboard", "name": "Cupboard",
                         "duration_minutes": 90, "total_kwh": 2.0}]}
    for cfg in ({**DEVICE}, dryer):
        e = MockConfigEntry(domain=DOMAIN, data={"provider": "manual", "devices": [cfg]})
        e.add_to_hass(hass)
        assert await hass.config_entries.async_setup(e.entry_id)
    flow = {"slug": "linen", "name": "Linen", "steps": [
        {"machine": "wash", "cycle_key": "eco"}, {"machine": "dry", "cycle_key": "cupboard"}]}
    laundry = MockConfigEntry(domain=DOMAIN, data={
        "entry_type": "laundry", "name": "Laundry", "washer_slug": "washer",
        "dryer_slug": "dryer", "gap_minutes": 15, "flows": [flow],
    })
    laundry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(laundry.entry_id)
    await hass.async_block_till_done()
    mgr = all_laundry_managers(hass)[0]
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "smartstart/laundry_sleep", "pair": mgr.slug,
                            "bedtime": "23:00", "wake_time": "9:00"})
    assert (await client.receive_json())["success"]
    await client.send_json({"id": 2, "type": "smartstart/laundry_flow_overnight", "pair": mgr.slug,
                            "flow": "linen", "overnight": True})
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    opts = hass.config_entries.async_get_entry(laundry.entry_id).options
    assert opts["wake_time"] == "09:00" and opts["flows"][0]["overnight"] is True

    await client.send_json({"id": 3, "type": "smartstart/overview"})
    pair = (await client.receive_json())["result"]["laundry"][0]
    assert pair["wake_time"] == "09:00"
    assert pair["flows"][0]["overnight"] is True
