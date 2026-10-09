"""Test the Elero cover entity: commands, status handling and position logic.

The default test cover is a venetian blind: 52 s up, 50 s down, 2 s slat
swing (so 50 s / 48 s of vertical travel), ventilation = drive up 1 s,
intermediate = fixed 75 %.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from homeassistant.components.cover import DOMAIN as COVER_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.elero.const import (
    INFO_BLOCKING,
    INFO_BOTTOM_POSITION_STOP,
    INFO_INTERMEDIATE_POSITION_STOP,
    INFO_MOVING_DOWN,
    INFO_MOVING_UP,
    INFO_STOPPED_IN_UNDEFINED_POSITION,
    INFO_TILT_VENTILATION_POS_STOP,
    INFO_TOP_POS_STOP_WICH_TILT_POS,
    INFO_TOP_POSITION_STOP,
)
from custom_components.elero.cover import END_STOP_GRACE

from .conftest import (
    cover_subentry,
    get_entity,
    make_entry,
    setup_entry,
    subentry_from_data,
)

ENTITY_ID = "cover.living_room"


async def tick(hass: HomeAssistant, freezer, seconds: float) -> None:
    """Let `seconds` pass, firing due timers along the way (≤1 s steps)."""
    remaining = seconds
    while remaining > 1e-9:
        step = min(1.0, remaining)
        freezer.tick(timedelta(seconds=step))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        remaining -= step


async def call(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        COVER_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY_ID, **data}, blocking=True
    )


def attrs(hass: HomeAssistant) -> dict:
    return hass.states.get(ENTITY_ID).attributes


def model(hass: HomeAssistant):
    """The precise (unrounded) estimate."""
    return get_entity(hass, ENTITY_ID)._current()


@pytest.fixture
async def cover(hass: HomeAssistant, init_integration, mock_transmitter):
    mock_transmitter.reset_mock()
    return get_entity(hass, ENTITY_ID)


@pytest.fixture
async def at_bottom(cover, respond, mock_transmitter):
    await respond(INFO_BOTTOM_POSITION_STOP)
    mock_transmitter.reset_mock()


@pytest.fixture
async def at_top(cover, respond, mock_transmitter):
    await respond(INFO_TOP_POSITION_STOP)
    mock_transmitter.reset_mock()


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
    hass: HomeAssistant, cover, mock_transmitter, service, command
) -> None:
    await call(hass, service)
    getattr(mock_transmitter, command).assert_called_once_with(1)


@pytest.mark.parametrize(
    ("button", "command"),
    [("ventilation", "ventilation_tilting"), ("intermediate", "intermediate")],
)
async def test_preset_buttons(
    hass: HomeAssistant, cover, mock_transmitter, button, command
) -> None:
    await hass.services.async_call(
        "button",
        "press",
        {ATTR_ENTITY_ID: f"button.living_room_{button}"},
        blocking=True,
    )
    getattr(mock_transmitter, command).assert_called_once_with(1)


async def test_unavailable_when_channel_not_learned(
    hass: HomeAssistant, mock_transmitter
) -> None:
    mock_transmitter.set_channel.return_value = False
    await setup_entry(hass, make_entry())
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE


async def test_polls_while_idle(
    hass: HomeAssistant, cover, freezer, mock_transmitter
) -> None:
    await tick(hass, freezer, 30)
    assert mock_transmitter.info.call_count == 1
    await tick(hass, freezer, 30)
    assert mock_transmitter.info.call_count == 2


# ── end stops ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "position", "tilt", "state"),
    [
        (INFO_TOP_POSITION_STOP, 100, 100, "open"),
        (INFO_BOTTOM_POSITION_STOP, 0, 0, "closed"),
    ],
)
async def test_end_stops(
    hass: HomeAssistant, cover, respond, status, position, tilt, state
) -> None:
    await respond(status)
    assert hass.states.get(ENTITY_ID).state == state
    assert attrs(hass)["current_position"] == position
    assert attrs(hass)["current_tilt_position"] == tilt
    assert attrs(hass)["elero_state"] == status


async def test_fault_keeps_position(
    hass: HomeAssistant, at_top, respond, freezer
) -> None:
    await call(hass, "close_cover")
    await tick(hass, freezer, 2 + 12)
    await respond(INFO_BLOCKING)
    assert not get_entity(hass, ENTITY_ID).is_closing
    assert attrs(hass)["current_position"] == 75


# ── the ventilation step (the "25 %" problem) ───────────────────────────


async def test_ventilation_step_from_bottom_opens_slats(
    hass: HomeAssistant, at_bottom, freezer, mock_transmitter
) -> None:
    """Close tilt = drive up 1 s: slats turn, the blind stays down."""
    await call(hass, "close_cover_tilt")
    mock_transmitter.ventilation_tilting.assert_called_once_with(1)
    assert get_entity(hass, ENTITY_ID).is_opening

    await tick(hass, freezer, 1)
    assert not get_entity(hass, ENTITY_ID).is_opening
    assert attrs(hass)["current_position"] == 0
    assert attrs(hass)["current_tilt_position"] == 50  # 1 s of a 2 s swing
    # Down, but the slats let light through: not closed.
    assert hass.states.get(ENTITY_ID).state == "open"
    mock_transmitter.up.assert_not_called()
    mock_transmitter.stop.assert_not_called()  # the drive stops itself


async def test_ventilation_stop_reports_do_not_jump_to_25(
    hass: HomeAssistant, at_bottom, respond, freezer
) -> None:
    """Polls keep reporting the ventilation stop; the estimate must hold."""
    await call(hass, "close_cover_tilt")
    await tick(hass, freezer, 1)
    for _ in range(4):
        await respond(INFO_TILT_VENTILATION_POS_STOP)
        await respond(INFO_TOP_POS_STOP_WICH_TILT_POS)
        await tick(hass, freezer, 30)
    assert attrs(hass)["current_position"] == 0
    assert attrs(hass)["current_tilt_position"] == 50
    assert hass.states.get(ENTITY_ID).state == "open"


async def test_ventilation_step_from_mid_position(
    hass: HomeAssistant, at_top, respond, freezer
) -> None:
    """The step is relative: wherever the cover is, it only nudges up."""
    await call(hass, "set_cover_position", position=40)
    await tick(hass, freezer, 35)
    assert attrs(hass)["current_tilt_position"] == 0
    position = model(hass).position

    await call(hass, "close_cover_tilt")
    await tick(hass, freezer, 1)
    await respond(INFO_TILT_VENTILATION_POS_STOP)
    assert model(hass).position == pytest.approx(position)
    assert attrs(hass)["current_tilt_position"] == 50


async def test_stale_movement_after_step_is_ignored(
    hass: HomeAssistant, at_bottom, respond, freezer
) -> None:
    """A poll catching the drive mid-step must not start an endless move."""
    await call(hass, "close_cover_tilt")
    await tick(hass, freezer, 1)
    await respond(INFO_MOVING_UP)
    assert not get_entity(hass, ENTITY_ID).is_opening
    assert attrs(hass)["current_position"] == 0


async def test_fixed_ventilation_position(
    hass: HomeAssistant, mock_transmitter, respond
) -> None:
    """Drives programmed the classic way snap to their configured position."""
    entry = make_entry(
        [cover_subentry(ventilation={"mode": "fixed", "position": 20, "duration": 1})]
    )
    await setup_entry(hass, entry)
    await respond(INFO_TILT_VENTILATION_POS_STOP)
    assert attrs(hass)["current_position"] == 20


async def test_users_existing_config_uses_step(
    hass: HomeAssistant, mock_transmitter, respond, freezer
) -> None:
    """A pre-4.3 sub-entry (tilt step 2 %, 50 s) becomes a 1 s step up."""
    legacy = {
        "name": "Living room",
        "channel": 1,
        "device_class": "venetian blind",
        "supported_features": ["up", "down", "stop", "close_tilt"],
        "travel_time": 50.0,
        "tilt_step": 2.0,
        "tilt_travel_time": 1.0,
    }
    await setup_entry(hass, make_entry([subentry_from_data(legacy)]))
    await respond(INFO_BOTTOM_POSITION_STOP)
    await call(hass, "close_cover_tilt")
    await tick(hass, freezer, 1)
    await respond(INFO_TILT_VENTILATION_POS_STOP)
    await tick(hass, freezer, 30)
    await respond(INFO_TILT_VENTILATION_POS_STOP)
    assert attrs(hass)["current_position"] == 0
    assert attrs(hass)["current_tilt_position"] == 100


# ── intermediate (fixed preset) ─────────────────────────────────────────


async def test_intermediate_moves_to_fixed_position(
    hass: HomeAssistant, at_top, respond, freezer, mock_transmitter
) -> None:
    await call(hass, "open_cover_tilt")
    mock_transmitter.intermediate.assert_called_once_with(1)
    assert get_entity(hass, ENTITY_ID).is_closing

    await tick(hass, freezer, 2 + 0.25 * 48)
    assert attrs(hass)["current_position"] == 75
    assert attrs(hass)["current_tilt_position"] == 0
    await respond(INFO_INTERMEDIATE_POSITION_STOP)
    assert attrs(hass)["current_position"] == 75


async def test_intermediate_status_sets_unknown_position(
    hass: HomeAssistant, cover, respond
) -> None:
    await respond(INFO_INTERMEDIATE_POSITION_STOP)
    assert attrs(hass)["current_position"] == 75


# ── full runs ───────────────────────────────────────────────────────────


async def test_position_interpolates_and_ui_updates(
    hass: HomeAssistant, at_top, freezer
) -> None:
    await call(hass, "close_cover")
    await tick(hass, freezer, 2 + 24)  # slats, then half the 48 s travel
    assert hass.states.get(ENTITY_ID).state == "closing"
    assert attrs(hass)["current_position"] == 50
    assert attrs(hass)["current_tilt_position"] == 0


async def test_run_ends_at_end_stop(
    hass: HomeAssistant, at_top, respond, freezer, mock_transmitter
) -> None:
    await call(hass, "close_cover")
    await tick(hass, freezer, 45)
    # Polling starts shortly before the expected end of the run.
    assert mock_transmitter.info.call_count >= 2
    await respond(INFO_BOTTOM_POSITION_STOP)
    assert hass.states.get(ENTITY_ID).state == "closed"
    mock_transmitter.stop.assert_not_called()


async def test_run_without_end_stop_report(
    hass: HomeAssistant, at_top, freezer
) -> None:
    """If the drive never confirms, assume it reached the end stop."""
    await call(hass, "close_cover")
    await tick(hass, freezer, 50 + END_STOP_GRACE + 2)
    assert not get_entity(hass, ENTITY_ID).is_closing
    assert attrs(hass)["current_position"] == 0


async def test_stop_keeps_estimate(
    hass: HomeAssistant, at_bottom, respond, freezer, mock_transmitter
) -> None:
    await call(hass, "open_cover")
    await tick(hass, freezer, 2 + 20)  # slats + 40 % of 50 s
    await call(hass, "stop_cover")
    mock_transmitter.stop.assert_called_once_with(1)
    assert attrs(hass)["current_position"] == 40
    await respond(INFO_STOPPED_IN_UNDEFINED_POSITION)
    assert attrs(hass)["current_position"] == 40


# ── moves started outside HA ────────────────────────────────────────────


async def test_external_move_is_tracked(
    hass: HomeAssistant, at_top, respond, freezer, mock_transmitter
) -> None:
    """A move started on a physical remote is picked up and fast-polled."""
    await respond(INFO_MOVING_DOWN)
    assert hass.states.get(ENTITY_ID).state == "closing"

    await tick(hass, freezer, 10)
    assert mock_transmitter.info.call_count >= 4  # every 2 s
    await respond(INFO_STOPPED_IN_UNDEFINED_POSITION)
    assert not get_entity(hass, ENTITY_ID).is_closing
    assert model(hass).position == pytest.approx(100 - 8 / 48 * 100)


# ── timed moves ─────────────────────────────────────────────────────────


async def test_set_position_runs_timed_move(
    hass: HomeAssistant, at_top, freezer, mock_transmitter
) -> None:
    await call(hass, "set_cover_position", position=40)
    mock_transmitter.down.assert_called_once_with(1)

    # (the test helper fires timers up to 0.5 s early)
    await tick(hass, freezer, 2 + 0.6 * 48 - 1)
    mock_transmitter.stop.assert_not_called()
    await tick(hass, freezer, 1)
    mock_transmitter.stop.assert_called_once_with(1)
    assert attrs(hass)["current_position"] == 40

    # The drive's status is confirmed shortly after.
    mock_transmitter.info.reset_mock()
    await tick(hass, freezer, 2)
    mock_transmitter.info.assert_called_once()


@pytest.mark.parametrize(("target", "command"), [(100, "up"), (0, "down")])
async def test_set_end_position_runs_to_end_stop(
    hass: HomeAssistant, cover, respond, freezer, mock_transmitter, target, command
) -> None:
    await respond(INFO_INTERMEDIATE_POSITION_STOP)
    await call(hass, "set_cover_position", position=target)
    getattr(mock_transmitter, command).assert_called_once_with(1)
    await tick(hass, freezer, 30)
    mock_transmitter.stop.assert_not_called()


async def test_set_position_calibrates_when_unknown(
    hass: HomeAssistant, cover, respond, freezer, mock_transmitter
) -> None:
    await call(hass, "set_cover_position", position=40)
    mock_transmitter.up.assert_called_once_with(1)
    await respond(INFO_TOP_POSITION_STOP)
    await hass.async_block_till_done()
    mock_transmitter.down.assert_called_once_with(1)
    await tick(hass, freezer, 2 + 0.6 * 48)
    assert attrs(hass)["current_position"] == 40


async def test_set_tilt_runs_timed_pulse(
    hass: HomeAssistant, at_bottom, freezer, mock_transmitter
) -> None:
    await call(hass, "set_cover_tilt_position", tilt_position=50)
    mock_transmitter.up.assert_called_once_with(1)
    await tick(hass, freezer, 1)
    mock_transmitter.stop.assert_called_once_with(1)
    assert attrs(hass)["current_tilt_position"] == 50
    assert attrs(hass)["current_position"] == 0


async def test_tilt_buttons_rotate_slats(
    hass: HomeAssistant, mock_transmitter, respond, freezer
) -> None:
    await setup_entry(hass, make_entry([cover_subentry(tilt_buttons="slats")]))
    await respond(INFO_BOTTOM_POSITION_STOP)
    await call(hass, "open_cover_tilt")
    mock_transmitter.up.assert_called_once_with(1)
    mock_transmitter.intermediate.assert_not_called()
    await tick(hass, freezer, 2)
    assert attrs(hass)["current_tilt_position"] == 100


@pytest.mark.parametrize(
    ("tilt", "command"), [(20, "ventilation_tilting"), (80, "intermediate")]
)
async def test_set_tilt_without_slat_timing(
    hass: HomeAssistant, mock_transmitter, tilt, command
) -> None:
    await setup_entry(hass, make_entry([cover_subentry(tilt_time=0)]))
    await call(hass, "set_cover_tilt_position", tilt_position=tilt)
    getattr(mock_transmitter, command).assert_called_once_with(1)


async def test_shutter_has_no_tilt_phase(
    hass: HomeAssistant, mock_transmitter, respond, freezer
) -> None:
    shutter = cover_subentry(
        device_class="roller shutter",
        features=["up", "down", "stop", "set_position"],
        travel_up=30,
        travel_down=30,
    )
    await setup_entry(hass, make_entry([shutter]))
    await respond(INFO_BOTTOM_POSITION_STOP)
    await call(hass, "open_cover")
    await tick(hass, freezer, 15)
    assert attrs(hass)["current_position"] == 50
    assert attrs(hass)["device_class"] == "shutter"


# ── learning travel times ───────────────────────────────────────────────


async def test_learns_travel_time_from_full_run(
    hass: HomeAssistant, mock_transmitter, respond, freezer
) -> None:
    await setup_entry(hass, make_entry([cover_subentry(learn=True)]))
    await respond(INFO_BOTTOM_POSITION_STOP)
    await call(hass, "open_cover")
    # The drive is slower than configured (52 s): still moving at 54 s.
    await tick(hass, freezer, 54)
    await respond(INFO_MOVING_UP)
    await tick(hass, freezer, 1)
    await respond(INFO_TOP_POSITION_STOP)

    # End stop between 54 s and 55 s → 54.5 s; learned 30 % of the way.
    assert attrs(hass)["travel_time_up"] == pytest.approx(52 + 0.3 * 2.5, abs=0.1)
    assert attrs(hass)["travel_time_down"] == 50


async def test_no_learning_unless_enabled(
    hass: HomeAssistant, at_bottom, respond, freezer
) -> None:
    await call(hass, "open_cover")
    await tick(hass, freezer, 54)
    await respond(INFO_MOVING_UP)
    await tick(hass, freezer, 1)
    await respond(INFO_TOP_POSITION_STOP)
    assert attrs(hass)["travel_time_up"] == 52


# ── persistence ─────────────────────────────────────────────────────────


def _stored(position, tilt, *, learned=None, move=None):
    return {
        "position": position,
        "tilt": tilt,
        "learned": learned or {},
        "move": move,
    }


async def test_restores_precise_state_and_learned_times(
    hass: HomeAssistant, mock_transmitter
) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(ENTITY_ID, "open", {"current_position": 37}),
                _stored(37.4, 50.0, learned={"travel_time_up": 55.5}),
            )
        ],
    )
    await setup_entry(hass, make_entry([cover_subentry(learn=True)]))
    assert model(hass).position == pytest.approx(37.4)
    assert attrs(hass)["current_tilt_position"] == 50
    assert attrs(hass)["travel_time_up"] == 55.5


async def test_restores_pre_42_state(hass: HomeAssistant, mock_transmitter) -> None:
    mock_restore_cache(
        hass,
        [State(ENTITY_ID, "open", {"current_position": 60, "current_tilt_position": 30})],
    )
    await setup_entry(hass, make_entry())
    assert attrs(hass)["current_position"] == 60
    assert attrs(hass)["current_tilt_position"] == 30


async def test_restores_move_interrupted_by_restart(
    hass: HomeAssistant, mock_transmitter, freezer
) -> None:
    """HA went down 20 s into closing from the top: account for those 20 s."""
    started = time.time() - 20
    move = {
        "direction": -1,
        "kind": "run",
        "started_wall": started,
        "position": 100.0,
        "tilt": 100.0,
        "duration": None,
    }
    mock_restore_cache_with_extra_data(
        hass,
        [(State(ENTITY_ID, "closing", {"current_position": 100}), _stored(100.0, 100.0, move=move))],
    )
    await setup_entry(hass, make_entry())
    assert model(hass).position == pytest.approx(100 - 18 / 48 * 100)
    assert attrs(hass)["current_tilt_position"] == 0


async def test_restored_finished_run_lands_on_end_stop(
    hass: HomeAssistant, mock_transmitter, freezer
) -> None:
    move = {
        "direction": 1,
        "kind": "run",
        "started_wall": time.time() - 600,
        "position": 0.0,
        "tilt": 0.0,
        "duration": None,
    }
    mock_restore_cache_with_extra_data(
        hass,
        [(State(ENTITY_ID, "opening", {"current_position": 0}), _stored(0.0, 0.0, move=move))],
    )
    await setup_entry(hass, make_entry())
    assert attrs(hass)["current_position"] == 100


async def test_stored_data_contains_precise_state(
    hass: HomeAssistant, at_bottom, freezer
) -> None:
    await call(hass, "open_cover")
    await tick(hass, freezer, 2 + 10)
    data = get_entity(hass, ENTITY_ID).extra_restore_state_data.as_dict()
    assert data["position"] == pytest.approx(20)
    assert data["tilt"] == 100
    assert data["move"]["direction"] == 1
    assert data["move"]["kind"] == "run"
    assert data["move"]["position"] == 0
