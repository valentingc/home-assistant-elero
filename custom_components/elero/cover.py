"""Support for Elero cover components."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverEntity,
    CoverEntityFeature,
    PLATFORM_SCHEMA as COVER_PLATFORM_SCHEMA,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_COVERS, CONF_DEVICE_CLASS, CONF_NAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.restore_state import (
    ExtraStoredData,
    RestoredExtraData,
    RestoreEntity,
)

from .const import (
    CONF_CHANNEL,
    CONF_SUPPORTED_FEATURES,
    CONF_TILT_STEP,
    CONF_TILT_TRAVEL_TIME,
    CONF_TRANSMITTER_SERIAL_NUMBER,
    CONF_TRAVEL_TIME,
    DEFAULT_TILT_STEP,
    DEFAULT_TILT_TRAVEL_TIME,
    DEFAULT_TRAVEL_TIME,
    DOMAIN,
    ELERO_COVER_DEVICE_CLASSES,
    INFO_BLOCKING,
    INFO_BOTTOM_POS_STOP_WICH_INT_POS,
    INFO_BOTTOM_POSITION_STOP,
    INFO_INTERMEDIATE_POSITION_STOP,
    INFO_MOVING_DOWN,
    INFO_MOVING_UP,
    INFO_OVERHEATED,
    INFO_START_TO_MOVE_DOWN,
    INFO_START_TO_MOVE_UP,
    INFO_STOPPED_IN_UNDEFINED_POSITION,
    INFO_TILT_VENTILATION_POS_STOP,
    INFO_TIMEOUT,
    INFO_TOP_POS_STOP_WICH_TILT_POS,
    INFO_TOP_POSITION_STOP,
    POLL_INTERVAL_ENDGAME,
    POLL_INTERVAL_IDLE,
    POLL_INTERVAL_MOVING,
    PRESET_FIXED,
    SUBENTRY_TYPE_COVER,
    TILT_BUTTONS_SLATS,
)
from .hub import EleroHub
from .model import (
    CLOSED,
    DOWN,
    OPEN,
    UP,
    CoverOptions,
    CoverState,
    PresetOptions,
    advance,
    time_to_position,
    time_to_tilt,
)

_LOGGER = logging.getLogger(__name__)

ATTR_ELERO_STATE = "elero_state"

SUPPORTED_FEATURES = {
    "close_tilt": CoverEntityFeature.CLOSE_TILT,
    "down": CoverEntityFeature.CLOSE,
    "open_tilt": CoverEntityFeature.OPEN_TILT,
    "set_position": CoverEntityFeature.SET_POSITION,
    "set_tilt_position": CoverEntityFeature.SET_TILT_POSITION,
    "stop_tilt": CoverEntityFeature.STOP_TILT,
    "stop": CoverEntityFeature.STOP,
    "up": CoverEntityFeature.OPEN,
}

STATUSES_UP = (INFO_START_TO_MOVE_UP, INFO_MOVING_UP)
STATUSES_DOWN = (INFO_START_TO_MOVE_DOWN, INFO_MOVING_DOWN)
STATUSES_VENTILATION = (INFO_TILT_VENTILATION_POS_STOP, INFO_TOP_POS_STOP_WICH_TILT_POS)
STATUSES_INTERMEDIATE = (
    INFO_INTERMEDIATE_POSITION_STOP,
    INFO_BOTTOM_POS_STOP_WICH_INT_POS,
)
STATUSES_FAULT = (INFO_BLOCKING, INFO_OVERHEATED, INFO_TIMEOUT)

# Moves shorter than this are not worth sending.
MIN_MOVE_SECONDS = 0.3
# Movement reports this long after a self-terminating preset step are stale.
PRESET_GRACE = 3.0
# Give up waiting for an end stop this long after the expected end of a run.
END_STOP_GRACE = 10.0
# Poll once more this long after a move ends, to confirm the drive's status.
CONFIRM_POLL_DELAY = 1.5
# How often the UI is refreshed while a cover is moving.
UI_TICK = 1.0
# Learned travel times move this far towards each new measurement.
LEARN_RATE = 0.3
# Measurements further off than this are ignored as outliers.
LEARN_TOLERANCE = (0.6, 1.5)
# Only learn if the end stop was bracketed by polls at most this far apart.
LEARN_MAX_BRACKET = 3.0

# Move kinds
MOVE_RUN = "run"  # open/close to an end stop
MOVE_TIMED = "timed"  # we send STOP after `duration`
MOVE_PRESET = "preset"  # the drive stops itself after `duration`
MOVE_EXTERNAL = "external"  # started outside HA (physical remote), end unknown


# ── Legacy YAML schema (still accepted, auto-imported as sub-entries) ────

ELERO_COVER_DEVICE_CLASSES_SCHEMA = vol.All(
    vol.Lower, vol.In(ELERO_COVER_DEVICE_CLASSES)
)
SUPPORTED_FEATURES_SCHEMA = vol.All(cv.ensure_list, [vol.In(SUPPORTED_FEATURES)])
CHANNEL_NUMBERS_SCHEMA = vol.All(vol.Coerce(int), vol.Range(min=1, max=15))

COVER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CHANNEL): CHANNEL_NUMBERS_SCHEMA,
        vol.Required(CONF_DEVICE_CLASS): ELERO_COVER_DEVICE_CLASSES_SCHEMA,
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_SUPPORTED_FEATURES): SUPPORTED_FEATURES_SCHEMA,
        vol.Required(CONF_TRANSMITTER_SERIAL_NUMBER): str,
        vol.Optional(CONF_TRAVEL_TIME, default=DEFAULT_TRAVEL_TIME): vol.Coerce(float),
        vol.Optional(CONF_TILT_STEP, default=DEFAULT_TILT_STEP): vol.Coerce(float),
        vol.Optional(
            CONF_TILT_TRAVEL_TIME, default=DEFAULT_TILT_TRAVEL_TIME
        ): vol.Coerce(float),
    }
)

PLATFORM_SCHEMA = COVER_PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_COVERS): vol.Schema({cv.slug: COVER_SCHEMA})}
)


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Legacy `cover: - platform: elero` YAML.

    These covers are imported as sub-entries of their transmitter when it is
    set up (see `_auto_import_yaml_covers`), so nothing is created here.
    """
    _LOGGER.warning(
        "Elero covers in configuration.yaml are imported into the UI "
        "automatically; remove the `platform: elero` cover configuration"
    )


