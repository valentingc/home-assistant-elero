"""Support for Elero electrical drives."""

from __future__ import annotations

__version__ = "4.3.3"

import logging
import os
import time
from datetime import timedelta

import homeassistant.helpers.config_validation as cv
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
    PLATFORMS,
    SUBENTRY_TYPE_COVER,
)
from .hub import EleroHub
from .transmitter import EleroRemoteTransmitter, EleroTransmitter

type EleroConfigEntry = ConfigEntry[EleroHub]

_LOGGER = logging.getLogger(__name__)

# Send an Easy Check if the stick has been silent for this long.
WATCHDOG_IDLE = 300


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


async def async_setup_entry(hass: HomeAssistant, entry: EleroConfigEntry) -> bool:
    """Set up Elero from a config entry."""
    serial_number = entry.data.get(CONF_TRANSMITTER_SERIAL_NUMBER)
    connection_type = entry.data.get(CONF_CONNECTION_TYPE, CONNECTION_LOCAL)

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

    hub = EleroHub(hass, transmitter)
    entry.runtime_data = hub
    hub.async_start(entry)

    async def _async_close() -> None:
        await hass.async_add_executor_job(transmitter.close_serial)

    entry.async_on_unload(_async_close)

    # Register the transmitter stick as a HA "hub" device so individual
    # covers can attach to it via their `via_device_id`.
    hub_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, hub.serial_number)},
        manufacturer="Elero",
        model="Transmitter Stick",
        name=f"Elero {hub.serial_number}",
    )
    hub.hub_device_id = hub_device.id

    @callback
    def _watchdog(_now):
        if transmitter.last_response_ts is None:
            return
        idle = time.time() - transmitter.last_response_ts
        if idle > WATCHDOG_IDLE:
            _LOGGER.debug(
                "Watchdog sending Easy Check to '%s' after %.1fs idle",
                hub.serial_number,
                idle,
            )
            hub.async_request_check()

    entry.async_on_unload(
        async_track_time_interval(hass, _watchdog, timedelta(minutes=2))
    )

    async def _async_on_stop(_event) -> None:
        await _async_close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_stop)
    )

    # Auto-import legacy YAML covers as sub-entries (idempotent: keyed by channel).
    _auto_import_yaml_covers(hass, entry, hub.serial_number)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Adding, reconfiguring or removing a cover sub-entry only fires update
    # listeners; reload so the change takes effect without a restart.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


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


async def async_unload_entry(hass: HomeAssistant, entry: EleroConfigEntry) -> bool:
    """Unload an Elero config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
