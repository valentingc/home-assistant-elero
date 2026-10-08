"""Test the Elero config flow and cover sub-entry flow."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_IMPORT, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.elero.const import (
    CONF_BAUDRATE,
    CONF_CHANNEL,
    CONF_CONNECTION_TYPE,
    CONF_REMOTE_TRANSMITTERS_ADDRESS,
    CONF_SUPPORTED_FEATURES,
    CONF_TILT_STEP,
    CONF_TILT_TRAVEL_TIME,
    CONF_TRANSMITTER_SERIAL_NUMBER,
    CONF_TRAVEL_TIME,
    CONNECTION_LOCAL,
    CONNECTION_REMOTE,
    DEFAULT_BAUDRATE,
    DEFAULT_TILT_STEP,
    DEFAULT_TILT_TRAVEL_TIME,
    DEFAULT_TRAVEL_TIME,
    DOMAIN,
    SUBENTRY_TYPE_COVER,
)

from .conftest import REMOTE_ADDRESS, TRANSMITTER_SERIAL, make_entry

ELERO_PORT = SimpleNamespace(
    device="/dev/ttyUSB0",
    manufacturer="elero GmbH",
    product="Transmitter Stick",
    serial_number=TRANSMITTER_SERIAL,
)
OTHER_PORT = SimpleNamespace(
    device="/dev/ttyUSB1",
    manufacturer="FTDI",
    product="FT232R",
    serial_number="XYZ",
)


@pytest.fixture(autouse=True)
def skip_setup():
    """Don't actually set up entries created by the flow."""
    with patch("custom_components.elero.async_setup_entry", return_value=True):
        yield


def _comports(*ports):
    return patch(
        "custom_components.elero.config_flow.list_ports.comports",
        return_value=list(ports),
    )


async def _start(hass: HomeAssistant, connection_type: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_TYPE: connection_type}
    )


async def test_local_stick(hass: HomeAssistant) -> None:
    """Only Elero sticks are offered; picking one creates the entry."""
    with _comports(ELERO_PORT, OTHER_PORT):
        result = await _start(hass, CONNECTION_LOCAL)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "local"
        offered = result["data_schema"].schema[CONF_TRANSMITTER_SERIAL_NUMBER].container
        assert list(offered) == [TRANSMITTER_SERIAL]

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TRANSMITTER_SERIAL_NUMBER: TRANSMITTER_SERIAL}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Elero {TRANSMITTER_SERIAL}"
    assert result["data"][CONF_CONNECTION_TYPE] == CONNECTION_LOCAL
    assert result["data"][CONF_BAUDRATE] == DEFAULT_BAUDRATE
    assert result["result"].unique_id == TRANSMITTER_SERIAL


async def test_local_no_sticks(hass: HomeAssistant) -> None:
    with _comports(OTHER_PORT):
        result = await _start(hass, CONNECTION_LOCAL)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_sticks_found"


async def test_local_already_configured(hass: HomeAssistant) -> None:
    make_entry([]).add_to_hass(hass)
    with _comports(ELERO_PORT):
        result = await _start(hass, CONNECTION_LOCAL)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TRANSMITTER_SERIAL_NUMBER: TRANSMITTER_SERIAL}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_remote(hass: HomeAssistant) -> None:
    result = await _start(hass, CONNECTION_REMOTE)
    assert result["step_id"] == "remote"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_TRANSMITTER_SERIAL_NUMBER: f" {TRANSMITTER_SERIAL} ",
            CONF_REMOTE_TRANSMITTERS_ADDRESS: f" {REMOTE_ADDRESS} ",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_CONNECTION_TYPE: CONNECTION_REMOTE,
        CONF_TRANSMITTER_SERIAL_NUMBER: TRANSMITTER_SERIAL,
        CONF_REMOTE_TRANSMITTERS_ADDRESS: REMOTE_ADDRESS,
    }


async def test_remote_blank_input(hass: HomeAssistant) -> None:
    result = await _start(hass, CONNECTION_REMOTE)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_TRANSMITTER_SERIAL_NUMBER: "  ",
            CONF_REMOTE_TRANSMITTERS_ADDRESS: REMOTE_ADDRESS,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_input"}


async def test_import_local_without_serial_picks_first_stick(
    hass: HomeAssistant,
) -> None:
    with _comports(ELERO_PORT):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={CONF_CONNECTION_TYPE: CONNECTION_LOCAL},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TRANSMITTER_SERIAL_NUMBER] == TRANSMITTER_SERIAL
    assert result["data"][CONF_BAUDRATE] == DEFAULT_BAUDRATE


async def test_import_remote(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            CONF_CONNECTION_TYPE: CONNECTION_REMOTE,
            CONF_TRANSMITTER_SERIAL_NUMBER: TRANSMITTER_SERIAL,
            CONF_REMOTE_TRANSMITTERS_ADDRESS: REMOTE_ADDRESS,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REMOTE_TRANSMITTERS_ADDRESS] == REMOTE_ADDRESS


async def test_add_cover_subentry(hass: HomeAssistant) -> None:
    """Selector output is normalised to native types and defaults are filled."""
    entry = make_entry([])
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COVER), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Kitchen",
            CONF_CHANNEL: 5.0,
            "device_class": "roller shutter",
            CONF_SUPPORTED_FEATURES: ["up", "down"],
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kitchen"

    subentry = next(iter(entry.subentries.values()))
    assert subentry.data[CONF_CHANNEL] == 5
    assert isinstance(subentry.data[CONF_CHANNEL], int)
    assert subentry.data[CONF_TRAVEL_TIME] == DEFAULT_TRAVEL_TIME
    assert subentry.data[CONF_TILT_STEP] == DEFAULT_TILT_STEP
    assert subentry.data[CONF_TILT_TRAVEL_TIME] == DEFAULT_TILT_TRAVEL_TIME


async def test_reconfigure_cover_subentry(hass: HomeAssistant) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Living room west",
            CONF_CHANNEL: 1,
            "device_class": "venetian blind",
            CONF_SUPPORTED_FEATURES: ["up", "down", "stop"],
            CONF_TRAVEL_TIME: 45,
            CONF_TILT_STEP: 0,
            CONF_TILT_TRAVEL_TIME: 1.5,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    subentry = entry.subentries[subentry_id]
    assert subentry.title == "Living room west"
    assert subentry.data[CONF_TRAVEL_TIME] == 45.0
    assert subentry.data[CONF_TILT_STEP] == 0.0
    assert subentry.data[CONF_TILT_TRAVEL_TIME] == 1.5
