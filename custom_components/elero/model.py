"""Cover options and the time-based motion model.

Elero drives only report where they stopped (top, bottom, a preset position)
or that they are moving, never a percentage. Positions in between are
estimated from how long the motor ran:

* A venetian blind first rotates its slats (``tilt_time`` seconds for a full
  swing) before the blind itself starts to travel. Moving up opens the slats,
  moving down closes them.
* After that the blind travels at a constant speed, which may differ between
  up and down.

The configured travel times are full-run stopwatch times, so the tilt phase is
part of them. Pure Python, no Home Assistant imports, so it is easy to test.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from .const import (
    CONF_CHANNEL,
    CONF_INTERMEDIATE,
    CONF_LEARN_TRAVEL_TIMES,
    CONF_PRESET_DURATION,
    CONF_PRESET_MODE,
    CONF_PRESET_POSITION,
    CONF_SUPPORTED_FEATURES,
    CONF_TILT_BUTTONS,
    CONF_TILT_STEP,
    CONF_TILT_TRAVEL_TIME,
    CONF_TRAVEL_TIME,
    CONF_TRAVEL_TIME_DOWN,
    CONF_TRAVEL_TIME_UP,
    CONF_VENTILATION,
    DEFAULT_INTERMEDIATE_POSITION,
    DEFAULT_PRESET_DURATION,
    DEFAULT_TILT_STEP,
    DEFAULT_TILT_TRAVEL_TIME,
    DEFAULT_TRAVEL_TIME,
    DEFAULT_VENTILATION_POSITION,
    PRESET_FIXED,
    PRESET_STEP_DOWN,
    PRESET_STEP_UP,
    TILT_BUTTONS_PRESETS,
    TILT_FEATURES,
)

OPEN = 100.0
CLOSED = 0.0
UP = 1
DOWN = -1

# Never divide by (almost) zero when the tilt phase eats the whole travel time.
_MIN_VERTICAL_SECONDS = 0.5


def _clamp(value: float) -> float:
    return max(CLOSED, min(OPEN, value))


@dataclass(frozen=True)
class PresetOptions:
    """What an Elero preset command (ventilation / intermediate) does."""

    mode: str = PRESET_FIXED
    position: float = DEFAULT_VENTILATION_POSITION  # PRESET_FIXED
    duration: float = DEFAULT_PRESET_DURATION  # PRESET_STEP_UP / _DOWN

    @property
    def direction(self) -> int:
        return {PRESET_STEP_UP: UP, PRESET_STEP_DOWN: DOWN}.get(self.mode, 0)

    @classmethod
    def from_data(cls, data: Mapping[str, Any] | None, default: PresetOptions):
        if not data:
            return default
        return cls(
            mode=data.get(CONF_PRESET_MODE, default.mode),
            position=float(data.get(CONF_PRESET_POSITION, default.position)),
            duration=float(data.get(CONF_PRESET_DURATION, default.duration)),
        )

    def as_data(self) -> dict[str, Any]:
        return {
            CONF_PRESET_MODE: self.mode,
            CONF_PRESET_POSITION: self.position,
            CONF_PRESET_DURATION: self.duration,
        }


@dataclass(frozen=True)
class CoverOptions:
    """Per-cover settings, derived from a cover sub-entry's data."""

    channel: int
    features: frozenset[str]
    travel_up: float = DEFAULT_TRAVEL_TIME
    travel_down: float = DEFAULT_TRAVEL_TIME
    tilt_time: float = DEFAULT_TILT_TRAVEL_TIME
    tilt_buttons: str = TILT_BUTTONS_PRESETS
    learn_travel_times: bool = False
    ventilation: PresetOptions = PresetOptions(position=DEFAULT_VENTILATION_POSITION)
    intermediate: PresetOptions = PresetOptions(position=DEFAULT_INTERMEDIATE_POSITION)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> CoverOptions:
        """Build options, filling in what older (pre-4.2) sub-entries lack."""
        legacy_travel = float(data.get(CONF_TRAVEL_TIME, DEFAULT_TRAVEL_TIME))
        travel_up = float(data.get(CONF_TRAVEL_TIME_UP, legacy_travel))
        travel_down = float(data.get(CONF_TRAVEL_TIME_DOWN, legacy_travel))

        if CONF_VENTILATION in data:
            ventilation = PresetOptions.from_data(
                data[CONF_VENTILATION], cls.ventilation
            )
        else:
            # Before 4.2 a "tilt step" > 0 meant the ventilation button only
            # nudges the cover up by that many percent of the travel.
            tilt_step = float(data.get(CONF_TILT_STEP, DEFAULT_TILT_STEP))
            ventilation = (
                PresetOptions(
                    mode=PRESET_STEP_UP,
                    position=DEFAULT_VENTILATION_POSITION,
                    duration=round(tilt_step / 100 * travel_up, 2),
                )
                if tilt_step > 0
                else cls.ventilation
            )

        return cls(
            channel=int(data[CONF_CHANNEL]),
            features=frozenset(data.get(CONF_SUPPORTED_FEATURES, ())),
            travel_up=travel_up,
            travel_down=travel_down,
            tilt_time=float(data.get(CONF_TILT_TRAVEL_TIME, DEFAULT_TILT_TRAVEL_TIME)),
            tilt_buttons=data.get(CONF_TILT_BUTTONS, TILT_BUTTONS_PRESETS),
            learn_travel_times=bool(data.get(CONF_LEARN_TRAVEL_TIMES, False)),
            ventilation=ventilation,
            intermediate=PresetOptions.from_data(
                data.get(CONF_INTERMEDIATE), cls.intermediate
            ),
        )

    def with_travel_times(self, up: float, down: float) -> CoverOptions:
        return replace(self, travel_up=up, travel_down=down)

    @property
    def has_tilt(self) -> bool:
        """Whether the slat phase is modelled (venetian-style covers)."""
        return self.tilt_time > 0 and bool(self.features & TILT_FEATURES)

    @property
    def effective_tilt_time(self) -> float:
        return self.tilt_time if self.has_tilt else 0.0

    def travel_time(self, direction: int) -> float:
        return self.travel_up if direction > 0 else self.travel_down

    def vertical_seconds(self, direction: int) -> float:
        """Seconds of pure vertical travel for a full run (tilt phase excluded)."""
        return max(
            _MIN_VERTICAL_SECONDS,
            self.travel_time(direction) - self.effective_tilt_time,
        )