# ── Config entry setup ──────────────────────────────────────────────────


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Elero covers from the cover sub-entries of a config entry."""
    hub: EleroHub = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_COVER:
            continue
        cover = EleroCover(hub, subentry)
        hub.covers[subentry_id] = cover
        async_add_entities([cover], config_subentry_id=subentry_id)


def cover_device_info(hub: EleroHub, subentry: ConfigSubentry) -> DeviceInfo:
    channel = int(subentry.data[CONF_CHANNEL])
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{hub.serial_number}_{channel}")},
        name=subentry.title,
        manufacturer="Elero",
        model=subentry.data[CONF_DEVICE_CLASS],
    )
    if hub.hub_device_id:
        info["via_device_id"] = hub.hub_device_id
    return info


# ── Entity ──────────────────────────────────────────────────────────────


@dataclass
class _Move:
    direction: int
    kind: str
    started: float  # time.monotonic()
    started_wall: float  # time.time(), survives restarts
    start: CoverState
    duration: float | None = None  # known run time (timed / preset moves)
    preset: PresetOptions | None = None
    learn: bool = False  # full run from one end stop, usable for learning
    last_moving_seen: float | None = None
    stopping: bool = False  # STOP is queued; the move ends when it goes out
    timers: list[CALLBACK_TYPE] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "kind": self.kind,
            "started_wall": self.started_wall,
            "position": self.start.position,
            "tilt": self.start.tilt,
            "duration": self.duration,
        }


class EleroCover(CoverEntity, RestoreEntity):
    """An Elero drive on one channel of a transmitter stick.

    Position and tilt are estimated with the motion model in `model.py` while
    the drive moves, and corrected whenever the drive reports an end stop or
    a fixed preset position. The precise estimate, any move in progress and
    learned travel times are stored with the entity and survive restarts.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, hub: EleroHub, subentry: ConfigSubentry) -> None:
        self._hub = hub
        self._opts = CoverOptions.from_data(subentry.data)
        self._timing = self._opts
        self._channel = self._opts.channel

        self._attr_unique_id = f"{hub.serial_number}_{self._channel}"
        self._attr_device_class = ELERO_COVER_DEVICE_CLASSES[
            subentry.data[CONF_DEVICE_CLASS]
        ]
        self._attr_device_info = cover_device_info(hub, subentry)
        feature_mask = CoverEntityFeature(0)
        for name in self._opts.features:
            feature_mask |= SUPPORTED_FEATURES[name]
        self._attr_supported_features = feature_mask

        self._state = CoverState()
        self._move: _Move | None = None
        self._elero_state: str | None = None
        self._pending_target: float | None = None
        self._ignore_movement_until = 0.0
        self._learned: dict[str, float] = {}
        self._available = False
        self._ui_tick: CALLBACK_TYPE | None = None
        self._confirm_poll: CALLBACK_TYPE | None = None

    # ── lifecycle and persistence ───────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_restore()

        self._available = self._hub.async_register(self._channel, self._async_on_status)
        self.async_on_remove(lambda: self._hub.async_unregister(self._channel))
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_idle_poll,
                timedelta(seconds=POLL_INTERVAL_IDLE),
            )
        )
        self.async_on_remove(self._cancel_all_timers)
        self._hub.async_request_poll(self._channel)

    async def _async_restore(self) -> None:
        if (state := await self.async_get_last_state()) is not None:
            # Rounded values, used if no precise data was stored (pre-4.3).
            position = state.attributes.get("current_position")
            tilt = state.attributes.get("current_tilt_position")
            self._state = CoverState(
                None if position is None else float(position),
                None if tilt is None else float(tilt),
            )
            self._elero_state = state.attributes.get(ATTR_ELERO_STATE)

        extra = await self.async_get_last_extra_data()
        if extra is None:
            return
        data = extra.as_dict()
        self._learned = {
            k: float(v) for k, v in (data.get("learned") or {}).items() if v is not None
        }
        self._apply_learned()
        if "position" in data or "tilt" in data:
            self._state = CoverState(data.get("position"), data.get("tilt"))
        if move := data.get("move"):
            self._restore_move(move)

    def _restore_move(self, move: dict[str, Any]) -> None:
        """Account for a move that was running when Home Assistant stopped."""
        direction = int(move["direction"])
        start = CoverState(move.get("position"), move.get("tilt"))
        elapsed = max(0.0, time.time() - float(move["started_wall"]))
        duration = move.get("duration")
        if duration is not None:
            # Timed moves were stopped by us, or not at all if HA went down
            # first; either way the estimate at `duration` is the best guess.
            elapsed = min(elapsed, float(duration))
        elif move["kind"] in (MOVE_RUN, MOVE_EXTERNAL) and start.position is not None:
            if elapsed >= self._expected_run_time(start, direction):
                self._state = self._end_stop_state(direction, start)
                return
        self._state = advance(start, direction, elapsed, self._timing)
        _LOGGER.debug("%s: restored interrupted move, now %s", self.entity_id, self._state)

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        state = self._current()
        return RestoredExtraData(
            {
                "position": state.position,
                "tilt": state.tilt,
                "learned": dict(self._learned),
                "move": None if self._move is None else self._move.as_dict(),
            }
        )

    def _apply_learned(self) -> None:
        if not self._opts.learn_travel_times:
            self._timing = self._opts
            return
        self._timing = self._opts.with_travel_times(
            self._learned.get("travel_time_up", self._opts.travel_up),
            self._learned.get("travel_time_down", self._opts.travel_down),
        )

    # ── state ───────────────────────────────────────────────────────────

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _current(self) -> CoverState:
        move = self._move
        if move is None:
            return self._state
        elapsed = self._now() - move.started
        if move.duration is not None:
            elapsed = min(elapsed, move.duration)
        return advance(move.start, move.direction, elapsed, self._timing)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def current_cover_position(self) -> int | None:
        position = self._current().position
        return None if position is None else round(position)

    @property
    def current_cover_tilt_position(self) -> int | None:
        tilt = self._current().tilt
        return None if tilt is None else round(tilt)

    @property
    def is_opening(self) -> bool:
        return self._move is not None and self._move.direction == UP

    @property
    def is_closing(self) -> bool:
        return self._move is not None and self._move.direction == DOWN

    @property
    def is_closed(self) -> bool | None:
        position = self.current_cover_position
        return None if position is None else position == 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "channel": self._channel,
            "travel_time_up": round(self._timing.travel_up, 1),
            "travel_time_down": round(self._timing.travel_down, 1),
        }
        if self._elero_state is not None:
            data[ATTR_ELERO_STATE] = self._elero_state
        return data

    # ── movement tracking ───────────────────────────────────────────────

    def _cancel_move_timers(self) -> None:
        if self._move is not None:
            for cancel in self._move.timers:
                cancel()
            self._move.timers.clear()
        if self._ui_tick is not None:
            self._ui_tick()
            self._ui_tick = None

    def _cancel_all_timers(self) -> None:
        self._cancel_move_timers()
        if self._confirm_poll is not None:
            self._confirm_poll()
            self._confirm_poll = None

    def _later(self, delay: float, action) -> None:
        """Schedule `action()` for the current move; cancelled when it ends."""
        assert self._move is not None
        self._move.timers.append(
            async_call_later(self.hass, max(0.0, delay), callback(lambda _now: action()))
        )

    @callback
    def _begin_move(
        self,
        direction: int,
        kind: str,
        *,
        duration: float | None = None,
        preset: PresetOptions | None = None,
        schedule: bool = True,
    ) -> _Move:
        """Start tracking a move.

        Moves we command are created with `schedule=False` and re-anchored by
        `_async_send_move` once the command has actually gone out.
        """
        # Freeze the estimate of any running move without reporting it stopped.
        start = self._current()
        self._cancel_move_timers()
        self._state = start
        self._move = move = _Move(
            direction=direction,
            kind=kind,
            started=self._now(),
            started_wall=time.time(),
            start=start,
            duration=duration,
            preset=preset,
            learn=(
                kind == MOVE_RUN
                and start.position == (CLOSED if direction == UP else OPEN)
            ),
        )
        if schedule:
            self._schedule_move_timers(move)
        self._ui_tick = async_track_time_interval(
            self.hass,
            callback(lambda _now: self.async_write_ha_state()),
            timedelta(seconds=UI_TICK),
        )
        _LOGGER.debug(
            "%s: %s move %s from %s",
            self.entity_id,
            kind,
            "up" if direction == UP else "down",
            start,
        )
        self.async_write_ha_state()
        return move

    def _schedule_move_timers(self, move: _Move) -> None:
        elapsed = self._now() - move.started
        if move.duration is not None:
            self._later(move.duration - elapsed, self._async_on_move_duration_elapsed)
        if move.kind == MOVE_RUN:
            # Poll from shortly before the drive should reach its end stop.
            expected = self._expected_run_time(move.start, move.direction)
            self._later(expected * 0.85 - elapsed, self._async_endgame_poll)
        elif move.kind == MOVE_EXTERNAL:
            self._later(POLL_INTERVAL_MOVING, self._async_external_poll)

    async def _async_send_move(self, move: _Move, command: str) -> float | None:
        """Send the command that starts `move`, then anchor it to the send time.

        The stick sends one request at a time, so with several covers (a
        cover group, or polls in flight) a command can go out noticeably
        later than requested. The drive only starts moving then, so that is
        when the move, its timed stop and its polls are measured from.
        """
        try:
            sent = await self._hub.async_command(self._channel, command)
        except Exception:
            if self._move is move:
                self._end_move()
            raise
        if self._move is not move or move.stopping:
            return None
        move.started = sent
        move.started_wall = time.time() - (self._now() - sent)
        self._schedule_move_timers(move)
        self.async_write_ha_state()
        return sent

    async def _async_stop(self, move: _Move | None) -> None:
        """Send STOP and end `move` where the drive was when STOP went out."""
        if move is not None:
            move.stopping = True
            for cancel in move.timers:
                cancel()
            move.timers.clear()
        try:
            sent = await self._hub.async_command(self._channel, "stop")
        except Exception:
            if move is not None and self._move is move:
                self._end_move()
            raise
        if move is not None and self._move is move:
            elapsed = sent - move.started
            if move.duration is not None:
                elapsed = min(elapsed, move.duration)
            self._end_move(advance(move.start, move.direction, elapsed, self._timing))
        self._schedule_confirm_poll()

    @callback
    def _end_move(self, final: CoverState | None = None) -> None:
        """Stop tracking the current move at `final` (default: the estimate)."""
        state = self._current()
        self._cancel_move_timers()
        self._move = None
        self._state = final if final is not None else state
        _LOGGER.debug("%s: stopped at %s", self.entity_id, self._state)
        self.async_write_ha_state()

    def _expected_run_time(self, start: CoverState, direction: int) -> float:
        if start.position is None:
            return self._timing.travel_time(direction)
        target = OPEN if direction == UP else CLOSED
        return time_to_position(start, target, self._timing)[1]

    def _end_stop_state(
        self, direction: int, current: CoverState | None = None
    ) -> CoverState:
        position = OPEN if direction == UP else CLOSED
        if self._timing.has_tilt:
            tilt = position
        else:
            tilt = (current or self._current()).tilt
        return CoverState(position, tilt)

    @callback
    def _async_on_move_duration_elapsed(self) -> None:
        move = self._move
        if move is None:
            return
        if move.kind == MOVE_TIMED:
            # Keep estimating until STOP actually goes out (the stick may be
            # busy, e.g. stopping the other covers of a group first).
            move.duration = None
            self.hass.async_create_task(self._async_stop(move))
        elif move.kind == MOVE_PRESET:
            self._finish_preset(move)

    @callback
    def _async_endgame_poll(self) -> None:
        move = self._move
        if move is None:
            return
        elapsed = self._now() - move.started
        if elapsed > self._expected_run_time(move.start, move.direction) + END_STOP_GRACE:
            # No end stop reported: the drive will have stopped there anyway.
            self._end_move(self._end_stop_state(move.direction))
            return
        self._hub.async_request_poll(self._channel)
        self._later(POLL_INTERVAL_ENDGAME, self._async_endgame_poll)

    @callback
    def _async_external_poll(self) -> None:
        move = self._move
        if move is None:
            return
        limit = self._timing.travel_time(move.direction) + END_STOP_GRACE
        if self._now() - move.started > limit:
            self._end_move()
            return
        self._hub.async_request_poll(self._channel)
        self._later(POLL_INTERVAL_MOVING, self._async_external_poll)

    @callback
    def _async_idle_poll(self, _now=None) -> None:
        if self._move is None:
            self._hub.async_request_poll(self._channel)

    def _schedule_confirm_poll(self) -> None:
        if self._confirm_poll is not None:
            self._confirm_poll()

        @callback
        def _poll(_now) -> None:
            self._confirm_poll = None
            self._hub.async_request_poll(self._channel)

        self._confirm_poll = async_call_later(self.hass, CONFIRM_POLL_DELAY, _poll)

    # ── presets (Elero ventilation / intermediate) ──────────────────────

    async def async_ventilation(self) -> None:
        """Send the Elero ventilation / tilting command."""
        await self._async_preset(self._opts.ventilation, "ventilation_tilting")

    async def async_intermediate(self) -> None:
        """Send the Elero intermediate position command."""
        await self._async_preset(self._opts.intermediate, "intermediate")

    async def _async_preset(self, preset: PresetOptions, command: str) -> None:
        self._pending_target = None
        move: _Move | None = None
        if preset.mode == PRESET_FIXED:
            current = self._current()
            if current.position is not None:
                direction, seconds = time_to_position(
                    current, preset.position, self._timing
                )
                if direction:
                    move = self._begin_move(
                        direction,
                        MOVE_PRESET,
                        duration=seconds,
                        preset=preset,
                        schedule=False,
                    )
        else:
            self._ignore_movement_until = self._now() + preset.duration + PRESET_GRACE
            move = self._begin_move(
                preset.direction,
                MOVE_PRESET,
                duration=preset.duration,
                preset=preset,
                schedule=False,
            )
        if move is None:
            await self._hub.async_command(self._channel, command)
            return
        sent = await self._async_send_move(move, command)
        if sent is not None and preset.mode != PRESET_FIXED:
            self._ignore_movement_until = sent + preset.duration + PRESET_GRACE

    @callback
    def _finish_preset(self, move: _Move) -> None:
        final = advance(move.start, move.direction, move.duration or 0, self._timing)
        if move.preset is not None and move.preset.mode == PRESET_FIXED:
            final = CoverState(move.preset.position, final.tilt)
        self._end_move(final)
        self._schedule_confirm_poll()

    @callback
    def _on_preset_stop(self, preset: PresetOptions) -> None:
        """The drive reports it is stopped at a preset position."""
        move = self._move
        if move is not None and move.kind == MOVE_PRESET:
            self._finish_preset(move)
        elif preset.mode == PRESET_FIXED:
            self._end_move(CoverState(preset.position, self._current().tilt))
        elif move is not None:
            # A step started elsewhere (physical remote): keep the estimate.
            self._end_move()
        # A step preset reported while idle is just the drive repeating its
        # last stop. It says nothing about the position, which is relative,
        # so the estimate stays as it is.

    # ── device status ───────────────────────────────────────────────────

    @callback
    def _async_on_status(self, status: str) -> None:
        if status != self._elero_state and status in STATUSES_FAULT:
            _LOGGER.warning("%s reports '%s'", self.entity_id, status)
        self._elero_state = status

        if status == INFO_TOP_POSITION_STOP:
            self._on_end_stop(UP)
        elif status == INFO_BOTTOM_POSITION_STOP:
            self._on_end_stop(DOWN)
        elif status in STATUSES_VENTILATION:
            self._on_preset_stop(self._opts.ventilation)
        elif status in STATUSES_INTERMEDIATE:
            self._on_preset_stop(self._opts.intermediate)
        elif self._move is not None and self._move.stopping:
            # Our STOP is on its way; `_async_stop` ends the move at the time
            # it went out, which is more precise than this report.
            pass
        elif status in STATUSES_UP or status in STATUSES_DOWN:
            self._on_moving(UP if status in STATUSES_UP else DOWN)
        elif status == INFO_STOPPED_IN_UNDEFINED_POSITION:
            if self._move is not None and self._move.kind != MOVE_PRESET:
                self._end_move()
        elif self._move is not None:
            # Faults and anything unexpected: the drive is not moving any more.
            # Keep the estimate rather than forgetting the position.
            self._end_move()
        self.async_write_ha_state()

    @callback
    def _on_moving(self, direction: int) -> None:
        move = self._move
        if move is not None and move.direction == direction:
            move.last_moving_seen = self._now()
            return
        if self._now() < self._ignore_movement_until:
            return  # stale report of a preset step that already finished
        self._begin_move(direction, MOVE_EXTERNAL)
        # A remote that moved this cover may have moved others as well.
        self._hub.async_request_poll_others(self._channel)

    @callback
    def _on_end_stop(self, direction: int) -> None:
        move = self._move
        if move is not None and move.learn and move.direction == direction:
            self._learn(move)
        self._end_move(self._end_stop_state(direction))
        if direction == UP and (target := self._pending_target) is not None:
            self._pending_target = None
            self.hass.async_create_task(self.async_set_cover_position(position=target))

    def _learn(self, move: _Move) -> None:
        """Refine the travel time from a full run between the end stops."""
        if not self._opts.learn_travel_times or move.last_moving_seen is None:
            return
        now = self._now()
        if now - move.last_moving_seen > LEARN_MAX_BRACKET:
            return
        # The end stop happened between the last "moving" report and this one.
        measured = (move.last_moving_seen + now) / 2 - move.started
        key = "travel_time_up" if move.direction == UP else "travel_time_down"
        current = self._timing.travel_time(move.direction)
        low, high = LEARN_TOLERANCE
        if not low * current <= measured <= high * current:
            _LOGGER.debug(
                "%s: ignoring travel time sample %.1fs", self.entity_id, measured
            )
            return
        self._learned[key] = round(current + LEARN_RATE * (measured - current), 2)
        self._apply_learned()
        _LOGGER.debug("%s: learned %s = %s", self.entity_id, key, self._learned[key])

    # ── cover services ──────────────────────────────────────────────────

    async def async_open_cover(self, **kwargs: Any) -> None:
        self._pending_target = None
        await self._async_send_move(self._begin_move(UP, MOVE_RUN, schedule=False), "up")

    async def async_close_cover(self, **kwargs: Any) -> None:
        self._pending_target = None
        await self._async_send_move(
            self._begin_move(DOWN, MOVE_RUN, schedule=False), "down"
        )

    async def async_stop_cover(self, **kwargs: Any) -> None:
        self._pending_target = None
        await self._async_stop(self._move)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        target = max(CLOSED, min(OPEN, float(kwargs[ATTR_POSITION])))
        # End positions: let the drive run into its end stop, which is exact.
        if target >= OPEN:
            await self.async_open_cover()
            return
        if target <= CLOSED:
            await self.async_close_cover()
            return
        current = self._current()
        if current.position is None:
            _LOGGER.info(
                "%s: position unknown, opening fully to calibrate", self.entity_id
            )
            move = self._begin_move(UP, MOVE_RUN, schedule=False)
            self._pending_target = target
            await self._async_send_move(move, "up")
            return
        direction, seconds = time_to_position(current, target, self._timing)
        if seconds < MIN_MOVE_SECONDS:
            return
        move = self._begin_move(direction, MOVE_TIMED, duration=seconds, schedule=False)
        await self._async_send_move(move, "up" if direction == UP else "down")

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        target = max(CLOSED, min(OPEN, float(kwargs[ATTR_TILT_POSITION])))
        if not self._timing.has_tilt:
            # No slat timing configured: fall back to the drive's two presets.
            if target < 50:
                await self.async_ventilation()
            else:
                await self.async_intermediate()
            return
        direction, seconds = time_to_tilt(self._current(), target, self._timing)
        if seconds <= 0:
            return
        move = self._begin_move(direction, MOVE_TIMED, duration=seconds, schedule=False)
        await self._async_send_move(move, "up" if direction == UP else "down")

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        if self._opts.tilt_buttons == TILT_BUTTONS_SLATS and self._timing.has_tilt:
            await self.async_set_cover_tilt_position(tilt_position=OPEN)
        else:
            await self.async_intermediate()

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        if self._opts.tilt_buttons == TILT_BUTTONS_SLATS and self._timing.has_tilt:
            await self.async_set_cover_tilt_position(tilt_position=CLOSED)
        else:
            await self.async_ventilation()

    async def async_stop_cover_tilt(self, **kwargs: Any) -> None:
        await self.async_stop_cover()
