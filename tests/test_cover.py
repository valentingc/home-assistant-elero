"""Test the Elero cover entity: commands, status handling and position logic."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.cover import DOMAIN as COVER_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.entity_component import async_update_entity
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.elero.const import (
    INFO_BLOCKING,
    INFO_BOTTOM_POS_STOP_WICH_INT_POS,
    INFO_BOTTOM_POSITION_STOP,
    INFO_INTERMEDIATE_POSITION_STOP,
    INFO_MOVING_DOWN,
    INFO_NO_INFORMATION,
    INFO_OVERHEATED,
    INFO_STOPPED_IN_UNDEFINED_POSITION,
    INFO_TILT_VENTILATION_POS_STOP,
    INFO_TOP_POS_STOP_WICH_TILT_POS,
    INFO_TOP_POSITION_STOP,
)

from .conftest import cover_subentry, get_entity, make_entry, setup_entry

ENTITY_ID = "cover.living_room"
TRAVEL_TIME = 30.0
TILT_TRAVEL_TIME = 2.0


class FakeClock:
    """Stand-in for the `time` module as seen by cover.py."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    fake = FakeClock()
    with patch("custom_components.elero.cover.time", SimpleNamespace(time=fake.time)):
        yield fake


@pytest.fixture
def scheduled(hass: HomeAssistant, monkeypatch):
    """Capture callbacks the cover schedules with loop.call_later."""
    calls: list[tuple[float, object]] = []

    class Handle:
        def cancel(self) -> None:
            pass

    def call_later(delay, callback, *args):
        calls.append((delay, lambda: callback(*args)))
        return Handle()

    monkeypatch.setattr(hass.loop, "call_later", call_later)
    return calls


@pytest.fixture
async def cover(hass: HomeAssistant, init_integration, clock):
    return get_entity(hass, ENTITY_ID)


@pytest.fixture
def respond(mock_transmitter):
    """Feed a device status into the cover, like the transmitter would."""

    def _respond(status: str) -> None:
        handler = mock_transmitter.set_channel.call_args.args[1]
        handler({"status": status})

    return _respond


async def _call(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        COVER_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY_ID, **data}, blocking=True
    )


# ── commands ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("service", "command"),
    [
        ("open_cover", "up"),
        ("close_cover", "down"),
        ("stop_cover", "stop"),
        ("open_cover_tilt", "intermediate"),
        ("close_cover_tilt", "ventilation_tilting"),
        ("stop_cover_tilt", "stop"),
    ],
)
async def test_service_sends_command(
    hass: HomeAssistant, cover, scheduled, mock_transmitter, service, command
) -> None:
    await _call(hass, service)
    getattr(mock_transmitter, command).assert_called_once_with(1)


async def test_unavailable_when_channel_not_learned(
    hass: HomeAssistant, mock_transmitter
) -> None:
    mock_transmitter.set_channel.return_value = False
    await setup_entry(hass, make_entry())
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE


# ── device status → state ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "position", "tilt", "closed"),
    [
        (INFO_TOP_POSITION_STOP, 100, 100, False),
        (INFO_BOTTOM_POSITION_STOP, 0, 0, True),
        (INFO_INTERMEDIATE_POSITION_STOP, 75, 75, False),
        (INFO_BOTTOM_POS_STOP_WICH_INT_POS, 75, 75, False),
        (INFO_TILT_VENTILATION_POS_STOP, 25, 25, False),
        (INFO_TOP_POS_STOP_WICH_TILT_POS, 25, 25, False),
        (INFO_NO_INFORMATION, None, None, None),
        (INFO_BLOCKING, None, None, None),
        (INFO_OVERHEATED, None, None, None),
    ],
)
async def test_status_sets_state(
    cover, respond, status, position, tilt, closed
) -> None:
    respond(INFO_TOP_POSITION_STOP if position is None else INFO_BOTTOM_POSITION_STOP)
    respond(status)
    assert cover.current_cover_position == position
    assert cover.current_cover_tilt_position == tilt
    assert cover.is_closed is closed
    assert cover.extra_state_attributes["elero_state"] == status


async def test_state_written_after_poll(
    hass: HomeAssistant, cover, respond, mock_transmitter
) -> None:
    """A status that arrives in response to a poll ends up in the HA state."""
    mock_transmitter.info.side_effect = lambda ch: respond(INFO_BOTTOM_POSITION_STOP)
    await async_update_entity(hass, ENTITY_ID)
    state = hass.states.get(ENTITY_ID)
    assert state.state == "closed"
    assert state.attributes["current_position"] == 0