@dataclass(frozen=True)
class CoverState:
    """Estimated position and slat tilt; None means unknown."""

    position: float | None = None
    tilt: float | None = None


def _tilt_phase(state: CoverState, direction: int, opts: CoverOptions):
    """Return (start tilt, seconds to rotate the slats fully in `direction`)."""
    tilt_time = opts.effective_tilt_time
    if tilt_time <= 0:
        return state.tilt, 0.0
    start = state.tilt
    if start is None:
        # Unknown slats: assume they need the full swing.
        start = CLOSED if direction > 0 else OPEN
    end = OPEN if direction > 0 else CLOSED
    return start, abs(end - start) / 100 * tilt_time


def advance(
    state: CoverState, direction: int, seconds: float, opts: CoverOptions
) -> CoverState:
    """Estimate the state after the motor ran `seconds` in `direction`."""
    if direction == 0 or seconds <= 0:
        return state
    tilt, tilt_needed = _tilt_phase(state, direction, opts)
    if opts.effective_tilt_time > 0:
        used = min(seconds, tilt_needed)
        tilt = _clamp(tilt + direction * used / opts.effective_tilt_time * 100)
        seconds -= used
    position = state.position
    if position is not None and seconds > 0:
        position = _clamp(
            position + direction * seconds / opts.vertical_seconds(direction) * 100
        )
    return CoverState(position, tilt)


def time_to_position(
    state: CoverState, target: float, opts: CoverOptions
) -> tuple[int, float]:
    """Direction and motor run time to reach `target` (state.position known)."""
    assert state.position is not None
    delta = target - state.position
    if delta == 0:
        return 0, 0.0
    direction = UP if delta > 0 else DOWN
    _, tilt_needed = _tilt_phase(state, direction, opts)
    return direction, tilt_needed + abs(delta) / 100 * opts.vertical_seconds(direction)


def time_to_tilt(
    state: CoverState, target: float, opts: CoverOptions
) -> tuple[int, float]:
    """Direction and motor run time to rotate the slats to `target`."""
    current = state.tilt
    if current is None:
        # Guess from the position: slats of a mostly open blind are usually open.
        current = OPEN if (state.position or 0) >= 50 else CLOSED
    delta = target - current
    if delta == 0 or opts.effective_tilt_time <= 0:
        return 0, 0.0
    return (UP if delta > 0 else DOWN), abs(delta) / 100 * opts.effective_tilt_time
