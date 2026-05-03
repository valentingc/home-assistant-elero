"""Support for Elero electrical drives."""

from __future__ import annotations

__version__ = "4.1.0"

import logging
import os
import threading
import time
from datetime import timedelta

import homeassistant.helpers.config_validation as cv
import serial
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigSubentry, SOURCE_IMPORT
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_NAME,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from serial.tools import list_ports

from .const import (
    BIT_8,
    BYTE_HEADER,
    BYTE_LENGTH_2,
    BYTE_LENGTH_4,
    BYTE_LENGTH_5,
    COMMAND_CHECH_TEXT,
    COMMAND_CHECK,
    COMMAND_INFO,
    COMMAND_INFO_TEXT,
    COMMAND_SEND,
    CONF_BAUDRATE,
    CONF_BYTESIZE,
    CONF_CHANNEL,
    CONF_CONNECTION_TYPE,
    CONF_PARITY,
    CONF_REMOTE_TRANSMITTERS,
    CONF_REMOTE_TRANSMITTERS_ADDRESS,
    CONF_STOPBITS,
    CONF_SUPPORTED_FEATURES,
    CONF_TILT_STEP,
    CONF_TILT_TRAVEL_TIME,
    CONF_TRANSMITTERS,
    CONF_TRANSMITTER_SERIAL_NUMBER,
    CONF_TRAVEL_TIME,
    CONNECTION_LOCAL,
    CONNECTION_REMOTE,
    DEFAULT_BAUDRATE,
    DEFAULT_BRAND,
    DEFAULT_BYTESIZE,
    DEFAULT_PARITY,
    DEFAULT_PRODUCT,
    DEFAULT_STOPBITS,
    DEFAULT_TILT_STEP,
    DEFAULT_TILT_TRAVEL_TIME,
    DEFAULT_TRAVEL_TIME,
    DOMAIN,
    HEX_255,
    INFO,
    INFO_UNKNOWN,
    PAYLOAD_DOWN,
    PAYLOAD_DOWN_TEXT,
    PAYLOAD_INTERMEDIATE_POS,
    PAYLOAD_INTERMEDIATE_POS_TEXT,
    PAYLOAD_STOP,
    PAYLOAD_STOP_TEXT,
    PAYLOAD_UP,
    PAYLOAD_UP_TEXT,
    PAYLOAD_VENTILATION_POS_TILTING,
    PAYLOAD_VENTILATION_POS_TILTING_TEXT,
    PLATFORMS,
    RESPONSE_LENGTH_CHECK,
    RESPONSE_LENGTH_INFO,
    RESPONSE_LENGTH_SEND,
    SUBENTRY_TYPE_COVER,
)

# Re-export status constants so existing imports `from custom_components.elero import INFO_*` keep working.
from .const import (  # noqa: F401
    INFO_BLOCKING,
    INFO_BOTTOM_POSITION_STOP,
    INFO_BOTTOM_POS_STOP_WICH_INT_POS,
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
    INFO_TOP_POSITION_STOP,
    INFO_TOP_POS_STOP_WICH_TILT_POS,
)

REQUIREMENTS = ["pyserial>=3.4"]
DEPENDENCIES: list[str] = []

_LOGGER = logging.getLogger(__name__)

# Legacy module-level singleton used by the old YAML cover.py path.
# Modern config-entry path stores transmitters in hass.data[DOMAIN][entry_id].
ELERO_TRANSMITTERS: "EleroTransmitters | None" = None


# ── YAML schema (kept for legacy import bridge) ─────────────────────────

ELERO_TRANSMITTER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_TRANSMITTER_SERIAL_NUMBER): str,
        vol.Optional(CONF_BAUDRATE, default=DEFAULT_BAUDRATE): cv.positive_int,
        vol.Optional(CONF_BYTESIZE, default=DEFAULT_BYTESIZE): cv.positive_int,
        vol.Optional(CONF_PARITY, default=DEFAULT_PARITY): str,
        vol.Optional(CONF_STOPBITS, default=DEFAULT_STOPBITS): cv.positive_int,
    }
)

ELERO_REMOTE_TRANSMITTER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRANSMITTER_SERIAL_NUMBER): str,
        vol.Required(CONF_REMOTE_TRANSMITTERS_ADDRESS): str,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_TRANSMITTERS): [ELERO_TRANSMITTER_SCHEMA],
                vol.Optional(CONF_REMOTE_TRANSMITTERS): [
                    ELERO_REMOTE_TRANSMITTER_SCHEMA
                ],
            }
        ),
    },
    extra=vol.ALLOW_EXTRA,
)