async def test_restores_last_state(hass: HomeAssistant, mock_transmitter) -> None:
    mock_restore_cache(
        hass,
        [
            State(
                ENTITY_ID,
                "open",
                {"current_position": 60, "current_tilt_position": 30},
            )
        ],
    )
    await setup_entry(hass, make_entry())
    entity = get_entity(hass, ENTITY_ID)
    assert entity.current_cover_position == 60
    assert entity.current_cover_tilt_position == 30
    assert entity.is_closed is False


# ── position tracking ───────────────────────────────────────────────────


async def test_position_interpolates_while_moving(
    cover, respond, clock, scheduled
) -> None:
    respond(INFO_TOP_POSITION_STOP)
    cover.close_cover()
    assert cover.is_closing

    clock.advance(TRAVEL_TIME / 2)
    assert cover.current_cover_position == 50

    clock.advance(TRAVEL_TIME)  # overshoot is clamped
    assert cover.current_cover_position == 0


async def test_stop_keeps_interpolated_position(
    cover, respond, clock, scheduled, mock_transmitter
) -> None:
    respond(INFO_BOTTOM_POSITION_STOP)
    cover.open_cover()
    clock.advance(TRAVEL_TIME * 0.4)
    cover.stop_cover()

    mock_transmitter.stop.assert_called_once_with(1)
    assert cover.current_cover_position == 40
    assert not cover.is_opening
    assert cover.is_closed is False


async def test_movement_status_starts_tracking(cover, respond, clock) -> None:
    """Movement started from a physical remote is picked up from the status."""
    respond(INFO_TOP_POSITION_STOP)
    respond(INFO_MOVING_DOWN)
    assert cover.is_closing
    clock.advance(TRAVEL_TIME / 4)
    assert cover.current_cover_position == 75


async def test_open_polls_status_after_travel_time(
    hass: HomeAssistant, cover, scheduled, respond, mock_transmitter
) -> None:
    """After a full run the drive is polled off the event loop and state written."""
    cover.open_cover()
    ((delay, poll),) = scheduled
    assert delay == TRAVEL_TIME + 1

    mock_transmitter.info.reset_mock()
    mock_transmitter.info.side_effect = lambda ch: respond(INFO_TOP_POSITION_STOP)
    poll()
    await hass.async_block_till_done()

    mock_transmitter.info.assert_called_once_with(1)
    assert hass.states.get(ENTITY_ID).attributes["current_position"] == 100


async def test_services_schedule_from_executor(
    hass: HomeAssistant, cover, respond, mock_transmitter
) -> None:
    """Commands run in the executor; their timers must still land on the loop.

    asyncio debug mode (on in tests) raises if loop.call_later is used from
    a worker thread.
    """
    respond(INFO_TOP_POSITION_STOP)
    await _call(hass, "close_cover")
    await _call(hass, "stop_cover")
    await _call(hass, "set_cover_position", position=40)
    await _call(hass, "set_cover_tilt_position", tilt_position=50)
    await _call(hass, "open_cover")


async def test_unload_cancels_pending_timers(
    hass: HomeAssistant, init_integration, mock_transmitter
) -> None:
    """Timers scheduled for a move don't outlive the entity."""
    await _call(hass, "close_cover")  # schedules the post-run status poll
    await _call(hass, "set_cover_position", position=40)  # schedules a stop
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    # The hass fixture fails the test on any lingering timer.


async def test_set_position_runs_timed_move(
    hass: HomeAssistant, cover, respond, clock, scheduled, mock_transmitter
) -> None:
    respond(INFO_TOP_POSITION_STOP)
    cover.set_cover_position(position=40)

    mock_transmitter.down.assert_called_once_with(1)
    ((delay, finish),) = scheduled
    assert delay == pytest.approx(TRAVEL_TIME * 0.6)

    clock.advance(delay)
    finish()
    await hass.async_block_till_done()

    mock_transmitter.stop.assert_called_once_with(1)
    assert cover.current_cover_position == 40
    assert not cover.is_closing


async def test_set_position_ignores_tiny_moves(
    cover, respond, scheduled, mock_transmitter
) -> None:
    respond(INFO_TOP_POSITION_STOP)
    cover.set_cover_position(position=99)
    mock_transmitter.down.assert_not_called()
    assert scheduled == []


async def test_set_position_calibrates_when_unknown(
    hass: HomeAssistant, cover, respond, scheduled, mock_transmitter
) -> None:
    """With no known position the cover opens fully first, then retries."""
    assert cover.current_cover_position is None
    cover.set_cover_position(position=40)

    mock_transmitter.up.assert_called_once_with(1)
    retry = dict(scheduled)[TRAVEL_TIME + 2]

    # The retry runs the move in the executor once the position is known.
    respond(INFO_TOP_POSITION_STOP)
    retry()
    await hass.async_block_till_done()
    mock_transmitter.down.assert_called_once_with(1)


