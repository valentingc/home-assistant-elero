"""Constants for the Elero integration."""

from homeassistant.const import Platform

DOMAIN = "elero"

PLATFORMS = [Platform.COVER, Platform.BUTTON]

# ── Connection types ────────────────────────────────────────────────────
CONNECTION_LOCAL = "local"
CONNECTION_REMOTE = "remote"

# ── Config keys (transmitter / hub level) ───────────────────────────────
CONF_CONNECTION_TYPE = "connection_type"
CONF_BAUDRATE = "baudrate"
CONF_BYTESIZE = "bytesize"
CONF_PARITY = "parity"
CONF_STOPBITS = "stopbits"
CONF_TRANSMITTER_SERIAL_NUMBER = "serial_number"
CONF_TRANSMITTERS = "transmitters"
CONF_REMOTE_TRANSMITTERS = "remote_transmitters"
CONF_REMOTE_TRANSMITTERS_ADDRESS = "address"
CONF_PORT = "port"

# ── Config keys (cover / sub-entry level) ───────────────────────────────
CONF_CHANNEL = "channel"
CONF_SUPPORTED_FEATURES = "supported_features"
CONF_TRAVEL_TIME = "travel_time"  # legacy: single travel time for both directions
CONF_TRAVEL_TIME_UP = "travel_time_up"
CONF_TRAVEL_TIME_DOWN = "travel_time_down"
CONF_TILT_STEP = "tilt_step"  # legacy: % bump per ventilation press
CONF_TILT_TRAVEL_TIME = "tilt_travel_time"
CONF_TILT_BUTTONS = "tilt_buttons"
CONF_LEARN_TRAVEL_TIMES = "learn_travel_times"
CONF_VENTILATION = "ventilation"
CONF_INTERMEDIATE = "intermediate"
CONF_PRESET_MODE = "mode"
CONF_PRESET_POSITION = "position"
CONF_PRESET_DURATION = "duration"

# Config-flow form sections
SECTION_TIMING = "timing"

# What an Elero preset command (ventilation / intermediate) does on the drive.
PRESET_FIXED = "fixed"  # drives to a fixed programmed position
PRESET_STEP_UP = "step_up"  # drives up for a short time from wherever it is
PRESET_STEP_DOWN = "step_down"  # drives down for a short time
PRESET_MODES = [PRESET_FIXED, PRESET_STEP_UP, PRESET_STEP_DOWN]

# What HA's open/close tilt controls do.
TILT_BUTTONS_PRESETS = "presets"  # open tilt = intermediate, close tilt = ventilation
TILT_BUTTONS_SLATS = "slats"  # open/close tilt fully rotate the slats
TILT_BUTTONS_MODES = [TILT_BUTTONS_PRESETS, TILT_BUTTONS_SLATS]

# Sub-entry types
SUBENTRY_TYPE_COVER = "cover"

# ── Defaults ────────────────────────────────────────────────────────────
DEFAULT_BAUDRATE = 38400
DEFAULT_BYTESIZE = 8
DEFAULT_PARITY = "N"
DEFAULT_STOPBITS = 1
DEFAULT_TRAVEL_TIME = 50.0
DEFAULT_TILT_STEP = 2.0
DEFAULT_TILT_TRAVEL_TIME = 2.0
DEFAULT_VENTILATION_POSITION = 25
DEFAULT_INTERMEDIATE_POSITION = 75
DEFAULT_PRESET_DURATION = 1.0

DEFAULT_BRAND = "elero"
DEFAULT_PRODUCT = "Transmitter Stick"

# ── Wire-protocol constants ─────────────────────────────────────────────
BIT_8 = 8
BYTE_HEADER = 0xAA
BYTE_LENGTH_2 = 0x02
BYTE_LENGTH_4 = 0x04
BYTE_LENGTH_5 = 0x05
HEX_255 = 0xFF

COMMAND_CHECK = 0x4A
COMMAND_CHECH_TEXT = "Easy Check"
COMMAND_INFO = 0x4E
COMMAND_INFO_TEXT = "Info"
COMMAND_SEND = 0x4C

