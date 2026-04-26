"""Support for Elero cover components."""

__version__ = "3.4.28"

import logging
import time

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverEntity,
    CoverEntityFeature,
    PLATFORM_SCHEMA as COVER_PLATFORM_SCHEMA,
)
from homeassistant.const import CONF_COVERS, CONF_DEVICE_CLASS, CONF_NAME
from homeassistant.helpers.restore_state import RestoreEntity

import custom_components.elero as elero
from custom_components.elero import (
    CONF_TRANSMITTER_SERIAL_NUMBER,
    INFO_BLOCKING,
    INFO_BOTTOM_POS_STOP_WICH_INT_POS,
    INFO_BOTTOM_POSITION_STOP,
    INFO_INTERMEDIATE_POSITION_STOP,
    INFO_MOVING_DOWN,
    INFO_MOVING_UP,
    INFO_NO_INFORMATION,
    INFO_OVERHEATED,
    INFO_START_TO_MOVE_DOWN,
    INFO_START_TO_MOVE_UP,
    INFO_STOPPED_IN_UNDEFINED_POSITION,
    INFO_SWITCHING_DEVICE_SWITCHED_OFF,
    INFO_SWITCHING_DEVICE_SWITCHED_ON,
    INFO_TILT_VENTILATION_POS_STOP,
    INFO_TIMEOUT,
    INFO_TOP_POS_STOP_WICH_TILT_POS,
    INFO_TOP_POSITION_STOP,
)

REQUIREMENTS = []
DEPENDENCIES = ["elero"]

_LOGGER = logging.getLogger(__name__)

ATTR_ELERO_STATE = "elero_state"

CONF_CHANNEL = "channel"
CONF_SUPPORTED_FEATURES = "supported_features"
CONF_TRAVEL_TIME = "travel_time"

ELERO_COVER_DEVICE_CLASSES = {
    "awning": "window",
    "interior shading": "window",
    "roller shutter": "window",
    "rolling door": "garage",
    "venetian blind": "window",
}

# Known Elero stop positions (0 = closed, 100 = open).
POSITION_CLOSED = 0
POSITION_OPEN = 100
POSITION_INTERMEDIATE = 75
POSITION_TILT_VENTILATION = 25

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
        vol.Optional(CONF_TRAVEL_TIME, default=50.0): vol.Coerce(float),
    }
)

