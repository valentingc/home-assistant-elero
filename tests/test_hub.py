"""Test the per-stick command queue."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.elero.hub import POLL_ATTEMPTS, EleroHub
from custom_components.elero.transmitter import EleroTransmitter

from .conftest import make_entry


@pytest.fixture
async def hub(hass: HomeAssistant):
    """A started hub whose transmitter records calls and can be paused."""
    tx = MagicMock(spec=EleroTransmitter)
    tx.get_serial_number.return_value = "SERIAL"
    calls: list[tuple] = []
    gate = threading.Event()
    gate.set()

    def record(name):
        def _call(*args):
            gate.wait(5)
            calls.append((name, *args))

        return _call

    for name in ("up", "down", "stop", "intermediate", "ventilation_tilting", "info", "check"):
        getattr(tx, name).side_effect = record(name)

    entry = make_entry([])
    entry.add_to_hass(hass)
    hub = EleroHub(hass, tx)
    hub.async_start(entry)
    hub.calls = calls
    hub.gate = gate
    yield hub
    gate.set()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_stop_jumps_the_queue(hass: HomeAssistant, hub) -> None:
    """While the stick is busy, STOP is sent before commands, polls go last."""
    hub.gate.clear()
    hub.async_request_poll(1)  # picked up immediately, blocks on the gate
    await asyncio.sleep(0.05)

    hub.async_request_poll(2)
    up = hass.async_create_task(hub.async_command(3, "up"))
    stop = hass.async_create_task(hub.async_command(4, "stop"))
    await asyncio.sleep(0)
    hub.gate.set()
    await asyncio.gather(up, stop)
    await hass.async_block_till_done()

    assert hub.calls == [
        ("info", 1, POLL_ATTEMPTS),
        ("stop", 4),
        ("up", 3),
        ("info", 2, POLL_ATTEMPTS),
    ]


async def test_duplicate_polls_are_merged(hass: HomeAssistant, hub) -> None:
    hub.gate.clear()
    hub.async_request_poll(9)  # in flight
    await asyncio.sleep(0.05)
    for _ in range(5):
        hub.async_request_poll(1)
    hub.gate.set()
    await hass.async_block_till_done()
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()

    assert hub.calls.count(("info", 1, POLL_ATTEMPTS)) == 1


async def test_command_errors_reach_the_caller(hass: HomeAssistant, hub) -> None:
    hub.transmitter.down.side_effect = OSError("gone")
    with pytest.raises(OSError):
        await hub.async_command(1, "down")
    # The worker keeps running.
    await hub.async_command(1, "up")
    assert ("up", 1) in hub.calls


async def test_unknown_command_is_rejected(hub) -> None:
    with pytest.raises(ValueError):
        await hub.async_command(1, "explode")


async def test_status_is_delivered_on_the_event_loop(
    hass: HomeAssistant, hub
) -> None:
    hub.transmitter.set_channel.return_value = True
    received: list[tuple[str, bool]] = []

    def listener(status: str) -> None:
        received.append((status, threading.get_ident() == hass.loop_thread_id))

    assert hub.async_register(1, listener)
    handler = hub.transmitter.set_channel.call_args.args[1]
    await hass.async_add_executor_job(handler, {"status": "top position stop"})
    await hass.async_block_till_done()

    assert received == [("top position stop", True)]