# ── HA setup hooks ──────────────────────────────────────────────────────


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Bridge legacy YAML config into a config entry on first start."""
    hass.data.setdefault(DOMAIN, {})

    # Stash any legacy `cover: - platform: elero` YAML so that
    # async_setup_entry can auto-import each cover as a sub-entry of the
    # matching transmitter. Keyed by serial_number.
    yaml_covers: dict[str, list[dict]] = {}
    for platform_conf in config.get("cover") or []:
        if not isinstance(platform_conf, dict):
            continue
        if platform_conf.get("platform") != DOMAIN:
            continue
        for _, cov in (platform_conf.get("covers") or {}).items():
            serial = str(cov.get(CONF_TRANSMITTER_SERIAL_NUMBER, "")).strip()
            if not serial:
                continue
            yaml_covers.setdefault(serial, []).append(dict(cov))
    hass.data[DOMAIN]["_yaml_covers"] = yaml_covers

    elero_yaml = config.get(DOMAIN)
    if not elero_yaml:
        return True

    # Local USB transmitters
    for tx in elero_yaml.get(CONF_TRANSMITTERS) or []:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={
                    CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
                    CONF_TRANSMITTER_SERIAL_NUMBER: tx.get(
                        CONF_TRANSMITTER_SERIAL_NUMBER
                    ),
                    CONF_BAUDRATE: tx.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
                    CONF_BYTESIZE: tx.get(CONF_BYTESIZE, DEFAULT_BYTESIZE),
                    CONF_PARITY: tx.get(CONF_PARITY, DEFAULT_PARITY),
                    CONF_STOPBITS: tx.get(CONF_STOPBITS, DEFAULT_STOPBITS),
                },
            )
        )

    # Remote (ser2net) transmitters
    for tx in elero_yaml.get(CONF_REMOTE_TRANSMITTERS) or []:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={
                    CONF_CONNECTION_TYPE: CONNECTION_REMOTE,
                    CONF_TRANSMITTER_SERIAL_NUMBER: tx[
                        CONF_TRANSMITTER_SERIAL_NUMBER
                    ],
                    CONF_REMOTE_TRANSMITTERS_ADDRESS: tx[
                        CONF_REMOTE_TRANSMITTERS_ADDRESS
                    ],
                },
            )
        )

    async_create_issue(
        hass,
        DOMAIN,
        "deprecated_yaml",
        is_fixable=False,
        severity=IssueSeverity.WARNING,
        translation_key="deprecated_yaml",
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Elero from a config entry."""
    global ELERO_TRANSMITTERS

    hass.data.setdefault(DOMAIN, {})

    serial_number = entry.data.get(CONF_TRANSMITTER_SERIAL_NUMBER)
    connection_type = entry.data.get(CONF_CONNECTION_TYPE, CONNECTION_LOCAL)

    transmitter: EleroTransmitter | None = None

    def _build_transmitter() -> EleroTransmitter | None:
        if connection_type == CONNECTION_REMOTE:
            address = entry.data[CONF_REMOTE_TRANSMITTERS_ADDRESS]
            tx = EleroRemoteTransmitter(serial_number, address)
            tx.init_serial()
            return tx if tx.get_transmitter_state() else None

        # local USB — discover the matching stick by serial number
        for cp in list_ports.comports():
            is_elero = (
                cp.manufacturer
                and DEFAULT_BRAND in cp.manufacturer
                and cp.product
                and DEFAULT_PRODUCT in cp.product
                and cp.serial_number
            )
            preset = (
                os.environ.get("ELERO_DEVICE") == cp.device
                and os.environ.get("ELERO_SERIAL_NUMBER")
            )
            if not (is_elero or preset):
                continue
            stick_serial = (
                os.environ["ELERO_SERIAL_NUMBER"] if preset else cp.serial_number
            )
            if serial_number and stick_serial != serial_number:
                continue
            tx = EleroTransmitter(
                cp.device,
                stick_serial,
                entry.data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
                entry.data.get(CONF_BYTESIZE, DEFAULT_BYTESIZE),
                entry.data.get(CONF_PARITY, DEFAULT_PARITY),
                entry.data.get(CONF_STOPBITS, DEFAULT_STOPBITS),
            )
            tx.init_serial()
            return tx if tx.get_transmitter_state() else None
        return None

    transmitter = await hass.async_add_executor_job(_build_transmitter)
    if transmitter is None:
        raise ConfigEntryNotReady(
            f"Could not connect to Elero transmitter '{serial_number}' ({connection_type})"
        )

    hass.data[DOMAIN][entry.entry_id] = transmitter

    # Register the transmitter stick as a HA "hub" device so individual
    # covers can attach to it via their `via_device`.
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, transmitter.get_serial_number())},
        manufacturer="Elero",
        model="Transmitter Stick",
        name=f"Elero {transmitter.get_serial_number()}",
    )

    # Maintain the legacy module-level singleton for any YAML cover configs
    # that still resolve transmitters by serial_number.
    if ELERO_TRANSMITTERS is None:
        ELERO_TRANSMITTERS = EleroTransmitters(None)
    ELERO_TRANSMITTERS.transmitters[transmitter.get_serial_number()] = transmitter

    @callback
    def _watchdog(_now):
        if transmitter.last_response_ts is None:
            return
        idle = time.time() - transmitter.last_response_ts
        if idle > 300:
            _LOGGER.debug(
                "Watchdog sending Easy Check to '%s' after %.1fs idle",
                transmitter.get_serial_number(),
                idle,
            )
            hass.async_add_executor_job(transmitter.check)

    entry.async_on_unload(
        async_track_time_interval(hass, _watchdog, timedelta(minutes=2))
    )

    async def _on_stop(_event):
        await hass.async_add_executor_job(transmitter.close_serial)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
    )

    # Auto-import legacy YAML covers as sub-entries (idempotent: keyed by channel).
    _auto_import_yaml_covers(hass, entry, transmitter.get_serial_number())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _auto_import_yaml_covers(
    hass: HomeAssistant, entry: ConfigEntry, serial_number: str
) -> None:
    """Migrate `cover: - platform: elero` YAML to ConfigSubentries.

    Idempotent: a cover whose channel already has a sub-entry is skipped.
    Safe to run on every start while the YAML config still exists; once the
    user removes the YAML the function becomes a no-op.
    """
    yaml_covers = hass.data.get(DOMAIN, {}).get("_yaml_covers", {}).get(
        serial_number, []
    )
    if not yaml_covers:
        return

    existing_channels = {
        int(sub.data.get(CONF_CHANNEL))
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_COVER
        and sub.data.get(CONF_CHANNEL) is not None
    }

    for cov in yaml_covers:
        try:
            channel = int(cov[CONF_CHANNEL])
        except (KeyError, TypeError, ValueError):
            continue
        if channel in existing_channels:
            continue
        try:
            sub_data = {
                CONF_NAME: cov[CONF_NAME],
                CONF_CHANNEL: channel,
                CONF_DEVICE_CLASS: cov[CONF_DEVICE_CLASS],
                CONF_SUPPORTED_FEATURES: list(cov[CONF_SUPPORTED_FEATURES]),
                CONF_TRAVEL_TIME: float(
                    cov.get(CONF_TRAVEL_TIME, DEFAULT_TRAVEL_TIME)
                ),
                CONF_TILT_STEP: float(cov.get(CONF_TILT_STEP, DEFAULT_TILT_STEP)),
                CONF_TILT_TRAVEL_TIME: float(
                    cov.get(CONF_TILT_TRAVEL_TIME, DEFAULT_TILT_TRAVEL_TIME)
                ),
            }
        except KeyError as exc:
            _LOGGER.warning(
                "Skipping YAML cover auto-import — missing field %s in %s",
                exc,
                cov,
            )
            continue

        subentry = ConfigSubentry(
            data=sub_data,
            subentry_type=SUBENTRY_TYPE_COVER,
            title=sub_data[CONF_NAME],
            unique_id=f"{serial_number}_{channel}",
        )
        try:
            hass.config_entries.async_add_subentry(entry, subentry)
            existing_channels.add(channel)
            _LOGGER.info(
                "Auto-imported YAML cover '%s' (ch %s) as sub-entry of %s",
                sub_data[CONF_NAME],
                channel,
                serial_number,
            )
        except Exception as exc:
            _LOGGER.error(
                "Failed to import YAML cover '%s' (ch %s): %s",
                sub_data.get(CONF_NAME, "?"),
                channel,
                exc,
            )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Elero config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    transmitter = hass.data[DOMAIN].pop(entry.entry_id, None)
    if transmitter:
        await hass.async_add_executor_job(transmitter.close_serial)
        if ELERO_TRANSMITTERS:
            ELERO_TRANSMITTERS.transmitters.pop(
                transmitter.get_serial_number(), None
            )
    return unload_ok