async def test_long_move_sets_slats_to_extreme(
    cover, respond, clock, scheduled
) -> None:
    respond(INFO_INTERMEDIATE_POSITION_STOP)
    cover.close_cover()
    clock.advance(TILT_TRAVEL_TIME + 1)
    cover.stop_cover()
    assert cover.current_cover_tilt_position == 0


# ── tilt step (close tilt on reprogrammed remotes) ──────────────────────


async def test_tilt_step_nudges_position(
    cover, respond, clock, mock_transmitter
) -> None:
    respond(INFO_BOTTOM_POSITION_STOP)
    cover.close_cover_tilt()

    mock_transmitter.ventilation_tilting.assert_called_once_with(1)
    assert cover.current_cover_position == 2
    assert cover.is_closed is False


async def test_tilt_step_ignores_stale_status_during_lock(
    cover, respond, clock
) -> None:
    """The drive's own tilt/move reports must not undo the tilt step."""
    respond(INFO_BOTTOM_POSITION_STOP)
    cover.close_cover_tilt()

    for status in (
        INFO_MOVING_DOWN,
        INFO_TILT_VENTILATION_POS_STOP,
        INFO_TOP_POS_STOP_WICH_TILT_POS,
    ):
        respond(status)
        assert cover.current_cover_position == 2
        assert not cover.is_closing

    clock.advance(11)
    respond(INFO_TILT_VENTILATION_POS_STOP)
    assert cover.current_cover_position == 25


async def test_tilt_step_noop_when_fully_open(cover, respond) -> None:
    respond(INFO_TOP_POSITION_STOP)
    cover.close_cover_tilt()
    assert cover.current_cover_position == 100


async def test_tilt_step_disabled(hass: HomeAssistant, mock_transmitter, clock) -> None:
    await setup_entry(hass, make_entry([cover_subentry(tilt_step=0)]))
    entity = get_entity(hass, ENTITY_ID)
    mock_transmitter.set_channel.call_args.args[1]({"status": INFO_BOTTOM_POSITION_STOP})

    entity.close_cover_tilt()
    mock_transmitter.ventilation_tilting.assert_called_once_with(1)
    assert entity.current_cover_position == 0


# ── timed tilt slider ───────────────────────────────────────────────────


async def test_set_tilt_runs_timed_pulse(
    hass: HomeAssistant, cover, respond, clock, scheduled, mock_transmitter
) -> None:
    respond(INFO_BOTTOM_POSITION_STOP)
    cover.set_cover_tilt_position(tilt_position=50)

    mock_transmitter.up.assert_called_once_with(1)
    ((delay, finish),) = scheduled
    assert delay == pytest.approx(TILT_TRAVEL_TIME * 0.5)

    clock.advance(delay)
    finish()
    await hass.async_block_till_done()

    mock_transmitter.stop.assert_called_once_with(1)
    assert cover.current_cover_tilt_position == 50


async def test_set_tilt_survives_stale_status(
    hass: HomeAssistant, cover, respond, clock, scheduled
) -> None:
    """Moving/undefined-stop reports right after a tilt pulse keep the target."""
    respond(INFO_BOTTOM_POSITION_STOP)
    cover.set_cover_tilt_position(tilt_position=50)
    ((delay, finish),) = scheduled
    clock.advance(delay)
    finish()
    await hass.async_block_till_done()

    respond(INFO_STOPPED_IN_UNDEFINED_POSITION)
    assert cover.current_cover_tilt_position == 50


async def test_set_tilt_unknown_assumes_from_position(
    cover, respond, scheduled, mock_transmitter
) -> None:
    """Unknown tilt is assumed open on a mostly-open cover."""
    respond(INFO_TOP_POSITION_STOP)
    cover._tilt_position = None
    cover.set_cover_tilt_position(tilt_position=0)
    mock_transmitter.down.assert_called_once_with(1)
    ((delay, _),) = scheduled
    assert delay == pytest.approx(TILT_TRAVEL_TIME)


@pytest.mark.parametrize(
    ("tilt", "command"), [(20, "ventilation_tilting"), (80, "intermediate")]
)
async def test_set_tilt_without_travel_time(
    hass: HomeAssistant, mock_transmitter, tilt, command
) -> None:
    """With tilt travel time 0 the slider falls back to the two Elero presets."""
    await setup_entry(hass, make_entry([cover_subentry(tilt_travel_time=0)]))
    get_entity(hass, ENTITY_ID).set_cover_tilt_position(tilt_position=tilt)
    getattr(mock_transmitter, command).assert_called_once_with(1)
    mock_transmitter.up.assert_not_called()
    mock_transmitter.down.assert_not_called()
