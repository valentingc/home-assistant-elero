"""Elero Transmitter Stick serial protocol (blocking; run in the executor)."""

from __future__ import annotations

import logging
import threading
import time

import serial

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
    RESPONSE_LENGTH_CHECK,
    RESPONSE_LENGTH_INFO,
    RESPONSE_LENGTH_SEND,
)

_LOGGER = logging.getLogger(__name__)

MAX_ATTEMPTS = 4


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

    def info(self, channel, attempts=MAX_ATTEMPTS):
        self.__process_command(
            COMMAND_INFO_TEXT,
            self.__get_info_command(channel),
            channel,
            RESPONSE_LENGTH_INFO,
            attempts,
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

    def __process_command(
        self, command_text, int_list, channel, resp_length, max_attempts=MAX_ATTEMPTS
    ):
        int_list.append(self.__calculate_checksum(*int_list))
        bytes_data = self.__create_serial_data(int_list)

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
            if attempt < max_attempts:
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
