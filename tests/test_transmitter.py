"""Test the Elero stick wire protocol with a fake serial port."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.elero.transmitter import EleroRemoteTransmitter, EleroTransmitter
from custom_components.elero.const import (
    INFO_BOTTOM_POSITION_STOP,
    INFO_TOP_POSITION_STOP,
    INFO_UNKNOWN,
)


def frame(*data: int) -> bytes:
    """Append the Elero checksum: all bytes incl. checksum sum to 0 mod 256."""
    return bytes([*data, (256 - sum(data)) % 256])


class FakeSerial:
    """Serial port that records writes and replays queued responses."""

    def __init__(self, responses: list[bytes] | None = None) -> None:
        self.responses = list(responses or [])
        self.written: list[bytes] = []
        self.is_open = True
        self.timeout = None
        self.write_timeout = None
        self._pending = b""

    def reset_input_buffer(self) -> None:
        self.flushed_before_write = len(self.written) + 1
        self._pending = b""

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        self._pending = self.responses.pop(0) if self.responses else b""
        return len(data)

    def read(self, size: int) -> bytes:
        chunk, self._pending = self._pending[:size], self._pending[size:]
        return chunk

    def close(self) -> None:
        self.is_open = False


@pytest.fixture(autouse=True)
def fake_time():
    """Retries and read deadlines use real time; make sleeping instant."""
    clock = {"now": 1_000_000.0}

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    with patch(
        "custom_components.elero.transmitter.time",
        SimpleNamespace(time=lambda: clock["now"], sleep=sleep),
    ):
        yield


def make_tx(port: FakeSerial) -> EleroTransmitter:
    tx = EleroTransmitter("/dev/null", "SERIAL", 38400, 8, "N", 1)
    tx._serial = port
    # Reconnects reuse the same fake port.
    tx.init_serial_port = lambda: setattr(tx, "_serial", port)
    return tx


def learn(tx: EleroTransmitter, *channels: int) -> None:
    low = sum(1 << (c - 1) for c in channels if c <= 8)
    high = sum(1 << (c - 9) for c in channels if c > 8)
    tx._serial.responses.append(frame(0xAA, 0x04, 0x4B, high, low))
    tx.check()


# ── command frames ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("up", 0x20),
        ("down", 0x40),
        ("stop", 0x10),
        ("intermediate", 0x44),
        ("ventilation_tilting", 0x24),
    ],
)
def test_send_command_frame(method: str, payload: int) -> None:
    port = FakeSerial([frame(0xAA, 0x05, 0x4D, 0x00, 0x01, 0x01)])
    tx = make_tx(port)
    getattr(tx, method)(1)
    assert port.written == [frame(0xAA, 0x05, 0x4C, 0x00, 0x01, payload)]


@pytest.mark.parametrize(
    ("channel", "high", "low"),
    [(1, 0x00, 0x01), (8, 0x00, 0x80), (9, 0x01, 0x00), (15, 0x40, 0x00)],
)
def test_channel_bitmask(channel: int, high: int, low: int) -> None:
    port = FakeSerial()
    tx = make_tx(port)
    with patch.object(tx, "_read_exact", return_value=b""):
        tx.info(channel)
    assert port.written[0] == frame(0xAA, 0x04, 0x4E, high, low)


def test_check_command_learns_channels() -> None:
    port = FakeSerial()
    tx = make_tx(port)
    learn(tx, 1, 3, 9, 15)
    assert port.written == [frame(0xAA, 0x02, 0x4A)]
    assert tx.get_learned_channels() == (1, 3, 9, 15)
    assert tx.last_response_ts is not None


def test_set_channel_requires_learned_channel() -> None:
    tx = make_tx(FakeSerial())
    learn(tx, 2)
    assert tx.set_channel(2, MagicMock()) is True
    assert tx.set_channel(3, MagicMock()) is False


# ── responses ───────────────────────────────────────────────────────────


def test_response_dispatched_to_channel_handler() -> None:
    port = FakeSerial()
    tx = make_tx(port)
    learn(tx, 1, 2)
    handler = MagicMock()
    tx.set_channel(2, handler)

    port.responses.append(frame(0xAA, 0x05, 0x4D, 0x00, 0x02, 0x01))
    tx.info(2)

    handler.assert_called_once()
    resp = handler.call_args.args[0]
    assert resp["status"] == INFO_TOP_POSITION_STOP
    assert resp["chs"] == {2}
    assert tx.consecutive_failures == 0


def test_unknown_status_reported_as_unknown() -> None:
    port = FakeSerial()
    tx = make_tx(port)
    learn(tx, 1)
    handler = MagicMock()
    tx.set_channel(1, handler)

    port.responses.append(frame(0xAA, 0x05, 0x4D, 0x00, 0x01, 0x77))
    tx.info(1)
    assert handler.call_args.args[0]["status"] == INFO_UNKNOWN


def test_checksum_error_is_counted_and_retried() -> None:
    port = FakeSerial()
    tx = make_tx(port)
    learn(tx, 1)
    handler = MagicMock()
    tx.set_channel(1, handler)

    bad = bytearray(frame(0xAA, 0x05, 0x4D, 0x00, 0x01, 0x01))
    bad[-1] ^= 0xFF
    port.responses += [bytes(bad), frame(0xAA, 0x05, 0x4D, 0x00, 0x01, 0x02)]
    tx.info(1)

    assert tx.checksum_error_count == 1
    assert len(port.written) == 1 + 2  # check + two attempts
    # Only the good frame's status is delivered.
    handler.assert_called_once()
    assert handler.call_args.args[0]["status"] == INFO_BOTTOM_POSITION_STOP


def test_misaligned_response_is_retried() -> None:
    port = FakeSerial()
    tx = make_tx(port)
    learn(tx, 1)
    handler = MagicMock()
    tx.set_channel(1, handler)

    good = frame(0xAA, 0x05, 0x4D, 0x00, 0x01, 0x01)
    port.responses += [good[3:] + good[:3], good]
    tx.info(1)

    assert len(port.written) == 1 + 2
    assert tx.reconnect_count == 0  # the port itself is fine
    handler.assert_called_once()
    assert handler.call_args.args[0]["status"] == INFO_TOP_POSITION_STOP


def test_input_buffer_flushed_before_each_write() -> None:
    port = FakeSerial([frame(0xAA, 0x05, 0x4D, 0x00, 0x01, 0x01)])
    tx = make_tx(port)
    tx.up(1)
    assert port.flushed_before_write == len(port.written)


# ── retries and recovery ────────────────────────────────────────────────


def test_timeout_retries_then_succeeds() -> None:
    port = FakeSerial()
    tx = make_tx(port)
    learn(tx, 1)
    handler = MagicMock()
    tx.set_channel(1, handler)

    port.responses += [b"", frame(0xAA, 0x05, 0x4D, 0x00, 0x01, 0x01)]
    tx.info(1)

    assert len(port.written) == 1 + 2  # check + two attempts
    assert tx.timeout_count == 1
    assert tx.reconnect_count == 1
    assert tx.consecutive_failures == 0
    handler.assert_called_once()


def test_gives_up_after_four_attempts() -> None:
    port = FakeSerial()
    tx = make_tx(port)
    learn(tx, 1)
    handler = MagicMock()
    tx.set_channel(1, handler)

    tx.info(1)

    assert len(port.written) == 1 + 4
    assert tx.timeout_count == 4
    assert tx.consecutive_failures == 4
    handler.assert_not_called()


def test_write_error_is_counted_and_recovered() -> None:
    port = FakeSerial()
    tx = make_tx(port)
    learn(tx, 1)
    port.write = MagicMock(side_effect=OSError("gone"))

    tx.up(1)

    assert tx.error_count == 4
    assert tx.reconnect_count == 4


def test_close_serial() -> None:
    port = FakeSerial()
    tx = make_tx(port)
    tx.close_serial()
    assert port.is_open is False


def test_remote_transmitter_uses_socket_url() -> None:
    port = FakeSerial([frame(0xAA, 0x04, 0x4B, 0x00, 0x01)])
    with patch(
        "custom_components.elero.transmitter.serial.serial_for_url", return_value=port
    ) as serial_for_url:
        tx = EleroRemoteTransmitter("SERIAL", "192.0.2.1:20109")
        tx.init_serial()

    assert serial_for_url.call_args.args[0] == "socket://192.0.2.1:20109"
    assert tx.get_transmitter_state() is True
    assert tx.get_learned_channels() == (1,)


def test_poll_attempts_can_be_limited() -> None:
    """Polls use fewer retries so a silent drive can't block the stick long."""
    port = FakeSerial()
    tx = make_tx(port)
    learn(tx, 1)
    tx.info(1, attempts=2)
    assert len(port.written) == 1 + 2
