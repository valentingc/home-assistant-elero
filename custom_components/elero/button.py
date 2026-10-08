"""Buttons for the Elero preset commands (ventilation / intermediate).

HA's open/close tilt controls are mapped to these presets by default, but
their names say little about what a programmed drive actually does. These
buttons expose the presets under their Elero names.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CHANNEL, SUBENTRY_TYPE_COVER, TILT_FEATURES
from .cover import cover_device_info
from .hub import EleroHub

PRESETS = ("ventilation", "intermediate")


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub: EleroHub = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_COVER:
            continue
        async_add_entities(
            [EleroPresetButton(hub, subentry, preset) for preset in PRESETS],
            config_subentry_id=subentry_id,
        )


class EleroPresetButton(ButtonEntity):
    """Send one Elero preset command to a cover."""

    _attr_has_entity_name = True

    def __init__(self, hub: EleroHub, subentry: ConfigSubentry, preset: str) -> None:
        self._hub = hub
        self._subentry_id = subentry.subentry_id
        self._preset = preset
        channel = int(subentry.data[CONF_CHANNEL])
        self._attr_translation_key = preset
        self._attr_unique_id = f"{hub.serial_number}_{channel}_{preset}"
        self._attr_device_info = cover_device_info(hub, subentry)
        # Mostly useful for venetian blinds; enable by default only there.
        self._attr_entity_registry_enabled_default = bool(
            set(subentry.data.get("supported_features", ())) & TILT_FEATURES
        )

    async def async_press(self) -> None:
        cover = self._hub.covers.get(self._subentry_id)
        if cover is None or cover.hass is None:
            raise HomeAssistantError("Cover is not available")
        if self._preset == "ventilation":
            await cover.async_ventilation()
        else:
            await cover.async_intermediate()
