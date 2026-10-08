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
    CONF_TRANSMITTER_SERIAL_NUMBER,
    CONNECTION_LOCAL,
    CONNECTION_REMOTE,
    DEFAULT_BAUDRATE,
    DOMAIN,
    SUBENTRY_TYPE_COVER,
)

from custom_components.elero.config_flow import _can_connect_remote

from .conftest import (
    ALL_FEATURES,
    REMOTE_ADDRESS,
    TRANSMITTER_SERIAL,
    cover_form,
    make_entry,
    subentry_from_data,
)

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


def _remote_reachable(reachable: bool = True):
    return patch(
        "custom_components.elero.config_flow._can_connect_remote",
        return_value=reachable,
    )


async def test_remote(hass: HomeAssistant) -> None:
    result = await _start(hass, CONNECTION_REMOTE)
    assert result["step_id"] == "remote"

    with _remote_reachable():
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


async def test_remote_unreachable(hass: HomeAssistant) -> None:
    result = await _start(hass, CONNECTION_REMOTE)
    with _remote_reachable(False):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_TRANSMITTER_SERIAL_NUMBER: TRANSMITTER_SERIAL,
                CONF_REMOTE_TRANSMITTERS_ADDRESS: REMOTE_ADDRESS,
            },
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


def test_can_connect_remote_closes_probe() -> None:
    with patch(
        "custom_components.elero.config_flow.EleroRemoteTransmitter"
    ) as tx_cls:
        tx_cls.return_value.get_transmitter_state.return_value = True
        assert _can_connect_remote(TRANSMITTER_SERIAL, REMOTE_ADDRESS)
    tx_cls.return_value.close_serial.assert_called_once()


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
    """The sectioned form is flattened into sub-entry data with native types."""
    entry = make_entry([])
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COVER), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    # Stick not connected: a number box instead of a channel dropdown.
    assert "options" not in result["data_schema"].schema[CONF_CHANNEL].config

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        cover_form(
            name="Kitchen",
            channel=5,
            travel_up=40,
            travel_down=38.5,
            ventilation={"mode": "step_up", "position": 25, "duration": 1.2},
        ),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kitchen"

    subentry = next(iter(entry.subentries.values()))
    assert subentry.unique_id == f"{TRANSMITTER_SERIAL}_5"
    assert subentry.data == {
        "name": "Kitchen",
        CONF_CHANNEL: 5,
        "device_class": "venetian blind",
        CONF_SUPPORTED_FEATURES: ALL_FEATURES,
        "tilt_buttons": "presets",
        "travel_time_up": 40.0,
        "travel_time_down": 38.5,
        "tilt_travel_time": 2.0,
        "learn_travel_times": False,
        "ventilation": {"mode": "step_up", "position": 25.0, "duration": 1.2},
        "intermediate": {"mode": "fixed", "position": 75.0, "duration": 1.0},
    }


async def test_add_cover_rejects_used_channel(hass: HomeAssistant) -> None:
    entry = make_entry()  # channel 1 is taken
    entry.add_to_hass(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COVER), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], cover_form(name="Other", channel=1)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CHANNEL: "channel_in_use"}


async def test_reconfigure_prefills_legacy_cover(hass: HomeAssistant) -> None:
    """A pre-4.3 cover shows its derived settings and is saved in the new format."""
    legacy = {
        "name": "Living room",
        CONF_CHANNEL: 1,
        "device_class": "venetian blind",
        CONF_SUPPORTED_FEATURES: ["up", "down", "stop", "close_tilt"],
        "travel_time": 50.0,
        "tilt_step": 2.0,
        "tilt_travel_time": 1.0,
    }
    entry = make_entry([subentry_from_data(legacy)])
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["step_id"] == "reconfigure"
    schema = result["data_schema"].schema
    timing = schema["timing"].schema.schema
    assert _default(timing, "travel_time_up") == 50.0
    assert _default(timing, "travel_time_down") == 50.0
    ventilation = schema["ventilation"].schema.schema
    assert _default(ventilation, "mode") == "step_up"
    assert _default(ventilation, "duration") == 1.0

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        cover_form(
            name="Living room west",
            features=["up", "down", "stop", "close_tilt"],
            travel_up=45,
        ),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    subentry = entry.subentries[subentry_id]
    assert subentry.title == "Living room west"
    assert subentry.data["travel_time_up"] == 45.0
    assert "travel_time" not in subentry.data
    assert "tilt_step" not in subentry.data


def _default(schema: dict, key: str):
    for marker in schema:
        if marker == key:
            return marker.default()
    raise KeyError(key)
