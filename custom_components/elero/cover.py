"""Support for Elero cover components."""

__version__ = "3.4.0"

import logging

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.cover import (ATTR_POSITION, ATTR_TILT_POSITION,
                                            CoverEntity,
                                            CoverEntityFeature)
from homeassistant.components.light import PLATFORM_SCHEMA
from homeassistant.const import (CONF_COVERS, CONF_DEVICE_CLASS, CONF_NAME,
                                 STATE_CLOSED, STATE_CLOSING, STATE_OPEN,
                                 STATE_OPENING, STATE_UNKNOWN)
import time
import custom_components.elero as elero
from custom_components.elero import (CONF_TRANSMITTER_SERIAL_NUMBER,
                                     INFO_BLOCKING,
                                     INFO_BOTTOM_POS_STOP_WICH_INT_POS,
                                     INFO_BOTTOM_POSITION_STOP,
                                     INFO_INTERMEDIATE_POSITION_STOP,
                                     INFO_MOVING_DOWN, INFO_MOVING_UP,
                                     INFO_NO_INFORMATION, INFO_OVERHEATED,
                                     INFO_START_TO_MOVE_DOWN,
                                     INFO_START_TO_MOVE_UP,
                                     INFO_STOPPED_IN_UNDEFINED_POSITION,
                                     INFO_SWITCHING_DEVICE_SWITCHED_OFF,
                                     INFO_SWITCHING_DEVICE_SWITCHED_ON,
                                     INFO_TILT_VENTILATION_POS_STOP,
                                     INFO_TIMEOUT,
                                     INFO_TOP_POS_STOP_WICH_TILT_POS,
                                     INFO_TOP_POSITION_STOP)

# Python libraries/modules that you would normally install for your component.
REQUIREMENTS = []

# Other HASS components that should be setup before the platform is loaded.
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

# Position slider values.
POSITION_CLOSED = 0
POSITION_INTERMEDIATE = 75
POSITION_OPEN = 100
POSITION_TILT_VENTILATION = 25
POSITION_UNDEFINED = 50

# Elero states.
STATE_INTERMEDIATE = "intermediate"
STATE_STOPPED = "stopped"
STATE_TILT_VENTILATION = "ventilation/tilt"
STATE_UNDEFINED = "undefined"

# Supported features.
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

# It is needed because of the transmitter has a channel handling bug.
CHANNEL_NUMBERS_SCHEMA = vol.All(vol.Coerce(int), vol.Range(min=1, max=15))

# Validation of the user's configuration.
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