def _legacy_lookup_transmitter(hass: HomeAssistant, serial_number: str | None):
    """Find an active transmitter by serial number (used by legacy YAML covers).

    Looks at all currently set-up config entries and returns the matching
    transmitter, falling back to the first one if no serial is given.
    """
    bucket = hass.data.get(DOMAIN, {})
    transmitters = [v for v in bucket.values() if isinstance(v, EleroTransmitter)]
    if not transmitters:
        _LOGGER.error("No Elero transmitter is set up yet")
        return None
    if not serial_number:
        return transmitters[0]
    for tx in transmitters:
        if tx.get_serial_number() == serial_number:
            return tx
    _LOGGER.error("No Elero transmitter found for serial '%s'", serial_number)
    return None


# ── Transmitter classes (unchanged behaviour) ───────────────────────────


class EleroTransmitters:
    """Container for the Elero Centero USB Transmitter Sticks (legacy)."""

    def __init__(self, config):
        self.config = config
        self.transmitters: dict[str, "EleroTransmitter"] = {}
        _LOGGER.info("Elero lib version: %s", __version__)

    def get_transmitter(self, serial_number):
        if serial_number in self.transmitters:
            return self.transmitters[serial_number]
        _LOGGER.error("The transmitter '%s' doesn't exist!", serial_number)
        return None

    def close_transmitters(self):
        for _, t in self.transmitters.items():
            t.close_serial()


