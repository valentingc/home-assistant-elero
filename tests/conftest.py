"""Shared fixtures for Elero tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import DATA_INSTANCES
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.elero import EleroTransmitter
from custom_components.elero.const import (
    CONF_CHANNEL,
    CONF_CONNECTION_TYPE,
    CONF_REMOTE_TRANSMITTERS_ADDRESS,
    CONF_SUPPORTED_FEATURES,
    CONF_TILT_STEP,
    CONF_TILT_TRAVEL_TIME,
    CONF_TRANSMITTER_SERIAL_NUMBER,
    CONF_TRAVEL_TIME,
    CONNECTION_REMOTE,
    DOMAIN,
    SUBENTRY_TYPE_COVER,
)

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


def cover_subentry(
    name: str = "Living room",
    channel: int = 1,
    *,
    device_class: str = "venetian blind",
    features: list[str] | None = None,
    travel_time: float = 30.0,
    tilt_step: float = 2.0,
    tilt_travel_time: float = 2.0,
) -> ConfigSubentryData:
    """Build cover sub-entry data as the config flow would store it."""
    return ConfigSubentryData(
        subentry_type=SUBENTRY_TYPE_COVER,
        title=name,
        unique_id=f"{TRANSMITTER_SERIAL}_{channel}",
        data={
            "name": name,
            CONF_CHANNEL: channel,
            "device_class": device_class,
            CONF_SUPPORTED_FEATURES: features or ALL_FEATURES,
            CONF_TRAVEL_TIME: travel_time,
            CONF_TILT_STEP: tilt_step,
            CONF_TILT_TRAVEL_TIME: tilt_travel_time,
        },
    )


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
