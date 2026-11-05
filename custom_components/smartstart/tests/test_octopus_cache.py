"""OctopusEntityProvider falls back to its persisted cache after a restart."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.smartstart.providers.octopus_entity import OctopusEntityProvider


def _state(slots):
    st = MagicMock()
    st.attributes = {"rates": slots} if slots is not None else {"event_type": "x"}
    return st


@pytest.mark.asyncio
async def test_cache_bridges_restart_gap():
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    slots = [
        {"start": (now + timedelta(minutes=30 * i)).isoformat(),
         "end": (now + timedelta(minutes=30 * (i + 1))).isoformat(),
         "value_inc_vat": 0.1 + i / 100}
        for i in range(-4, 20)
    ]
    saved = {}
    store = MagicMock()
    store.async_save = AsyncMock(side_effect=lambda d: saved.update(d))
    store.async_load = AsyncMock(side_effect=lambda: dict(saved))
    hass = MagicMock()
    with patch("custom_components.smartstart.providers.octopus_entity.Store", return_value=store):
        p = OctopusEntityProvider(hass, "event.cur", "event.next")
        hass.states.get = MagicMock(side_effect=lambda eid: _state(slots if eid == "event.cur" else []))
        assert len(await p.async_get_rates()) == 24
        assert store.async_save.await_count == 1
        await p.async_get_rates()
        assert store.async_save.await_count == 1  # unchanged → not re-saved

        # "Restart": new provider, entities restored without rates.
        p2 = OctopusEntityProvider(hass, "event.cur", "event.next")
        hass.states.get = MagicMock(side_effect=lambda eid: _state(None))
        rates = await p2.async_get_rates()
        assert rates and all(r.end > datetime.now(timezone.utc) for r in rates)
        assert len(rates) < 24  # past slots dropped
