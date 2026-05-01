"""Config flow for the Elero integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_DEVICE_CLASS, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
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
    CONF_TILT_STEP,
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
    DEFAULT_TRAVEL_TIME,
    DOMAIN,
    ELERO_COVER_DEVICE_CLASSES,
    SUBENTRY_TYPE_COVER,
    SUPPORTED_FEATURE_NAMES,
)

_LOGGER = logging.getLogger(__name__)


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


def _cover_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): TextSelector(),
            vol.Required(
                CONF_CHANNEL, default=defaults.get(CONF_CHANNEL, 1)
            ): NumberSelector(
                NumberSelectorConfig(min=1, max=15, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(
                CONF_DEVICE_CLASS,
                default=defaults.get(CONF_DEVICE_CLASS, "venetian blind"),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(ELERO_COVER_DEVICE_CLASSES.keys()),
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_SUPPORTED_FEATURES,
                default=defaults.get(
                    CONF_SUPPORTED_FEATURES, ["up", "down", "stop"]
                ),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=SUPPORTED_FEATURE_NAMES,
                    mode=SelectSelectorMode.LIST,
                    multiple=True,
                )
            ),
            vol.Optional(
                CONF_TRAVEL_TIME,
                default=defaults.get(CONF_TRAVEL_TIME, DEFAULT_TRAVEL_TIME),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=600, step=0.5, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_TILT_STEP,
                default=defaults.get(CONF_TILT_STEP, DEFAULT_TILT_STEP),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=100, step=0.5, mode=NumberSelectorMode.BOX
                )
            ),
        }
    )


class EleroCoverSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure an individual Elero cover."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data=_normalize(user_input),
            )
        return self.async_show_form(step_id="user", data_schema=_cover_schema())

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry: ConfigSubentry = self._get_reconfigure_subentry()
        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=user_input[CONF_NAME],
                data=_normalize(user_input),
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_cover_schema(defaults=dict(subentry.data)),
        )


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce numeric strings from selector output back to native types."""
    out = dict(data)
    out[CONF_CHANNEL] = int(out[CONF_CHANNEL])
    out[CONF_TRAVEL_TIME] = float(out.get(CONF_TRAVEL_TIME, DEFAULT_TRAVEL_TIME))
    out[CONF_TILT_STEP] = float(out.get(CONF_TILT_STEP, DEFAULT_TILT_STEP))
    return out