PAYLOAD_DOWN = 0x40
PAYLOAD_DOWN_TEXT = "Down"
PAYLOAD_INTERMEDIATE_POS = 0x44
PAYLOAD_INTERMEDIATE_POS_TEXT = "Intermediate position"
PAYLOAD_STOP = 0x10
PAYLOAD_STOP_TEXT = "Stop"
PAYLOAD_UP = 0x20
PAYLOAD_UP_TEXT = "Up"
PAYLOAD_VENTILATION_POS_TILTING = 0x24
PAYLOAD_VENTILATION_POS_TILTING_TEXT = "Tilt/ventilation"

RESPONSE_LENGTH_CHECK = 6
RESPONSE_LENGTH_INFO = 7
RESPONSE_LENGTH_SEND = 7

# ── Device response statuses ────────────────────────────────────────────
INFO_BLOCKING = "blocking"
INFO_BOTTOM_POSITION_STOP = "bottom position stop"
INFO_BOTTOM_POS_STOP_WICH_INT_POS = "bottom position stop wich is intermediate position"
INFO_INTERMEDIATE_POSITION_STOP = "intermediate position stop"
INFO_MOVING_DOWN = "moving down"
INFO_MOVING_UP = "moving up"
INFO_NO_INFORMATION = "no information"
INFO_OVERHEATED = "overheated"
INFO_START_TO_MOVE_DOWN = "start to move down"
INFO_START_TO_MOVE_UP = "start to move up"
INFO_STOPPED_IN_UNDEFINED_POSITION = "stopped in undefined position"
INFO_SWITCHING_DEVICE_SWITCHED_OFF = "switching device switched off"
INFO_SWITCHING_DEVICE_SWITCHED_ON = "switching device switched on"
INFO_TILT_VENTILATION_POS_STOP = "tilt ventilation position stop"
INFO_TIMEOUT = "timeout"
INFO_TOP_POSITION_STOP = "top position stop"
INFO_TOP_POS_STOP_WICH_TILT_POS = "top position stop wich is tilt position"
INFO_UNKNOWN = "unknown response"

INFO = {
    0x00: INFO_NO_INFORMATION,
    0x01: INFO_TOP_POSITION_STOP,
    0x02: INFO_BOTTOM_POSITION_STOP,
    0x03: INFO_INTERMEDIATE_POSITION_STOP,
    0x04: INFO_TILT_VENTILATION_POS_STOP,
    0x05: INFO_BLOCKING,
    0x06: INFO_OVERHEATED,
    0x07: INFO_TIMEOUT,
    0x08: INFO_START_TO_MOVE_UP,
    0x09: INFO_START_TO_MOVE_DOWN,
    0x0A: INFO_MOVING_UP,
    0x0B: INFO_MOVING_DOWN,
    0x0D: INFO_STOPPED_IN_UNDEFINED_POSITION,
    0x0E: INFO_TOP_POS_STOP_WICH_TILT_POS,
    0x0F: INFO_BOTTOM_POS_STOP_WICH_INT_POS,
    0x10: INFO_SWITCHING_DEVICE_SWITCHED_OFF,
    0x11: INFO_SWITCHING_DEVICE_SWITCHED_ON,
}

# ── Cover supported feature names (used in sub-entry config) ────────────
SUPPORTED_FEATURE_NAMES = [
    "up",
    "down",
    "stop",
    "set_position",
    "open_tilt",
    "close_tilt",
    "stop_tilt",
    "set_tilt_position",
]

# ── Cover device classes (config-time string → HA device_class) ─────────
ELERO_COVER_DEVICE_CLASSES = {
    "awning": "awning",
    "interior shading": "shade",
    "roller shutter": "shutter",
    "rolling door": "garage",
    "venetian blind": "blind",
}

# Features that involve the slats; the tilt phase is only modelled for
# covers that use at least one of them.
TILT_FEATURES = {"open_tilt", "close_tilt", "stop_tilt", "set_tilt_position"}

# ── Polling ─────────────────────────────────────────────────────────────
POLL_INTERVAL_IDLE = 30.0  # seconds between status polls while stopped
POLL_INTERVAL_MOVING = 2.0  # while a move with an unknown end is tracked
POLL_INTERVAL_ENDGAME = 1.0  # near the expected end of a full run
