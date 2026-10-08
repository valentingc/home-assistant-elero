"""Shared fixtures for Elero tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import DEFAULT, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import DATA_INSTANCES
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.elero.const import (
    CONF_CHANNEL,
    CONF_CONNECTION_TYPE,
    CONF_REMOTE_TRANSMITTERS_ADDRESS,
    CONF_TRANSMITTER_SERIAL_NUMBER,
    CONNECTION_REMOTE,
    DOMAIN,
    SUBENTRY_TYPE_COVER,
)
from custom_components.elero.transmitter import EleroTransmitter

TRANSMITTER_SERIAL = "AABBCCDD"
REMOTE_ADDRESS = "192.0.2.1:20109"

ALL_FEATURES = [
    "up",
    "down",
    "stop",
    "set_position",
    "open_tilt",
    "close_tilt",
    "stop_tilt",
    "set_tilt_position",
]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load integrations from custom_components/."""
    return


@pytest.fixture(autouse=True)
def fail_on_deprecated_usage(caplog):
    """Fail any test that triggers an HA deprecation report for elero.

    In tests HA only logs these, but in production the same report can raise
    when HA can't attribute the call to the integration (see HA 2026.9 and
    the `via_device` regression), so treat them as errors.
    """
    yield
    reports = [
        r.getMessage()
        for r in caplog.get_records("call")
        if r.name == "homeassistant.helpers.frame" and "elero" in r.getMessage()
    ]
    assert not reports, "\n".join(reports)


@pytest.fixture
def mock_transmitter():
    """Replace the remote transmitter with a fake that needs no network/serial."""
    tx = MagicMock(spec=EleroTransmitter)
    tx.get_serial_number.return_value = TRANSMITTER_SERIAL
    tx.get_transmitter_state.return_value = True
    tx.get_learned_channels.return_value = (1, 2, 3, 4, 5)
    # Remember status callbacks across reset_mock(); return_value still
    # decides whether the channel counts as taught-in.
    tx.handlers = {}

    def set_channel(channel, handler):
        tx.handlers[channel] = handler
        return DEFAULT

    tx.set_channel.side_effect = set_channel
    tx.set_channel.return_value = True
    # Diagnostic attributes surfaced in the cover's state attributes.
    tx.last_command_ts = None
    tx.last_response_ts = None
    tx.error_count = 0
    tx.timeout_count = 0
    tx.reconnect_count = 0
    tx.checksum_error_count = 0
    tx.consecutive_failures = 0
    with patch(
        "custom_components.elero.EleroRemoteTransmitter", return_value=tx
    ):
        yield tx


def cover_data(
    name: str = "Living room",
    channel: int = 1,
    *,
    device_class: str = "venetian blind",
    features: list[str] | None = None,
    travel_up: float = 52.0,
    travel_down: float = 50.0,
    tilt_time: float = 2.0,
    tilt_buttons: str = "presets",
    learn: bool = False,
    ventilation: dict | None = None,
    intermediate: dict | None = None,
) -> dict[str, Any]:
    """Cover sub-entry data in the current (4.2) format."""
    return {
        "name": name,
        CONF_CHANNEL: channel,
        "device_class": device_class,
        "supported_features": features or ALL_FEATURES,
        "tilt_buttons": tilt_buttons,
        "travel_time_up": travel_up,
        "travel_time_down": travel_down,
        "tilt_travel_time": tilt_time,
        "learn_travel_times": learn,
        "ventilation": ventilation
        or {"mode": "step_up", "position": 25.0, "duration": 1.0},
        "intermediate": intermediate
        or {"mode": "fixed", "position": 75.0, "duration": 1.0},
    }


def cover_subentry(name: str = "Living room", channel: int = 1, **kwargs) -> ConfigSubentryData:
    return subentry_from_data(cover_data(name, channel, **kwargs))


def subentry_from_data(data: dict[str, Any]) -> ConfigSubentryData:
    return ConfigSubentryData(
        subentry_type=SUBENTRY_TYPE_COVER,
        title=data["name"],
        unique_id=f"{TRANSMITTER_SERIAL}_{data[CONF_CHANNEL]}",
        data=data,
    )


def cover_form(**kwargs) -> dict[str, Any]:
    """Cover sub-entry form input (with sections), as the UI submits it."""
    data = cover_data(**kwargs)
    return {
        "name": data["name"],
        # Dropdown (stick connected) and number box both accept a string.
        CONF_CHANNEL: str(data[CONF_CHANNEL]),
        "device_class": data["device_class"],
        "supported_features": data["supported_features"],
        "tilt_buttons": data["tilt_buttons"],
        "timing": {
            "travel_time_up": data["travel_time_up"],
            "travel_time_down": data["travel_time_down"],
            "tilt_travel_time": data["tilt_travel_time"],
            "learn_travel_times": data["learn_travel_times"],
        },
        "ventilation": data["ventilation"],
        "intermediate": data["intermediate"],
    }


def make_entry(subentries: list[ConfigSubentryData] | None = None) -> MockConfigEntry:
    """Build a remote-transmitter config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Elero {TRANSMITTER_SERIAL}",
        unique_id=TRANSMITTER_SERIAL,
        data={
            CONF_CONNECTION_TYPE: CONNECTION_REMOTE,
            CONF_TRANSMITTER_SERIAL_NUMBER: TRANSMITTER_SERIAL,
            CONF_REMOTE_TRANSMITTERS_ADDRESS: REMOTE_ADDRESS,
        },
        subentries_data=subentries if subentries is not None else [cover_subentry()],
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
async def init_integration(hass: HomeAssistant, mock_transmitter) -> MockConfigEntry:
    """Set up the integration with one fully-featured cover on channel 1."""
    entry = make_entry()
    await setup_entry(hass, entry)
    return entry


def get_entity(hass: HomeAssistant, entity_id: str) -> Any:
    """Return the live entity object behind an entity_id."""
    entity = hass.data[DATA_INSTANCES]["cover"].get_entity(entity_id)
    assert entity is not None, entity_id
    return entity


def status_handler(mock_transmitter, channel: int = 1):
    """The callback the hub registered with the stick for `channel`."""
    assert channel in mock_transmitter.handlers, f"channel {channel} not registered"
    return mock_transmitter.handlers[channel]


@pytest.fixture
def respond(hass: HomeAssistant, mock_transmitter):
    """Report a drive status, as the stick would from the executor thread."""

    async def _respond(status: str, channel: int = 1) -> None:
        await hass.async_add_executor_job(
            status_handler(mock_transmitter, channel), {"status": status}
        )
        await hass.async_block_till_done()

    return _respond