class EleroTransmitter:
    """Representation of an Elero Centero USB Transmitter Stick."""

    def __init__(
        self, serial_device, serial_number, baudrate, bytesize, parity, stopbits
    ):
        self._port = serial_device
        self._serial_number = serial_number
        self._baudrate = baudrate
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits
        self._threading_lock = threading.Lock()
        self._serial = None
        self._learned_channels: dict = {}
        # Diagnostics
        self.last_command_ts = None
        self.last_response_ts = None
        self.error_count = 0
        self.timeout_count = 0
        self.reconnect_count = 0
        self.checksum_error_count = 0
        self.consecutive_failures = 0

    def init_serial(self):
        self.init_serial_port()
        if self._serial:
            self.check()

    def init_serial_port(self):
        try:
            self._serial = serial.Serial(
                self._port,
                self._baudrate,
                self._bytesize,
                self._parity,
                self._stopbits,
                timeout=2,
                write_timeout=2,
            )
        except serial.serialutil.SerialException as exc:
            _LOGGER.exception(
                "Unable to open serial port for '%s' to the Transmitter Stick: '%s'",
                self._serial_number,
                exc,
            )

    def log_out_serial_port_details(self):
        _LOGGER.debug(
            "Transmitter stick on port '%s' serial: '%s'",
            self._port,
            self._serial_number,
        )

    def close_serial(self):
        acquired = self._threading_lock.acquire(timeout=5)
        if not acquired:
            _LOGGER.error("Failed to acquire lock to close serial connection.")
            return
        try:
            if self._serial and self._serial.is_open:
                self._serial.close()
        except Exception as exc:
            _LOGGER.exception("Problem closing serial connection: '%s'", exc)
        finally:
            self._threading_lock.release()

    def get_transmitter_state(self):
        return bool(self._serial)

    def get_serial_number(self):
        return self._serial_number

    def get_learned_channels(self):
        return tuple(sorted(self._learned_channels.keys()))

    # ── command helpers ────────────────────────────────────────────────

    def __get_check_command(self):
        return [BYTE_HEADER, BYTE_LENGTH_2, COMMAND_CHECK]

    def check(self):
        self.__process_command(
            COMMAND_CHECH_TEXT, self.__get_check_command(), 0, RESPONSE_LENGTH_CHECK
        )

    def _set_learned_channels(self, resp):
        self._learned_channels = dict.fromkeys(resp["chs"])
        chs = " ".join(map(str, list(self._learned_channels.keys())))
        _LOGGER.debug(
            "The taught channels on the '%s' transmitter are '%s'.",
            self._serial_number,
            chs,
        )

    def set_channel(self, channel, obj):
        if channel in self._learned_channels:
            self._learned_channels[channel] = obj
            return True
        _LOGGER.error(
            "The '%s' channel is not taught to the '%s' transmitter.",
            channel,
            self._serial_number,
        )
        return False

    def __get_info_command(self, channel):
        return [
            BYTE_HEADER,
            BYTE_LENGTH_4,
            COMMAND_INFO,
            self.__set_upper_channel_bits(channel),
            self.__set_lower_channel_bits(channel),
        ]

    def info(self, channel):
        self.__process_command(
            COMMAND_INFO_TEXT,
            self.__get_info_command(channel),
            channel,
            RESPONSE_LENGTH_INFO,
        )

    def __get_send_command(self, channel, payload):
        return [
            BYTE_HEADER,
            BYTE_LENGTH_5,
            COMMAND_SEND,
            self.__set_upper_channel_bits(channel),
            self.__set_lower_channel_bits(channel),
            payload,
        ]

    def up(self, channel):
        self.__process_command(
            PAYLOAD_UP_TEXT,
            self.__get_send_command(channel, PAYLOAD_UP),
            channel,
            RESPONSE_LENGTH_SEND,
        )

    def down(self, channel):
        self.__process_command(
            PAYLOAD_DOWN_TEXT,
            self.__get_send_command(channel, PAYLOAD_DOWN),
            channel,
            RESPONSE_LENGTH_SEND,
        )

    def stop(self, channel):
        self.__process_command(
            PAYLOAD_STOP_TEXT,
            self.__get_send_command(channel, PAYLOAD_STOP),
            channel,
            RESPONSE_LENGTH_SEND,
        )

    def intermediate(self, channel):
        self.__process_command(
            PAYLOAD_INTERMEDIATE_POS_TEXT,
            self.__get_send_command(channel, PAYLOAD_INTERMEDIATE_POS),
            channel,
            RESPONSE_LENGTH_SEND,
        )

    def ventilation_tilting(self, channel):
        self.__process_command(
            PAYLOAD_VENTILATION_POS_TILTING_TEXT,
            self.__get_send_command(channel, PAYLOAD_VENTILATION_POS_TILTING),
            channel,
            RESPONSE_LENGTH_SEND,
        )

    # ── low-level I/O ──────────────────────────────────────────────────

    def __process_command(self, command_text, int_list, channel, resp_length):
        int_list.append(self.__calculate_checksum(*int_list))
        bytes_data = self.__create_serial_data(int_list)

        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            ser_resp = b""
            try:
                _LOGGER.debug(
                    "Trying to send '%s' command (attempt %d/%d)",
                    command_text,
                    attempt,
                    max_attempts,
                )
                acquired = self._threading_lock.acquire(timeout=5)
                if not acquired:
                    _LOGGER.error(
                        "Timeout acquiring lock for '%s' (attempt %d)",
                        command_text,
                        attempt,
                    )
                    continue
                try:
                    self.last_command_ts = time.time()
                    if not self._serial or not self._serial.is_open:
                        self.init_serial_port()
                        if not self._serial:
                            raise serial.serialutil.SerialException(
                                "Serial port not initialised"
                            )
                    try:
                        self._serial.timeout = 2
                        self._serial.write_timeout = 2
                    except Exception:
                        pass

                    self._serial.write(bytes_data)
                    ser_resp = self._read_exact(resp_length, overall_timeout=2.5)
                finally:
                    self._threading_lock.release()

                if not ser_resp:
                    _LOGGER.warning(
                        "Empty/timeout response for '%s' (attempt %d)",
                        command_text,
                        attempt,
                    )
                    self._recover_serial()
                    continue

                resp = self.__parse_response(ser_resp, channel)
                rsp = resp.get("status")
                chs = resp.get("chs")
                _LOGGER.debug(
                    "Sent '%s' to transmitter '%s' ch '%s' cmd: %s resp: %s status: '%s' chs: '%s' attempt: %d",
                    command_text,
                    self._serial_number,
                    channel,
                    bytes_data,
                    ser_resp,
                    rsp,
                    chs,
                    attempt,
                )
                if command_text == COMMAND_CHECH_TEXT:
                    self._set_learned_channels(resp)
                else:
                    self._process_response(resp)
                self.last_response_ts = time.time()
                self.consecutive_failures = 0
                break
            except TimeoutError:
                _LOGGER.warning(
                    "Timeout waiting full response for '%s' (attempt %d)",
                    command_text,
                    attempt,
                )
                self.timeout_count += 1
                self.consecutive_failures += 1
                self._recover_serial()
            except Exception as exc:
                _LOGGER.exception(
                    "Error communicating with transmitter '%s' cmd '%s' ch '%s' attempt %d: %s",
                    self._serial_number,
                    command_text,
                    channel,
                    attempt,
                    exc,
                )
                self.error_count += 1
                self.consecutive_failures += 1
                self._recover_serial()
            time.sleep(0.5)

    def _read_exact(self, expected_len, overall_timeout=2.5):
        if not self._serial:
            return b""
        deadline = time.time() + overall_timeout
        buf = bytearray()
        while len(buf) < expected_len and time.time() < deadline:
            chunk = self._serial.read(expected_len - len(buf))
            if chunk:
                buf.extend(chunk)
            else:
                time.sleep(0.05)
        if len(buf) != expected_len:
            raise TimeoutError(
                f"Expected {expected_len} bytes, received {len(buf)} within {overall_timeout}s"
            )
        return bytes(buf)

    def _recover_serial(self):
        try:
            if self._serial and self._serial.is_open:
                try:
                    self._serial.close()
                except Exception:
                    pass
        finally:
            self.reconnect_count += 1
            self.init_serial_port()

    def _process_response(self, resp):
        for ch in resp["chs"]:
            if ch in self._learned_channels and self._learned_channels[ch] is not None:
                self._learned_channels[ch](resp)
            else:
                _LOGGER.error(
                    "The channel is not learned '%s' on the transmitter: '%s'.",
                    self._serial_number,
                    ch,
                )

    def __parse_response(self, ser_resp, channel):
        response = {
            "bytes": ser_resp,
            "header": ser_resp[0],
            "length": ser_resp[1],
            "command": ser_resp[2],
            "ch_h": self.__get_upper_channel_bits(ser_resp[3]),
            "ch_l": self.__get_lower_channel_bits(ser_resp[4]),
            "chs": set(),
            "status": None,
            "cs": None,
        }
        response["chs"] = set(response["ch_h"] + response["ch_l"])
        resp_length = len(ser_resp)
        if (sum(ser_resp) % 256) != 0:
            self.checksum_error_count += 1
            _LOGGER.error(
                "Checksum error from transmitter '%s' channel '%s' raw %s",
                self._serial_number,
                channel,
                ser_resp,
            )
        if resp_length == RESPONSE_LENGTH_CHECK:
            response["cs"] = ser_resp[5]
        elif resp_length == RESPONSE_LENGTH_SEND:
            if ser_resp[5] in INFO:
                response["status"] = INFO[ser_resp[5]]
            else:
                response["status"] = INFO_UNKNOWN
                _LOGGER.error(
                    "Transmitter: '%s' ch: '%s' status is unknown: '%X'.",
                    self._serial_number,
                    channel,
                    ser_resp[5],
                )
            response["cs"] = ser_resp[6]
        else:
            _LOGGER.error(
                "Transmitter: '%s' ch: '%s' unknown response: '%s'.",
                self._serial_number,
                channel,
                ser_resp,
            )
            response["status"] = INFO_UNKNOWN
        return response

    def __calculate_checksum(self, *args):
        return (256 - sum(args)) % 256

    def __create_serial_data(self, int_list):
        return bytes(int_list)

    def __set_upper_channel_bits(self, channel):
        return (1 << (channel - 1)) >> BIT_8

    def __set_lower_channel_bits(self, channel):
        return (1 << (channel - 1)) & HEX_255

    def __get_upper_channel_bits(self, byt):
        channels = []
        for i in range(0, 8):
            if (byt >> i) & 1 == 1:
                channels.append(i + 9)
        return tuple(channels)

    def __get_lower_channel_bits(self, byt):
        channels = []
        for i in range(0, 8):
            if (byt >> i) & 1 == 1:
                channels.append(i + 1)
        return tuple(channels)


class EleroRemoteTransmitter(EleroTransmitter):
    """Elero Transmitter Stick connected via ser2net (TCP)."""

    def __init__(self, serial_number, address):
        self._address = address
        super().__init__(None, serial_number, None, None, None, None)

    def init_serial(self):
        self.init_serial_port()
        if self._serial:
            self.check()

    def init_serial_port(self):
        url = f"socket://{self._address}"
        try:
            self._serial = serial.serial_for_url(url, timeout=2, write_timeout=2)
            _LOGGER.info(
                "Elero Transmitter Stick is remotely connected to '%s' with serial number: '%s'",
                self._address,
                self._serial_number,
            )
        except Exception as exc:
            _LOGGER.exception(
                "Unable to connect to remote serial port '%s' for serial number '%s': '%s'",
                url,
                self._serial_number,
                exc,
            )

    def log_out_serial_port_details(self):
        _LOGGER.debug("Remote Transmitter stick on address '%s'.", self._address)
