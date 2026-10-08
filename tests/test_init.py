"""Test setting up, unloading and migrating the Elero integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.setup import async_setup_component

from custom_components.elero import cover as cover_platform
from custom_components.elero.diagnostics import async_get_config_entry_diagnostics
from custom_components.elero.const import (
    CONF_CHANNEL,
    CONF_CONNECTION_TYPE,
    CONF_TRANSMITTER_SERIAL_NUMBER,
    CONF_REMOTE_TRANSMITTERS_ADDRESS,
    CONNECTION_REMOTE,
    DOMAIN,
    SUBENTRY_TYPE_COVER,
)

from .conftest import (
    REMOTE_ADDRESS,
    TRANSMITTER_SERIAL,
    cover_form,
    cover_subentry,
    make_entry,
    setup_entry,
)


async def test_covers_attach_to_hub_device(
    hass: HomeAssistant, mock_transmitter
) -> None:
    """Covers load and are registered via the transmitter hub device."""
    entry = make_entry([cover_subentry("Living room", 1), cover_subentry("Kitchen", 2)])
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    dev_reg = dr.async_get(hass)
    hub = dev_reg.async_get_device_by_identifier(
        (DOMAIN, TRANSMITTER_SERIAL), entry.entry_id
    )
    assert hub is not None
    assert hub.model == "Transmitter Stick"

    ent_reg = er.async_get(hass)
    for channel in (1, 2):
        device = dev_reg.async_get_device_by_identifier(
            (DOMAIN, f"{TRANSMITTER_SERIAL}_{channel}"), entry.entry_id
        )
        assert device is not None
        assert device.via_device_id == hub.id

        entity_id = ent_reg.async_get_entity_id(
            "cover", DOMAIN, f"{TRANSMITTER_SERIAL}_{channel}"
        )
        assert entity_id is not None
        assert ent_reg.async_get(entity_id).device_id == device.id
        assert hass.states.get(entity_id) is not None

    # Each cover registers its response handler and is polled once on add.
    assert {c.args[0] for c in mock_transmitter.set_channel.call_args_list} == {1, 2}
    assert {c.args[0] for c in mock_transmitter.info.call_args_list} == {1, 2}


async def test_setup_not_ready_when_transmitter_unreachable(
    hass: HomeAssistant, mock_transmitter
) -> None:
    """A transmitter that does not answer leaves the entry retrying."""
    mock_transmitter.get_transmitter_state.return_value = False
    entry = make_entry()
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_closes_serial(
    hass: HomeAssistant, init_integration, mock_transmitter
) -> None:
    """Unloading the entry closes the connection and removes the entities."""
    entry = init_integration
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    mock_transmitter.close_serial.assert_called_once()
    assert hass.states.get("cover.living_room").state == "unavailable"


async def test_added_cover_subentry_is_set_up(
    hass: HomeAssistant, init_integration
) -> None:
    """A cover added through the sub-entry flow shows up without a restart."""
    entry = init_integration
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COVER), context={"source": "user"}
    )
    # Only taught-in channels that no cover uses yet are offered.
    channel_selector = result["data_schema"].schema[CONF_CHANNEL]
    assert channel_selector.config["options"] == ["2", "3", "4", "5"]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], cover_form(name="Bedroom", channel=3)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert hass.states.get("cover.bedroom") is not None
    assert hass.states.get("button.bedroom_ventilation") is not None


async def test_reconfigured_cover_subentry_is_applied(
    hass: HomeAssistant, init_integration
) -> None:
    """Reconfiguring a cover takes effect without a restart."""
    entry = init_integration
    subentry_id = next(iter(entry.subentries))
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], cover_form(travel_up=42)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    state = hass.states.get("cover.living_room")
    assert state.attributes["travel_time_up"] == 42


async def test_channel_change_keeps_entities_and_device(
    hass: HomeAssistant, init_integration
) -> None:
    """Moving a cover to another channel keeps its entity ids and device."""
    entry = init_integration
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{TRANSMITTER_SERIAL}_1"), entry.entry_id
    )

    subentry_id = next(iter(entry.subentries))
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], cover_form(channel=4)
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    cover = ent_reg.async_get("cover.living_room")
    assert cover.unique_id == f"{TRANSMITTER_SERIAL}_4"
    assert cover.device_id == device.id
    assert (
        ent_reg.async_get("button.living_room_ventilation").unique_id
        == f"{TRANSMITTER_SERIAL}_4_ventilation"
    )
    assert dev_reg.async_get(device.id).identifiers == {
        (DOMAIN, f"{TRANSMITTER_SERIAL}_4")
    }
    assert entry.subentries[subentry_id].unique_id == f"{TRANSMITTER_SERIAL}_4"
    assert hass.states.get("cover.living_room").attributes["channel"] == 4


async def test_channel_in_use_is_rejected(
    hass: HomeAssistant, mock_transmitter
) -> None:
    entry = make_entry([cover_subentry("Living room", 1), cover_subentry("Kitchen", 2)])
    await setup_entry(hass, entry)
    kitchen_id = next(
        sid for sid, sub in entry.subentries.items() if sub.title == "Kitchen"
    )
    # Number box fallback lets one type any channel; make sure it is checked.
    with patch.object(mock_transmitter, "get_learned_channels", return_value=()):
        result = await entry.start_subentry_reconfigure_flow(hass, kitchen_id)
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], cover_form(name="Kitchen", channel=1)
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CHANNEL: "channel_in_use"}


async def test_diagnostics(hass: HomeAssistant, init_integration) -> None:
    diag = await async_get_config_entry_diagnostics(hass, init_integration)
    assert diag["entry"][CONF_REMOTE_TRANSMITTERS_ADDRESS] == "**REDACTED**"
    assert diag["transmitter"]["learned_channels"] == [1, 2, 3, 4, 5]
    (cover,) = diag["covers"].values()
    assert cover["entity_id"] == "cover.living_room"
    assert cover["options"]["ventilation"]["mode"] == "step_up"


async def test_yaml_is_imported(hass: HomeAssistant, mock_transmitter) -> None:
    """Legacy YAML transmitters and covers become an entry with sub-entries."""
    config = {
        DOMAIN: {
            "remote_transmitters": [
                {"serial_number": TRANSMITTER_SERIAL, "address": REMOTE_ADDRESS}
            ]
        },
        "cover": [
            {
                "platform": DOMAIN,
                "covers": {
                    "shower": {
                        "serial_number": TRANSMITTER_SERIAL,
                        "name": "Shower",
                        "channel": 4,
                        "device_class": "roller shutter",
                        "supported_features": ["up", "down", "stop"],
                    }
                },
            }
        ],
    }
    # Only exercise the elero side of the import; the legacy cover platform
    # itself is covered separately.
    assert await async_setup_component(hass, DOMAIN, config)
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.source == SOURCE_IMPORT
    assert entry.data[CONF_CONNECTION_TYPE] == CONNECTION_REMOTE
    assert entry.data[CONF_TRANSMITTER_SERIAL_NUMBER] == TRANSMITTER_SERIAL

    subentries = list(entry.subentries.values())
    assert len(subentries) == 1
    assert subentries[0].data[CONF_CHANNEL] == 4
    assert subentries[0].title == "Shower"
    assert hass.states.get("cover.shower") is not None

    assert ir.async_get(hass).async_get_issue(DOMAIN, "deprecated_yaml")


async def test_yaml_import_is_idempotent(
    hass: HomeAssistant, mock_transmitter
) -> None:
    """Re-running the YAML cover import does not duplicate sub-entries."""
    entry = make_entry([cover_subentry("Shower", 4)])
    hass.data.setdefault(DOMAIN, {})["_yaml_covers"] = {
        TRANSMITTER_SERIAL: [
            {
                "name": "Shower",
                "channel": 4,
                "device_class": "roller shutter",
                "supported_features": ["up"],
            }
        ]
    }
    with patch("custom_components.elero.async_setup", return_value=True):
        await setup_entry(hass, entry)

    assert len(entry.subentries) == 1


async def test_legacy_platform_creates_nothing(hass: HomeAssistant) -> None:
    """`platform: elero` covers are imported as sub-entries instead."""
    add_entities = MagicMock()
    cover_platform.setup_platform(hass, {"covers": {}}, add_entities)
    add_entities.assert_not_called()


async def test_serial_closed_on_home_assistant_stop(
    hass: HomeAssistant, init_integration, mock_transmitter
) -> None:
    hass.bus.async_fire("homeassistant_stop")
    await hass.async_block_till_done()
    mock_transmitter.close_serial.assert_called()
