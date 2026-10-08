"""Test the cover options and motion model."""

from __future__ import annotations

import pytest

from custom_components.elero.const import PRESET_FIXED, PRESET_STEP_UP
from custom_components.elero.model import (
    DOWN,
    UP,
    CoverOptions,
    CoverState,
    PresetOptions,
    advance,
    time_to_position,
    time_to_tilt,
)

VENETIAN = CoverOptions(
    channel=1,
    features=frozenset({"up", "down", "stop", "close_tilt", "set_tilt_position"}),
    travel_up=52.0,
    travel_down=50.0,
    tilt_time=2.0,
)
SHUTTER = CoverOptions(
    channel=2,
    features=frozenset({"up", "down", "stop"}),
    travel_up=30.0,
    travel_down=30.0,
    tilt_time=2.0,  # ignored: no tilt features
)


def test_options_from_pre_42_data() -> None:
    """Old sub-entries: one travel time, tilt step → ventilation step up."""
    opts = CoverOptions.from_data(
        {
            "channel": 3,
            "supported_features": ["up", "down", "close_tilt"],
            "travel_time": 50,
            "tilt_step": 2,
            "tilt_travel_time": 1,
        }
    )
    assert (opts.travel_up, opts.travel_down) == (50, 50)
    assert opts.ventilation == PresetOptions(mode=PRESET_STEP_UP, position=25, duration=1.0)
    assert opts.intermediate.mode == PRESET_FIXED
    assert opts.intermediate.position == 75
    assert opts.learn_travel_times is False


def test_options_without_tilt_step_keep_fixed_ventilation() -> None:
    opts = CoverOptions.from_data({"channel": 1, "tilt_step": 0})
    assert opts.ventilation.mode == PRESET_FIXED
    assert opts.ventilation.position == 25


def test_options_from_new_data() -> None:
    opts = CoverOptions.from_data(
        {
            "channel": 1,
            "travel_time_up": 40,
            "travel_time_down": 35,
            "ventilation": {"mode": "step_down", "position": 10, "duration": 0.5},
        }
    )
    assert (opts.travel_up, opts.travel_down) == (40, 35)
    assert opts.ventilation.direction == DOWN
    assert opts.ventilation.duration == 0.5


def test_tilt_phase_only_with_tilt_features() -> None:
    assert VENETIAN.has_tilt
    assert not SHUTTER.has_tilt
    assert SHUTTER.vertical_seconds(UP) == 30


def test_slats_turn_before_the_blind_travels() -> None:
    start = CoverState(position=0, tilt=0)
    # First second: only the slats move (half of the 2 s swing).
    assert advance(start, UP, 1.0, VENETIAN) == CoverState(0, 50)
    # After the swing the blind travels; vertical time = 52 - 2 = 50 s.
    assert advance(start, UP, 2.0 + 25.0, VENETIAN) == CoverState(50, 100)


def test_moving_down_closes_slats_first() -> None:
    start = CoverState(position=100, tilt=100)
    state = advance(start, DOWN, 2.0 + 24.0, VENETIAN)  # vertical down = 48 s
    assert state.tilt == 0
    assert state.position == pytest.approx(50)


def test_positions_are_clamped() -> None:
    assert advance(CoverState(90, 100), UP, 100, VENETIAN).position == 100
    assert advance(CoverState(10, 0), DOWN, 100, VENETIAN).position == 0


def test_unknown_position_stays_unknown() -> None:
    state = advance(CoverState(None, 0), UP, 10, VENETIAN)
    assert state.position is None
    assert state.tilt == 100


def test_shutter_has_no_tilt_phase() -> None:
    assert advance(CoverState(0, None), UP, 15, SHUTTER) == CoverState(50, None)


def test_time_to_position_includes_tilt_phase() -> None:
    direction, seconds = time_to_position(CoverState(100, 100), 40, VENETIAN)
    assert direction == DOWN
    assert seconds == pytest.approx(2.0 + 0.6 * 48)


def test_time_to_position_round_trips() -> None:
    start = CoverState(30, 20)
    direction, seconds = time_to_position(start, 70, VENETIAN)
    assert advance(start, direction, seconds, VENETIAN).position == pytest.approx(70)


def test_time_to_tilt() -> None:
    assert time_to_tilt(CoverState(0, 0), 50, VENETIAN) == (UP, 1.0)
    assert time_to_tilt(CoverState(0, 100), 25, VENETIAN) == (DOWN, 1.5)
    assert time_to_tilt(CoverState(0, 0), 50, SHUTTER) == (0, 0.0)


def test_time_to_tilt_guesses_unknown_tilt_from_position() -> None:
    assert time_to_tilt(CoverState(80, None), 0, VENETIAN) == (DOWN, 2.0)
    assert time_to_tilt(CoverState(10, None), 100, VENETIAN) == (UP, 2.0)


def test_ventilation_step_of_one_second_opens_slats() -> None:
    """The user's setup: ventilation = drive up ~1 s from wherever it is."""
    opts = CoverOptions.from_data(
        {
            "channel": 1,
            "supported_features": ["up", "down", "close_tilt"],
            "travel_time": 50,
            "tilt_step": 2,
            "tilt_travel_time": 1,
        }
    )
    preset = opts.ventilation
    state = advance(CoverState(0, 0), preset.direction, preset.duration, opts)
    assert state == CoverState(0, 100)
    # From halfway down with closed slats it also just opens the slats.
    state = advance(CoverState(50, 0), preset.direction, preset.duration, opts)
    assert state == CoverState(50, 100)