PLATFORM_SCHEMA = COVER_PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_COVERS): vol.Schema({cv.slug: COVER_SCHEMA})}
)


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up the Elero cover platform."""
    covers = []
    covers_conf = config.get(CONF_COVERS, {})
    for _, cover_conf in covers_conf.items():
        transmitter = elero.ELERO_TRANSMITTERS.get_transmitter(
            cover_conf.get(CONF_TRANSMITTER_SERIAL_NUMBER)
        )
        if not transmitter:
            _LOGGER.error(
                "The transmitter '%s' of channel '%s' - '%s' is non-existent!",
                cover_conf.get(CONF_TRANSMITTER_SERIAL_NUMBER),
                cover_conf.get(CONF_CHANNEL),
                cover_conf.get(CONF_NAME),
            )
            continue

        covers.append(
            EleroCover(
                hass,
                transmitter,
                cover_conf.get(CONF_NAME),
                cover_conf.get(CONF_CHANNEL),
                cover_conf.get(CONF_DEVICE_CLASS),
                cover_conf.get(CONF_SUPPORTED_FEATURES),
                cover_conf.get(CONF_TRAVEL_TIME),
            )
        )

    add_devices(covers, True)


class EleroCover(CoverEntity, RestoreEntity):
    """Representation of an Elero cover device.

    Position tracking uses time-based interpolation: when the cover is moving,
    the current position is calculated from the start position, direction,
    elapsed time, and configured travel_time.  Known stop events from the radio
    (top / bottom / intermediate / tilt) override the estimate with the exact
    position.
    """

    # ── lifecycle ───────────────────────────────────────────────────────

    async def async_added_to_hass(self):
        """Restore state on HA startup."""
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if not state:
            return
        self._position = state.attributes.get("current_position")
        self._tilt_position = state.attributes.get("current_tilt_position")
        self._elero_state = state.attributes.get(ATTR_ELERO_STATE)
        if self._position is not None:
            self._closed = self._position == 0
        _LOGGER.debug("Restored state for %s: position=%s", self._name, self._position)

    def __init__(
        self, hass, transmitter, name, channel, device_class,
        supported_features, travel_time,
    ):
        """Initialize an Elero cover."""
        self.hass = hass
        self._transmitter = transmitter
        self._name = name
        self._channel = channel
        self._device_class = ELERO_COVER_DEVICE_CLASSES[device_class]

        self._supported_features = 0
        for f in supported_features:
            self._supported_features |= SUPPORTED_FEATURES[f]

        self._available = self._transmitter.set_channel(
            self._channel, self.response_handler
        )

        # Core state
        self._position = None       # 0 = closed, 100 = open, None = unknown
        self._tilt_position = None
        self._is_opening = False
        self._is_closing = False
        self._closed = None
        self._elero_state = None
        self._response = {}

        # Travel-time position tracking
        self._travel_time = travel_time      # seconds for a full 0↔100 travel
        self._move_start_time = None         # time.time() when movement began
        self._move_start_position = None     # position at movement start
        self._move_direction = 0             # +1 = opening, -1 = closing

        # Handle for a scheduled stop (used by set_cover_position)
        self._scheduled_stop = None

    # ── HA entity properties ────────────────────────────────────────────

    @property
    def unique_id(self):
        return f"{self._transmitter.get_serial_number()}_{self._channel}"

    @property
    def name(self):
        return self._name

    @property
    def device_class(self):
        return self._device_class

    @property
    def supported_features(self):
        return self._supported_features

    @property
    def should_poll(self):
        return True

    @property
    def available(self):
        return self._available

    @property
    def current_cover_position(self):
        """Return current position, interpolating in real-time while moving."""
        if self._move_start_time is not None and self._move_start_position is not None:
            elapsed = time.time() - self._move_start_time
            delta = (elapsed / self._travel_time) * 100.0 * self._move_direction
            return max(0, min(100, round(self._move_start_position + delta)))
        return self._position

    @property
    def current_cover_tilt_position(self):
        return self._tilt_position

    @property
    def is_opening(self):
        return self._is_opening

    @property
    def is_closing(self):
        return self._is_closing

    @property
    def is_closed(self):
        return self._closed

    # NOTE: we intentionally do NOT override the `state` property.
    # CoverEntity computes it from is_opening / is_closing / is_closed,
    # which keeps everything consistent.

    @property
    def extra_state_attributes(self):
        data = {}
        if self._elero_state is not None:
            data[ATTR_ELERO_STATE] = self._elero_state
        data["travel_time"] = self._travel_time
        data["move_start_position"] = self._move_start_position
        tx = self._transmitter
        data["last_command_ts"] = tx.last_command_ts
        data["last_response_ts"] = tx.last_response_ts
        data["error_count"] = tx.error_count
        data["timeout_count"] = tx.timeout_count
        data["reconnect_count"] = tx.reconnect_count
        data["checksum_error_count"] = tx.checksum_error_count
        data["consecutive_failures"] = tx.consecutive_failures
        return data

    # ── movement helpers ────────────────────────────────────────────────

    def _cancel_scheduled_stop(self):
        """Cancel any pending timed stop from set_cover_position."""
        if self._scheduled_stop is not None:
            self._scheduled_stop.cancel()
            self._scheduled_stop = None

    def _start_moving(self, direction):
        """Begin tracking a movement.  direction: +1 (open) or -1 (close).

        Captures the current interpolated position *before* resetting the
        tracking state so that back-to-back commands don't lose position info.
        """
        self._cancel_scheduled_stop()
        current = self.current_cover_position
        self._move_start_time = time.time()
        self._move_start_position = (
            current if current is not None
            else (0 if direction > 0 else 100)
        )
        self._move_direction = direction
        self._is_opening = direction > 0
        self._is_closing = direction < 0
        self._closed = False
        _LOGGER.debug(
            "%s: start moving %s from position %s",
            self._name,
            "up" if direction > 0 else "down",
            self._move_start_position,
        )

    def _stop_moving(self, final_position=None):
        """Finalise a movement.

        If *final_position* is given (from a known radio stop event) it is used
        directly.  Otherwise the position is calculated from elapsed time.
        """
        self._cancel_scheduled_stop()
        if final_position is not None:
            self._position = final_position
        elif self._move_start_time is not None:
            self._position = self.current_cover_position
        # Clear movement tracking
        self._move_start_time = None
        self._move_start_position = None
        self._move_direction = 0
        self._is_opening = False
        self._is_closing = False
        if self._position is not None:
            self._closed = self._position == 0
        _LOGGER.debug("%s: stopped at position %s", self._name, self._position)

    # ── cover commands ──────────────────────────────────────────────────

    def update(self):
        """Poll the device for its current state."""
        self._transmitter.info(self._channel)

    def open_cover(self, **kwargs):
        """Open the cover."""
        self._transmitter.up(self._channel)
        self._start_moving(+1)
        # Schedule an update after expected travel so we catch the stop event
        self.hass.loop.call_later(self._travel_time + 1, self.update)

    def close_cover(self, **kwargs):
        """Close the cover."""
        self._transmitter.down(self._channel)
        self._start_moving(-1)
        self.hass.loop.call_later(self._travel_time + 1, self.update)

    def stop_cover(self, **kwargs):
        """Stop the cover."""
        self._transmitter.stop(self._channel)
        self._stop_moving()

    def set_cover_position(self, **kwargs):
        """Move the cover to a specific position (time-based approximation).

        Calculates how long to run from the current estimated position to the
        target, starts the motor, and schedules a timed stop.  If the current
        position is unknown, a full open is performed first to calibrate.
        """
        target = kwargs.get(ATTR_POSITION)
        if target is None:
            return
        target = max(0, min(100, target))

        current = self.current_cover_position
        if current is None:
            _LOGGER.warning(
                "%s: position unknown — opening fully to calibrate", self._name
            )
            self.open_cover()
            self._scheduled_stop = self.hass.loop.call_later(
                self._travel_time + 2,
                lambda: self.set_cover_position(position=target),
            )
            return

        diff = target - current
        if abs(diff) < 2:
            return

        move_time = abs(diff) / 100.0 * self._travel_time

        if diff > 0:
            self._transmitter.up(self._channel)
            self._start_moving(+1)
        else:
            self._transmitter.down(self._channel)
            self._start_moving(-1)

        def _finish_move():
            _LOGGER.debug(
                "%s: timed move complete — stopping at target %s", self._name, target
            )
            self.hass.async_add_executor_job(self._execute_timed_stop, target)

        self._scheduled_stop = self.hass.loop.call_later(move_time, _finish_move)

    def _execute_timed_stop(self, target):
        """Send the stop command and set final position (runs in executor)."""
        self._transmitter.stop(self._channel)
        self._stop_moving(final_position=target)
        self._scheduled_stop = None

    def cover_ventilation_tilting_position(self, **kwargs):
        """Move into the ventilation/tilting position."""
        self._transmitter.ventilation_tilting(self._channel)
        self._cancel_scheduled_stop()

    def cover_intermediate_position(self, **kwargs):
        """Move into the intermediate position."""
        self._transmitter.intermediate(self._channel)
        self._cancel_scheduled_stop()

    def close_cover_tilt(self, **kwargs):
        self.cover_ventilation_tilting_position()

    def open_cover_tilt(self, **kwargs):
        self.cover_intermediate_position()

    def stop_cover_tilt(self, **kwargs):
        self.stop_cover()

    def set_cover_tilt_position(self, **kwargs):
        tilt_position = kwargs.get(ATTR_TILT_POSITION)
        if tilt_position is None:
            return
        if tilt_position < 50:
            self.cover_ventilation_tilting_position()
        else:
            self.cover_intermediate_position()

    # ── response handling ───────────────────────────────────────────────

    def response_handler(self, response):
        """Callback invoked by the transmitter with a device response."""
        self._response = response
        self._set_states()

    def _set_states(self):
        """Update cover state from the last device response."""
        status = self._response.get("status")
        if status is None:
            return

        self._elero_state = status
        _LOGGER.debug(
            "%s ch %s: status=%s", self._name, self._channel, status
        )

        # ── definite stop positions ─────────────────────────────────────

        if status == INFO_TOP_POSITION_STOP:
            self._stop_moving(final_position=POSITION_OPEN)
            self._tilt_position = None
            self._closed = False

        elif status == INFO_BOTTOM_POSITION_STOP:
            self._stop_moving(final_position=POSITION_CLOSED)
            self._tilt_position = None
            self._closed = True

        elif status == INFO_INTERMEDIATE_POSITION_STOP:
            self._stop_moving(final_position=POSITION_INTERMEDIATE)
            self._tilt_position = POSITION_INTERMEDIATE
            self._closed = False

        elif status == INFO_TILT_VENTILATION_POS_STOP:
            self._stop_moving(final_position=POSITION_TILT_VENTILATION)
            self._tilt_position = POSITION_TILT_VENTILATION
            self._closed = False

        elif status == INFO_TOP_POS_STOP_WICH_TILT_POS:
            self._stop_moving(final_position=POSITION_TILT_VENTILATION)
            self._tilt_position = POSITION_TILT_VENTILATION
            self._closed = False

        elif status == INFO_BOTTOM_POS_STOP_WICH_INT_POS:
            self._stop_moving(final_position=POSITION_INTERMEDIATE)
            self._tilt_position = POSITION_INTERMEDIATE
            self._closed = False

        # ── movement in progress ────────────────────────────────────────

        elif status in (INFO_START_TO_MOVE_UP, INFO_MOVING_UP):
            # If we already track this movement (we initiated it), keep going.
            # If not (physical remote, MultiTel2, etc.), start tracking now.
            if self._move_start_time is None:
                self._start_moving(+1)
            self._tilt_position = None

        elif status in (INFO_START_TO_MOVE_DOWN, INFO_MOVING_DOWN):
            if self._move_start_time is None:
                self._start_moving(-1)
            self._tilt_position = None

        # ── stopped at unknown position ─────────────────────────────────

        elif status == INFO_STOPPED_IN_UNDEFINED_POSITION:
            # Keep the calculated position from elapsed-time tracking.
            self._stop_moving()
            self._tilt_position = None

        # ── no information ──────────────────────────────────────────────

        elif status == INFO_NO_INFORMATION:
            self._stop_moving()
            self._position = None
            self._tilt_position = None
            self._closed = None

        # ── errors ──────────────────────────────────────────────────────

        elif status in (INFO_BLOCKING, INFO_OVERHEATED, INFO_TIMEOUT):
            self._stop_moving()
            self._position = None
            self._tilt_position = None
            self._closed = None
            _LOGGER.error(
                "Transmitter '%s' ch %s error: %s",
                self._transmitter.get_serial_number(),
                self._channel,
                status,
            )

        elif status in (
            INFO_SWITCHING_DEVICE_SWITCHED_ON,
            INFO_SWITCHING_DEVICE_SWITCHED_OFF,
        ):
            self._stop_moving()
            self._position = None
            self._tilt_position = None
            self._closed = None

        else:
            self._stop_moving()
            self._position = None
            self._tilt_position = None
            self._closed = None
            _LOGGER.error(
                "Transmitter '%s' ch %s unhandled response: %s",
                self._transmitter.get_serial_number(),
                self._channel,
                status,
            )
