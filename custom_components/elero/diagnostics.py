"""Diagnostics for Elero: stick health and per-cover model state."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_REMOTE_TRANSMITTERS_ADDRESS
from .hub import EleroHub

TO_REDACT = {CONF_REMOTE_TRANSMITTERS_ADDRESS}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry
) -> dict[str, Any]:
    hub: EleroHub = entry.runtime_data
    tx = hub.transmitter
    covers = {}
    for subentry_id, cover in hub.covers.items():
        current = cover._current()  # noqa: SLF001 - diagnostics only
        covers[subentry_id] = {
            "entity_id": cover.entity_id,
            "options": {
                **asdict(cover._opts),  # noqa: SLF001
                "features": sorted(cover._opts.features),  # noqa: SLF001
            },
            "learned": dict(cover._learned),  # noqa: SLF001
            "position": current.position,
            "tilt": current.tilt,
            "moving": None if cover._move is None else cover._move.as_dict(),  # noqa: SLF001
            "elero_state": cover._elero_state,  # noqa: SLF001
            "available": cover.available,
        }
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "transmitter": {
            "serial_number": hub.serial_number,
            "learned_channels": list(hub.learned_channels()),
            "last_command_ts": tx.last_command_ts,
            "last_response_ts": tx.last_response_ts,
            "error_count": tx.error_count,
            "timeout_count": tx.timeout_count,
            "reconnect_count": tx.reconnect_count,
            "checksum_error_count": tx.checksum_error_count,
            "consecutive_failures": tx.consecutive_failures,
        },
        "covers": covers,
    }
