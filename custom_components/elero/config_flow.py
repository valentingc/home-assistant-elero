"""Config flow for the Elero integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_DEVICE_CLASS, CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from serial.tools import list_ports

from .const import (
    CONF_BAUDRATE,
    CONF_BYTESIZE,
    CONF_CHANNEL,
    CONF_CONNECTION_TYPE,
    CONF_PARITY,
    CONF_REMOTE_TRANSMITTERS_ADDRESS,
    CONF_STOPBITS,
    CONF_SUPPORTED_FEATURES,
    CONF_INTERMEDIATE,
    CONF_LEARN_TRAVEL_TIMES,
    CONF_PRESET_DURATION,
    CONF_PRESET_MODE,
    CONF_PRESET_POSITION,
    CONF_TILT_BUTTONS,
    CONF_TILT_TRAVEL_TIME,
    CONF_TRANSMITTER_SERIAL_NUMBER,
    CONF_TRAVEL_TIME_DOWN,
    CONF_TRAVEL_TIME_UP,
    CONF_VENTILATION,
    CONNECTION_LOCAL,
    CONNECTION_REMOTE,
    DEFAULT_BAUDRATE,
    DEFAULT_BRAND,
    DEFAULT_BYTESIZE,
    DEFAULT_PARITY,
    DEFAULT_PRODUCT,
    DEFAULT_STOPBITS,
    DOMAIN,
    ELERO_COVER_DEVICE_CLASSES,
    PRESET_MODES,
    SECTION_TIMING,
    SUBENTRY_TYPE_COVER,
    SUPPORTED_FEATURE_NAMES,
    TILT_BUTTONS_MODES,
)
from .model import CoverOptions
from .transmitter import EleroRemoteTransmitter

_LOGGER = logging.getLogger(__name__)


def _can_connect_remote(serial: str, address: str) -> bool:
    """Check that a ser2net stick answers before creating the entry."""
    tx = EleroRemoteTransmitter(serial, address)
    try:
        tx.init_serial()
        return tx.get_transmitter_state()
    finally:
        tx.close_serial()


def _discover_local_sticks() -> list[dict[str, str]]:
    """Return USB ports that look like Elero transmitter sticks."""
    sticks: list[dict[str, str]] = []
    for cp in list_ports.comports():
        if (
            cp.manufacturer
            and DEFAULT_BRAND in cp.manufacturer
            and cp.product
            and DEFAULT_PRODUCT in cp.product
            and cp.serial_number
        ):
            sticks.append({"device": cp.device, "serial": cp.serial_number})
    return sticks


# ── Main config flow ────────────────────────────────────────────────────


class EleroConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Elero."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._discovered: list[dict[str, str]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick local USB or remote ser2net."""
        if user_input is not None:
            if user_input[CONF_CONNECTION_TYPE] == CONNECTION_LOCAL:
                return await self.async_step_local()
            return await self.async_step_remote()

        schema = vol.Schema(
            {
                vol.Required(CONF_CONNECTION_TYPE, default=CONNECTION_LOCAL): vol.In(
                    {
                        CONNECTION_LOCAL: "Local USB stick",
                        CONNECTION_REMOTE: "Remote (ser2net)",
                    }
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a discovered local Elero stick."""
        if not self._discovered:
            self._discovered = await self.hass.async_add_executor_job(
                _discover_local_sticks
            )

        if not self._discovered:
            return self.async_abort(reason="no_sticks_found")

        serials = {s["serial"]: f"{s['serial']} ({s['device']})" for s in self._discovered}

        if user_input is not None:
            serial = user_input[CONF_TRANSMITTER_SERIAL_NUMBER]
            await self.async_set_unique_id(serial)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Elero {serial}",
                data={
                    CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
                    CONF_TRANSMITTER_SERIAL_NUMBER: serial,
                    CONF_BAUDRATE: DEFAULT_BAUDRATE,
                    CONF_BYTESIZE: DEFAULT_BYTESIZE,
                    CONF_PARITY: DEFAULT_PARITY,
                    CONF_STOPBITS: DEFAULT_STOPBITS,
                },
            )

        schema = vol.Schema(
            {vol.Required(CONF_TRANSMITTER_SERIAL_NUMBER): vol.In(serials)}
        )
        return self.async_show_form(step_id="local", data_schema=schema)

    async def async_step_remote(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a remote ser2net stick."""
        errors: dict[str, str] = {}
        if user_input is not None:
            serial = user_input[CONF_TRANSMITTER_SERIAL_NUMBER].strip()
            address = user_input[CONF_REMOTE_TRANSMITTERS_ADDRESS].strip()
            if not serial or not address:
                errors["base"] = "invalid_input"
            else:
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                if not await self.hass.async_add_executor_job(
                    _can_connect_remote, serial, address
                ):
                    errors["base"] = "cannot_connect"
            if not errors:
                return self.async_create_entry(
                    title=f"Elero {serial} ({address})",
                    data={
                        CONF_CONNECTION_TYPE: CONNECTION_REMOTE,
                        CONF_TRANSMITTER_SERIAL_NUMBER: serial,
                        CONF_REMOTE_TRANSMITTERS_ADDRESS: address,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_TRANSMITTER_SERIAL_NUMBER): TextSelector(),
                vol.Required(CONF_REMOTE_TRANSMITTERS_ADDRESS): TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="remote", data_schema=schema, errors=errors
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Import legacy YAML config."""
        serial = import_data.get(CONF_TRANSMITTER_SERIAL_NUMBER)
        if not serial:
            # YAML used to allow auto-discovery without a serial; pick first stick.
            sticks = await self.hass.async_add_executor_job(_discover_local_sticks)
            if not sticks:
                return self.async_abort(reason="no_sticks_found")
            serial = sticks[0]["serial"]

        await self.async_set_unique_id(serial)
        self._abort_if_unique_id_configured()

        connection_type = import_data.get(CONF_CONNECTION_TYPE, CONNECTION_LOCAL)
        title = f"Elero {serial} (imported)"
        data: dict[str, Any] = {
            CONF_CONNECTION_TYPE: connection_type,
            CONF_TRANSMITTER_SERIAL_NUMBER: serial,
        }
        if connection_type == CONNECTION_LOCAL:
            data.update(
                {
                    CONF_BAUDRATE: import_data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
                    CONF_BYTESIZE: import_data.get(CONF_BYTESIZE, DEFAULT_BYTESIZE),
                    CONF_PARITY: import_data.get(CONF_PARITY, DEFAULT_PARITY),
                    CONF_STOPBITS: import_data.get(CONF_STOPBITS, DEFAULT_STOPBITS),
                }
            )
        else:
            data[CONF_REMOTE_TRANSMITTERS_ADDRESS] = import_data[
                CONF_REMOTE_TRANSMITTERS_ADDRESS
            ]
        return self.async_create_entry(title=title, data=data)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_COVER: EleroCoverSubentryFlow}


# ── Sub-entry flow: cover ──────────────────────────────────────────────

_PRESET_MODE_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=PRESET_MODES,
        mode=SelectSelectorMode.DROPDOWN,
        translation_key="preset_mode",
    )
)
_PERCENT = NumberSelector(
    NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER,
                         unit_of_measurement="%")
)
_SECONDS = NumberSelector(
    NumberSelectorConfig(min=0.1, max=60, step=0.1, mode=NumberSelectorMode.BOX,
                         unit_of_measurement="s")
)


def _seconds(minimum: float, maximum: float, step: float) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )


def _preset_section(preset) -> section:
    return section(
        vol.Schema(
            {
                vol.Required(CONF_PRESET_MODE, default=preset.mode): _PRESET_MODE_SELECTOR,
                vol.Required(CONF_PRESET_POSITION, default=preset.position): _PERCENT,
                vol.Required(CONF_PRESET_DURATION, default=preset.duration): _SECONDS,
            }
        ),
        {"collapsed": True},
    )


def _channel_selector(choices: list[int] | None):
    if choices:
        return SelectSelector(
            SelectSelectorConfig(
                options=[str(c) for c in choices], mode=SelectSelectorMode.DROPDOWN
            )
        )
    return NumberSelector(
        NumberSelectorConfig(min=1, max=15, step=1, mode=NumberSelectorMode.BOX)
    )


def _cover_schema(
    data: dict[str, Any] | None, channel_choices: list[int] | None
) -> vol.Schema:
    data = data or {}
    name = data.get(CONF_NAME, "")
    device_class = data.get(CONF_DEVICE_CLASS, "venetian blind")
    features = list(data.get(CONF_SUPPORTED_FEATURES, ["up", "down", "stop"]))
    channel = data.get(CONF_CHANNEL)
    if channel is None:
        channel = channel_choices[0] if channel_choices else 1
    opts = CoverOptions.from_data({CONF_CHANNEL: channel, **data})
    channel_default = str(channel) if channel_choices else channel

    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=name): TextSelector(),
            vol.Required(CONF_CHANNEL, default=channel_default): _channel_selector(
                channel_choices
            ),
            vol.Required(CONF_DEVICE_CLASS, default=device_class): SelectSelector(
                SelectSelectorConfig(
                    options=list(ELERO_COVER_DEVICE_CLASSES),
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(CONF_SUPPORTED_FEATURES, default=features): SelectSelector(
                SelectSelectorConfig(
                    options=SUPPORTED_FEATURE_NAMES,
                    mode=SelectSelectorMode.LIST,
                    multiple=True,
                )
            ),
            vol.Required(CONF_TILT_BUTTONS, default=opts.tilt_buttons): SelectSelector(
                SelectSelectorConfig(
                    options=TILT_BUTTONS_MODES,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="tilt_buttons",
                )
            ),
            vol.Required(SECTION_TIMING): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_TRAVEL_TIME_UP, default=opts.travel_up
                        ): _seconds(1, 600, 0.5),
                        vol.Required(
                            CONF_TRAVEL_TIME_DOWN, default=opts.travel_down
                        ): _seconds(1, 600, 0.5),
                        vol.Required(
                            CONF_TILT_TRAVEL_TIME, default=opts.tilt_time
                        ): _seconds(0, 30, 0.1),
                        vol.Required(
                            CONF_LEARN_TRAVEL_TIMES, default=opts.learn_travel_times
                        ): BooleanSelector(),
                    }
                )
            ),
            vol.Required(CONF_VENTILATION): _preset_section(opts.ventilation),
            vol.Required(CONF_INTERMEDIATE): _preset_section(opts.intermediate),
        }
    )


def _normalize(user_input: dict[str, Any]) -> dict[str, Any]:
    """Flatten the form into sub-entry data with native types."""
    timing = user_input[SECTION_TIMING]

    def preset(data: dict[str, Any]) -> dict[str, Any]:
        return {
            CONF_PRESET_MODE: data[CONF_PRESET_MODE],
            CONF_PRESET_POSITION: float(data[CONF_PRESET_POSITION]),
            CONF_PRESET_DURATION: float(data[CONF_PRESET_DURATION]),
        }

    return {
        CONF_NAME: user_input[CONF_NAME],
        CONF_CHANNEL: int(float(user_input[CONF_CHANNEL])),
        CONF_DEVICE_CLASS: user_input[CONF_DEVICE_CLASS],
        CONF_SUPPORTED_FEATURES: list(user_input[CONF_SUPPORTED_FEATURES]),
        CONF_TILT_BUTTONS: user_input[CONF_TILT_BUTTONS],
        CONF_TRAVEL_TIME_UP: float(timing[CONF_TRAVEL_TIME_UP]),
        CONF_TRAVEL_TIME_DOWN: float(timing[CONF_TRAVEL_TIME_DOWN]),
        CONF_TILT_TRAVEL_TIME: float(timing[CONF_TILT_TRAVEL_TIME]),
        CONF_LEARN_TRAVEL_TIMES: bool(timing[CONF_LEARN_TRAVEL_TIMES]),
        CONF_VENTILATION: preset(user_input[CONF_VENTILATION]),
        CONF_INTERMEDIATE: preset(user_input[CONF_INTERMEDIATE]),
    }


class EleroCoverSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure an individual Elero cover."""

    def _serial(self) -> str:
        return self._get_entry().data[CONF_TRANSMITTER_SERIAL_NUMBER]

    def _used_channels(self, exclude: str | None = None) -> set[int]:
        return {
            int(sub.data[CONF_CHANNEL])
            for sub_id, sub in self._get_entry().subentries.items()
            if sub.subentry_type == SUBENTRY_TYPE_COVER and sub_id != exclude
        }

    def _channel_choices(self, exclude: str | None = None) -> list[int] | None:
        """Taught-in channels not used by another cover, if the stick is up."""
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return None
        learned = entry.runtime_data.learned_channels()
        free = [c for c in learned if c not in self._used_channels(exclude)]
        return free or None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize(user_input)
            if data[CONF_CHANNEL] in self._used_channels():
                errors[CONF_CHANNEL] = "channel_in_use"
            else:
                return self.async_create_entry(
                    title=data[CONF_NAME],
                    data=data,
                    unique_id=f"{self._serial()}_{data[CONF_CHANNEL]}",
                )
        choices = self._channel_choices()
        schema = _cover_schema(None, choices)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry: ConfigSubentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize(user_input)
            old_channel = int(subentry.data[CONF_CHANNEL])
            if data[CONF_CHANNEL] in self._used_channels(subentry.subentry_id):
                errors[CONF_CHANNEL] = "channel_in_use"
            else:
                unique_id = subentry.unique_id
                if data[CONF_CHANNEL] != old_channel:
                    self._move_channel(old_channel, data[CONF_CHANNEL])
                    unique_id = f"{self._serial()}_{data[CONF_CHANNEL]}"
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    title=data[CONF_NAME],
                    data=data,
                    unique_id=unique_id,
                )
        choices = self._channel_choices(subentry.subentry_id)
        schema = _cover_schema(dict(subentry.data), choices)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )

    def _move_channel(self, old: int, new: int) -> None:
        """Keep the entities and device (and their history) on a channel change."""
        serial = self._serial()
        ent_reg = er.async_get(self.hass)
        for platform, suffix in (("cover", ""), ("button", "_ventilation"), ("button", "_intermediate")):
            old_uid = f"{serial}_{old}{suffix}"
            if entity_id := ent_reg.async_get_entity_id(platform, DOMAIN, old_uid):
                ent_reg.async_update_entity(
                    entity_id, new_unique_id=f"{serial}_{new}{suffix}"
                )
        dev_reg = dr.async_get(self.hass)
        if device := dev_reg.async_get_device_by_identifier(
            (DOMAIN, f"{serial}_{old}"), self._get_entry().entry_id
        ):
            dev_reg.async_update_device(
                device.id, new_identifiers={(DOMAIN, f"{serial}_{new}")}
            )
