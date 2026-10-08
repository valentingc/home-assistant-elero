"""Test Elero covers inside a Home Assistant cover group."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest
from homeassistant.components.cover import DOMAIN as COVER_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.elero.const import (
    INFO_BOTTOM_POSITION_STOP,
    INFO_MOVING_DOWN,
    INFO_TOP_POSITION_STOP,
)

from .conftest import cover_subentry, get_entity, make_entry, setup_entry
from .test_cover import tick

GROUP = "cover.all_covers"
MEMBERS = ["cover.living_room", "cover.kitchen", "cover.bedroom"]
# Each command keeps the stick busy this long (radio round trip).
COMMAND_TIME = 0.8


@pytest.fixture
async def group(hass: HomeAssistant, mock_transmitter, respond, freezer):
    """Three venetian blinds on one stick, in a cover group, all at the top."""
    # The group platform is YAML-configured, so the cover component must be
    # set up with it before the Elero entry sets up the cover domain.
    assert await async_setup_component(
        hass,
        COVER_DOMAIN,
        {
            COVER_DOMAIN: [
                {"platform": "group", "name": "All covers", "entities": MEMBERS}
            ]
        },
    )
    entry = make_entry(
        [
            cover_subentry("Living room", 1),
            cover_subentry("Kitchen", 2),
            cover_subentry("Bedroom", 3),
        ]
    )
    await setup_entry(hass, entry)
    for channel in (1, 2, 3):
        await respond(INFO_TOP_POSITION_STOP, channel)

    # Commands block the stick like the real radio round trip does. The drive
    # reacts when the command goes out; record when that really happened.
    sent: list[tuple[str, int, float]] = []

    def radio(command):
        def _send(channel, *_args):
            sent.append((command, channel, time.monotonic()))
            freezer.tick(timedelta(seconds=COMMAND_TIME))

        return _send

    for command in ("up", "down", "stop", "intermediate", "ventilation_tilting"):
        getattr(mock_transmitter, command).side_effect = radio(command)
    mock_transmitter.reset_mock()
    return sent


def real_position(sent, channel: int, now: float | None = None) -> float:
    """Where a drive that started at the top really is, from radio times."""
    start = next(t for cmd, ch, t in sent if ch == channel and cmd == "down")
    stops = [t for cmd, ch, t in sent if ch == channel and cmd == "stop"]
    end = stops[0] if stops else now
    run = end - start
    return max(0.0, 100 - max(0.0, run - 2) / 48 * 100)


async def call_group(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        COVER_DOMAIN, service, {ATTR_ENTITY_ID: GROUP, **data}, blocking=True
    )


def position(hass: HomeAssistant, entity_id: str) -> int:
    return hass.states.get(entity_id).attributes["current_position"]


async def test_group_reflects_members(hass: HomeAssistant, group) -> None:
    assert hass.states.get(GROUP).state == "open"
    assert position(hass, GROUP) == 100


async def test_group_close_moves_all_members(
    hass: HomeAssistant, group, respond, freezer, mock_transmitter
) -> None:
    await call_group(hass, "close_cover")
    assert sorted(c.args[0] for c in mock_transmitter.down.call_args_list) == [1, 2, 3]
    assert hass.states.get(GROUP).state == "closing"
    for member in MEMBERS:
        assert hass.states.get(member).state == "closing"

    await tick(hass, freezer, 2 + 24)
    # The group follows the members while they move.
    assert position(hass, GROUP) == pytest.approx(
        sum(position(hass, m) for m in MEMBERS) / 3, abs=1
    )

    for channel in (1, 2, 3):
        await respond(INFO_BOTTOM_POSITION_STOP, channel)
    assert hass.states.get(GROUP).state == "closed"
    assert position(hass, GROUP) == 0


async def test_group_set_position_lands_every_member_on_target(
    hass: HomeAssistant, group, freezer, mock_transmitter
) -> None:
    """The stick sends one command at a time, so members start one after
    another. Each member's timed stop must be measured from its own start."""
    await call_group(hass, "set_cover_position", position=40)
    await tick(hass, freezer, 40)

    assert mock_transmitter.stop.call_count == 3
    for channel, member in zip((1, 2, 3), MEMBERS, strict=True):
        real = real_position(group, channel)
        assert real == pytest.approx(40, abs=1.5), member
        model = get_entity(hass, member)._current().position
        assert model == pytest.approx(real, abs=0.5), member
    assert position(hass, GROUP) == pytest.approx(40, abs=1)


async def test_group_stop_uses_actual_stop_time(
    hass: HomeAssistant, group, freezer, mock_transmitter
) -> None:
    """A member whose STOP goes out later has also travelled further."""
    await call_group(hass, "close_cover")
    await tick(hass, freezer, 20)
    await call_group(hass, "stop_cover")
    await hass.async_block_till_done()

    for channel, member in zip((1, 2, 3), MEMBERS, strict=True):
        real = real_position(group, channel)
        model = get_entity(hass, member)._current().position
        assert model == pytest.approx(real, abs=0.5), member
        assert not get_entity(hass, member).is_closing


async def test_group_tilt_sends_preset_to_all(
    hass: HomeAssistant, group, mock_transmitter
) -> None:
    await call_group(hass, "close_cover_tilt")
    assert sorted(
        c.args[0] for c in mock_transmitter.ventilation_tilting.call_args_list
    ) == [1, 2, 3]


async def test_group_stop_after_staggered_starts(
    hass: HomeAssistant, group, freezer, mock_transmitter
) -> None:
    """Members started at different times each stop where they really are."""
    await hass.services.async_call(
        COVER_DOMAIN, "close_cover", {ATTR_ENTITY_ID: MEMBERS[0]}, blocking=True
    )
    await tick(hass, freezer, 5)
    await hass.services.async_call(
        COVER_DOMAIN, "close_cover", {ATTR_ENTITY_ID: MEMBERS[1:]}, blocking=True
    )
    await tick(hass, freezer, 10)
    await call_group(hass, "stop_cover")

    for channel, member in zip((1, 2, 3), MEMBERS, strict=True):
        real = real_position(group, channel)
        model = get_entity(hass, member)._current().position
        assert model == pytest.approx(real, abs=0.5), member
    assert position(hass, MEMBERS[0]) < position(hass, MEMBERS[1])


async def test_remote_group_move_is_picked_up_on_all_members(
    hass: HomeAssistant, group, respond, mock_transmitter
) -> None:
    """When one cover notices a move from a physical remote, the others on the
    stick are polled right away instead of at their next idle poll."""
    await respond(INFO_MOVING_DOWN, 1)
    hub = get_entity(hass, MEMBERS[0])._hub
    # The queue worker is a background task (not awaited by
    # async_block_till_done); yield until it has drained the queue. The clock
    # is frozen, so only sleep(0) makes progress here.
    for _ in range(100):
        await asyncio.sleep(0)
        await hass.async_block_till_done()
        if hub._queue.empty() and mock_transmitter.info.call_count >= 2:
            break
    polled = {c.args[0] for c in mock_transmitter.info.call_args_list}
    assert {2, 3} <= polled