# Validation of the user's configuration.
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_COVERS): vol.Schema({cv.slug: COVER_SCHEMA}), }
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
            t = cover_conf.get(CONF_TRANSMITTER_SERIAL_NUMBER)
            ch = cover_conf.get(CONF_CHANNEL)
            n = cover_conf.get(CONF_NAME)
            _LOGGER.error(
                f"The transmitter '{t}' of the '{ch}' - '{n}' channel is "
                "non-existent transmitter!"
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


class EleroCover(CoverEntity):
    """Representation of a Elero cover device."""

    def __init__(
        self, hass, transmitter, name, channel, device_class, supported_features, travel_time
    ):
        """Init of a Elero cover."""
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
        self._position = None
        self._is_opening = None
        self._is_closing = None
        self._closed = None
        self._tilt_position = None
        self._state = None
        self._elero_state = None
        self._response = dict()
        self._travel_time = travel_time
        self._last_known_position = None
        self._start_time = None

    @property
    def unique_id(self):
        """
        Gets the unique ID of the cover.
        """
        ser_num = self._transmitter.get_serial_number()
        ch = self._channel
        return f"{ser_num}_{ch}"

    @property
    def name(self):
        """Return the name of the cover."""
        return self._name

    @property
    def device_class(self):
        """Return the class of this device, from component DEVICE_CLASSES."""
        return self._device_class

    @property
    def supported_features(self):
        """Flag supported features."""
        return self._supported_features

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state.

        Because of you can use other remote control (like MultiTel2)
        next to the HA in your system and the status of the Elero devices
        may change therefore it is necessary to monitor their statuses.
        """
        return True

    @property
    def available(self):
        """Return True if entity is available."""
        return self._available

    @property
    def current_cover_position(self):
        """Return the current position of the cover.

        None is unknown, 0 is closed, 100 is fully open.
        """
        return self._position

    @property
    def current_cover_tilt_position(self):
        """Return the current tilt position of the cover."""
        return self._tilt_position

    @property
    def is_opening(self):
        """Return if the cover is opening or not."""
        return self._is_opening

    @property
    def is_closing(self):
        """Return if the cover is closing or not."""
        return self._is_closing

    @property
    def is_closed(self):
        """Return if the cover is closed."""
        return self._closed

    @property
    def state(self):
        """Return the state of the cover."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return device specific state attributes."""
        data = {}

        elero_state = self._elero_state
        if elero_state is not None:
            data[ATTR_ELERO_STATE] = self._elero_state

        data["travel_time"] = self._travel_time
        data["last_known_position"] = self._last_known_position

        return data

    def update(self):
        """Get the device sate and update its attributes and state."""
        self._transmitter.info(self._channel)

    def close_cover(self, **kwargs):
        """Close the cover."""
        self._transmitter.down(self._channel)
        self._closed = False
        self._is_closing = True
        self._is_opening = False
        self._state = STATE_CLOSING
        self._position = POSITION_CLOSED
        self._tilt_position = POSITION_UNDEFINED
        self._start_time = time.time()

    def open_cover(self, **kwargs):
        """Open the cover."""
        self._transmitter.up(self._channel)
        self._closed = False
        self._is_closing = False
        self._is_opening = True
        self._state = STATE_OPENING
        self._position = POSITION_OPEN
        self._tilt_position = POSITION_UNDEFINED
        self._start_time = time.time()

    def stop_cover(self, **kwargs):
        """Stop the cover."""
        self._transmitter.stop(self._channel)

        elapsed_time = time.time() - self._start_time if self._start_time else 0
        self._start_time = None

        delta_position = (elapsed_time / self._travel_time) * 100
        if self._is_opening:
            self._position += delta_position
        elif self._is_closing:
            self._position -= delta_position 
        self._position = max(0, min(100, self._position))
        self._last_known_position = self._position

        self._closed = False
        self._is_closing = False
        self._is_opening = False
        self._state = STATE_STOPPED
        self._tilt_position = POSITION_UNDEFINED

    def set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        position = kwargs.get(ATTR_POSITION)

        # Validate position input
        if position is None or not (0 <= position <= 100):
            _LOGGER.error("Invalid position: must be between 0 and 100")
            return

        # Validate current position availability
        if self._last_known_position is None:
            _LOGGER.error("Cannot set position because the last known position is unavailable.")
            return

        target_position = position
        current_position = self._last_known_position

        _LOGGER.debug(f"set_cover_position called with target position: {position}")
        _LOGGER.debug(f"current_position: {current_position}, travel_time: {self._travel_time}")

        if target_position == current_position:
            _LOGGER.info("Target position is the same as the current position. No action needed.")
            return

        # Determine direction
        move_time = abs(target_position - current_position) / 100 * self._travel_time
        _LOGGER.debug(f"calculated move_time: {move_time}s")
        if target_position > current_position:
            self.open_cover()  # Move up
            self._state = STATE_OPENING
        else:
            self.close_cover()  # Move down
            self._state = STATE_CLOSING

        # Schedule to stop the cover after the calculated travel time.
        def stop_cover_after_travel_time():
            _LOGGER.debug(f"Stopping cover after {move_time}s, final position: {target_position}")
            """Stop the cover after moving for the calculated travel time."""
            self.stop_cover()
            self._position = target_position
            self._last_known_position = target_position

            # Update the state based on the final position
            if target_position == 100:
                self._state = STATE_OPEN
            elif target_position == 0:
                self._state = STATE_CLOSED
            else:
                self._state = STATE_UNKNOWN

            self._is_opening = False
            self._is_closing = False

        self.hass.loop.call_later(move_time, stop_cover_after_travel_time)

    def cover_ventilation_tilting_position(self, **kwargs):
        """Move into the ventilation/tilting position."""
        self._transmitter.ventilation_tilting(self._channel)
        self._closed = False
        self._is_closing = False
        self._is_opening = False
        self._state = STATE_TILT_VENTILATION
        self._position = POSITION_TILT_VENTILATION
        self._tilt_position = POSITION_TILT_VENTILATION

    def cover_intermediate_position(self, **kwargs):
        """Move into the intermediate position."""
        self._transmitter.intermediate(self._channel)
        self._closed = False
        self._is_closing = False
        self._is_opening = False
        self._state = STATE_INTERMEDIATE
        self._position = POSITION_INTERMEDIATE
        self._tilt_position = POSITION_INTERMEDIATE

    def close_cover_tilt(self, **kwargs):
        """Close the cover tilt."""
        self.cover_ventilation_tilting_position()

    def open_cover_tilt(self, **kwargs):
        """Open the cover tilt."""
        self.cover_intermediate_position()

    def stop_cover_tilt(self, **kwargs):
        """Stop the cover tilt."""
        self.stop_cover()

    def set_cover_tilt_position(self, **kwargs):
        """Move the cover tilt to a specific position."""
        tilt_position = kwargs.get(ATTR_TILT_POSITION)
        if tilt_position < 50:
            self.cover_ventilation_tilting_position()
        elif tilt_position > 50:
            self.cover_intermediate_position()
        else:
            _LOGGER.error(f"Wrong Tilt Position slider data: {tilt_position}")

    def response_handler(self, response):
        """Handle callback to the response from the Transmitter."""
        self._response = response
        self.set_states()

    def set_states(self):
        """Set the state of the cover."""
        self._elero_state = self._response["status"]
        if self._response["status"] == INFO_NO_INFORMATION:
            self._closed = None
            self._is_closing = None
            self._is_opening = None
            self._state = STATE_UNKNOWN
            self._position = None
            self._tilt_position = None
        elif self._response["status"] == INFO_TOP_POSITION_STOP:
            self._closed = False
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_OPEN
            self._position = POSITION_OPEN
            self._tilt_position = POSITION_UNDEFINED
        elif self._response["status"] == INFO_BOTTOM_POSITION_STOP:
            self._closed = True
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_CLOSED
            self._position = POSITION_CLOSED
            self._tilt_position = POSITION_UNDEFINED
        elif self._response["status"] == INFO_INTERMEDIATE_POSITION_STOP:
            self._closed = False
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_INTERMEDIATE
            self._position = POSITION_INTERMEDIATE
            self._tilt_position = POSITION_INTERMEDIATE
        elif self._response["status"] == INFO_TILT_VENTILATION_POS_STOP:
            self._closed = False
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_TILT_VENTILATION
            self._position = POSITION_TILT_VENTILATION
            self._tilt_position = POSITION_TILT_VENTILATION
        elif self._response["status"] == INFO_START_TO_MOVE_UP:
            self._closed = False
            self._is_closing = False
            self._is_opening = True
            self._state = STATE_OPENING
            self._position = POSITION_UNDEFINED
            self._tilt_position = POSITION_UNDEFINED
        elif self._response["status"] == INFO_START_TO_MOVE_DOWN:
            self._closed = False
            self._is_closing = True
            self._is_opening = False
            self._state = STATE_CLOSING
            self._position = POSITION_UNDEFINED
            self._tilt_position = POSITION_UNDEFINED
        elif self._response["status"] == INFO_MOVING_UP:
            self._closed = False
            self._is_closing = False
            self._is_opening = True
            self._state = STATE_OPENING
            self._position = POSITION_UNDEFINED
            self._tilt_position = POSITION_UNDEFINED
        elif self._response["status"] == INFO_MOVING_DOWN:
            self._closed = False
            self._is_closing = True
            self._is_opening = False
            self._state = STATE_CLOSING
            self._position = POSITION_UNDEFINED
            self._tilt_position = POSITION_UNDEFINED
        elif self._response["status"] == INFO_STOPPED_IN_UNDEFINED_POSITION:
            self._closed = False
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_UNDEFINED
            self._position = POSITION_UNDEFINED
            self._tilt_position = POSITION_UNDEFINED
        elif self._response["status"] == INFO_TOP_POS_STOP_WICH_TILT_POS:
            self._closed = False
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_TILT_VENTILATION
            self._position = POSITION_TILT_VENTILATION
            self._tilt_position = POSITION_TILT_VENTILATION
        elif self._response["status"] == INFO_BOTTOM_POS_STOP_WICH_INT_POS:
            self._closed = True
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_INTERMEDIATE
            self._position = POSITION_INTERMEDIATE
            self._tilt_position = POSITION_INTERMEDIATE
        elif self._response["status"] in (INFO_BLOCKING, INFO_OVERHEATED, INFO_TIMEOUT):
            self._closed = None
            self._is_closing = None
            self._is_opening = None
            self._state = STATE_UNKNOWN
            self._position = None
            self._tilt_position = None
            t = self._transmitter.get_serial_number()
            r = self._response["status"]
            _LOGGER.error(
                f"Transmitter: '{t}' ch: '{self._channel}'  error response: '{r}'."
            )

        elif self._response["status"] in (
            INFO_SWITCHING_DEVICE_SWITCHED_ON,
            INFO_SWITCHING_DEVICE_SWITCHED_OFF,
        ):
            self._closed = None
            self._is_closing = None
            self._is_opening = None
            self._state = STATE_UNKNOWN
            self._position = None
            self._tilt_position = None
        else:
            self._closed = None
            self._is_closing = None
            self._is_opening = None
            self._state = STATE_UNKNOWN
            self._position = None
            self._tilt_position = None
            t = self._transmitter.get_serial_number()
            r = self._response["status"]
            _LOGGER.error(
                f"Transmitter: '{t}' ch: '{self._channel}' "
                f"unhandled response: '{r}'."
            )
        self._last_known_position = self._position
