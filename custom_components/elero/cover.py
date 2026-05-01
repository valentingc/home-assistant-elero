"""Support for Elero cover components."""

from __future__ import annotations

__version__ = "4.0.0"

import logging
import time
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverEntity,
    CoverEntityFeature,
    PLATFORM_SCHEMA as COVER_PLATFORM_SCHEMA,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_COVERS, CONF_DEVICE_CLASS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import _legacy_lookup_transmitter
from .const import (
    CONF_CHANNEL,
    CONF_SUPPORTED_FEATURES,
    CONF_TILT_STEP,
    CONF_TRANSMITTER_SERIAL_NUMBER,
    CONF_TRAVEL_TIME,
    DEFAULT_TILT_STEP,
    DEFAULT_TRAVEL_TIME,
    DOMAIN,
    ELERO_COVER_DEVICE_CLASSES,
    INFO_BLOCKING,
    INFO_BOTTOM_POSITION_STOP,
    INFO_BOTTOM_POS_STOP_WICH_INT_POS,
    INFO_INTERMEDIATE_POSITION_STOP,
    INFO_MOVING_DOWN,
    INFO_MOVING_UP,
    INFO_NO_INFORMATION,
    INFO_OVERHEATED,
    INFO_START_TO_MOVE_DOWN,
    INFO_START_TO_MOVE_UP,
    INFO_STOPPED_IN_UNDEFINED_POSITION,
    INFO_SWITCHING_DEVICE_SWITCHED_OFF,
    INFO_SWITCHING_DEVICE_SWITCHED_ON,
    INFO_TILT_VENTILATION_POS_STOP,
    INFO_TIMEOUT,
    INFO_TOP_POSITION_STOP,
    INFO_TOP_POS_STOP_WICH_TILT_POS,
    POSITION_CLOSED,
    POSITION_INTERMEDIATE,
    POSITION_OPEN,
    POSITION_TILT_VENTILATION,
    SUBENTRY_TYPE_COVER,
)

_LOGGER = logging.getLogger(__name__)

ATTR_ELERO_STATE = "elero_state"

SUPPORTED_FEATURES = {
    "close_tilt": CoverEntityFeature.CLOSE_TILT,
    "down": CoverEntityFeature.CLOSE,
    "open_tilt": CoverEntityFeature.OPEN_TILT,
    "set_position": CoverEntityFeature.SET_POSITION,
    "set_tilt_position": CoverEntityFeature.SET_TILT_POSITION,
    "stop_tilt": CoverEntityFeature.STOP_TILT,
    "stop": CoverEntityFeature.STOP,
    "up": CoverEntityFeature.OPEN,
}

# ── Legacy YAML schema (still accepted, but new users should use the UI) ──

ELERO_COVER_DEVICE_CLASSES_SCHEMA = vol.All(
    vol.Lower, vol.In(ELERO_COVER_DEVICE_CLASSES)
)
SUPPORTED_FEATURES_SCHEMA = vol.All(cv.ensure_list, [vol.In(SUPPORTED_FEATURES)])
CHANNEL_NUMBERS_SCHEMA = vol.All(vol.Coerce(int), vol.Range(min=1, max=15))

COVER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CHANNEL): CHANNEL_NUMBERS_SCHEMA,
        vol.Required(CONF_DEVICE_CLASS): ELERO_COVER_DEVICE_CLASSES_SCHEMA,
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_SUPPORTED_FEATURES): SUPPORTED_FEATURES_SCHEMA,
        vol.Required(CONF_TRANSMITTER_SERIAL_NUMBER): str,
        vol.Optional(CONF_TRAVEL_TIME, default=DEFAULT_TRAVEL_TIME): vol.Coerce(float),
        vol.Optional(CONF_TILT_STEP, default=DEFAULT_TILT_STEP): vol.Coerce(float),
    }
)

PLATFORM_SCHEMA = COVER_PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_COVERS): vol.Schema({cv.slug: COVER_SCHEMA})}
)


# ── Setup hooks ─────────────────────────────────────────────────────────


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up the Elero cover platform from legacy YAML.

    Skips covers that have already been auto-imported as a sub-entry of the
    matching ConfigEntry — the modern (subentry) path takes precedence.
    """
    covers = []
    covers_conf = config.get(CONF_COVERS, {})
    for _, cover_conf in covers_conf.items():
        serial = cover_conf.get(CONF_TRANSMITTER_SERIAL_NUMBER)
        channel = cover_conf.get(CONF_CHANNEL)
        if _has_subentry_for_channel(hass, serial, channel):
            _LOGGER.debug(
                "Skipping legacy YAML cover '%s' ch %s — already a sub-entry",
                cover_conf.get(CONF_NAME),
                channel,
            )
            continue

        transmitter = _legacy_lookup_transmitter(hass, serial)
        if not transmitter:
            _LOGGER.error(
                "The transmitter '%s' of channel '%s' - '%s' is non-existent!",
                serial,
                channel,
                cover_conf.get(CONF_NAME),
            )
            continue
        covers.append(
            EleroCover(
                hass=hass,
                transmitter=transmitter,
                name=cover_conf[CONF_NAME],
                channel=channel,
                device_class=cover_conf[CONF_DEVICE_CLASS],
                supported_features=cover_conf[CONF_SUPPORTED_FEATURES],
                travel_time=cover_conf[CONF_TRAVEL_TIME],
                tilt_step=cover_conf.get(CONF_TILT_STEP, DEFAULT_TILT_STEP),
                unique_suffix=str(channel),
            )
        )
    add_devices(covers, True)


def _has_subentry_for_channel(hass, serial_number, channel) -> bool:
    """Return True if any ConfigEntry already has a cover subentry for this channel."""
    if not serial_number or channel is None:
        return False
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        return False
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_TRANSMITTER_SERIAL_NUMBER) != serial_number:
            continue
        for sub in entry.subentries.values():
            if (
                sub.subentry_type == SUBENTRY_TYPE_COVER
                and int(sub.data.get(CONF_CHANNEL, -1)) == ch
            ):
                return True
    return False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Elero covers from sub-entries of a config entry."""
    transmitter = hass.data[DOMAIN][entry.entry_id]
    serial = transmitter.get_serial_number()

    by_subentry: dict[str, list[EleroCover]] = {}
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_COVER:
            continue
        cover = EleroCover(
            hass=hass,
            transmitter=transmitter,
            name=subentry.data[CONF_NAME],
            channel=int(subentry.data[CONF_CHANNEL]),
            device_class=subentry.data[CONF_DEVICE_CLASS],
            supported_features=subentry.data[CONF_SUPPORTED_FEATURES],
            travel_time=float(
                subentry.data.get(CONF_TRAVEL_TIME, DEFAULT_TRAVEL_TIME)
            ),
            tilt_step=float(subentry.data.get(CONF_TILT_STEP, DEFAULT_TILT_STEP)),
            unique_suffix=str(int(subentry.data[CONF_CHANNEL])),
            hub_serial=serial,
        )
        by_subentry.setdefault(subentry_id, []).append(cover)

    for subentry_id, entities in by_subentry.items():
        async_add_entities(entities, True, config_subentry_id=subentry_id)


# ── Entity ──────────────────────────────────────────────────────────────


class EleroCover(CoverEntity, RestoreEntity):
    """Representation of an Elero cover device.

    Position tracking uses time-based interpolation: when the cover is moving,
    the current position is calculated from the start position, direction,
    elapsed time, and configured travel_time. Known stop events from the radio
    (top / bottom / intermediate / tilt) override the estimate with the exact
    position.
    """

    _attr_should_poll = True

    async def async_added_to_hass(self):
        """Restore state on HA startup."""
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if not state:
            return
        self._position = state.attributes.get("current_position")
        self._tilt_position = state.attributes.get("current_tilt_position")
        self._elero_state = state.attributes.get(ATTR_ELERO_STATE)
        if self._position is not None:
            self._closed = self._position == 0
        _LOGGER.debug(
            "Restored state for %s: position=%s", self._attr_name, self._position
        )

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        transmitter,
        name: str,
        channel: int,
        device_class: str,
        supported_features: list[str],
        travel_time: float,
        tilt_step: float = DEFAULT_TILT_STEP,
        unique_suffix: str | None = None,
        hub_serial: str | None = None,
    ):
        self.hass = hass
        self._transmitter = transmitter
        self._channel = channel
        self._tilt_step = tilt_step

        serial = hub_serial or transmitter.get_serial_number()
        suffix = unique_suffix or str(channel)
        self._attr_name = name
        self._attr_unique_id = f"{serial}_{suffix}"
        self._attr_device_class = ELERO_COVER_DEVICE_CLASSES[device_class]

        feature_mask = 0
        for f in supported_features:
            feature_mask |= SUPPORTED_FEATURES[f]
        self._attr_supported_features = feature_mask

        # Each cover is its own device under the transmitter hub.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{serial}_{channel}")},
            name=name,
            manufacturer="Elero",
            model=device_class,
            via_device=(DOMAIN, serial),
        )

        self._available = self._transmitter.set_channel(
            self._channel, self.response_handler
        )

        # Core state
        self._position: int | None = None  # 0 = closed, 100 = open
        self._tilt_position: int | None = None
        self._is_opening = False
        self._is_closing = False
        self._closed: bool | None = None
        self._elero_state: str | None = None
        self._response: dict = {}

        # Travel-time position tracking
        self._travel_time = travel_time
        self._move_start_time: float | None = None
        self._move_start_position: int | None = None
        self._move_direction = 0

        # Handle for a scheduled stop (used by set_cover_position)
        self._scheduled_stop = None

        # After a manual tilt-step, ignore the device's "tilt ventilation"
        # stop response until this timestamp.
        self._tilt_step_lock_until = 0.0

    # ── HA entity properties ────────────────────────────────────────────

    @property
    def available(self):
        return self._available

    @property
    def current_cover_position(self):
        """Return current position, interpolating in real-time while moving."""
        if (
            self._move_start_time is not None
            and self._move_start_position is not None
        ):
            elapsed = time.time() - self._move_start_time
            delta = (elapsed / self._travel_time) * 100.0 * self._move_direction
            return max(0, min(100, round(self._move_start_position + delta)))
        return self._position

    @property
    def current_cover_tilt_position(self):
        return self._tilt_position

    @property
    def is_opening(self):
        return self._is_opening

    @property
    def is_closing(self):
        return self._is_closing

    @property
    def is_closed(self):
        return self._closed

    @property
    def extra_state_attributes(self):
        data: dict[str, Any] = {}
        if self._elero_state is not None:
            data[ATTR_ELERO_STATE] = self._elero_state
        data["channel"] = self._channel
        data["travel_time"] = self._travel_time
        data["tilt_step"] = self._tilt_step
        data["move_start_position"] = self._move_start_position
        tx = self._transmitter
        data["last_command_ts"] = tx.last_command_ts
        data["last_response_ts"] = tx.last_response_ts
        data["error_count"] = tx.error_count
        data["timeout_count"] = tx.timeout_count
        data["reconnect_count"] = tx.reconnect_count
        data["checksum_error_count"] = tx.checksum_error_count
        data["consecutive_failures"] = tx.consecutive_failures
        return data

    # ── movement helpers ────────────────────────────────────────────────

    def _cancel_scheduled_stop(self):
        if self._scheduled_stop is not None:
            self._scheduled_stop.cancel()
            self._scheduled_stop = None

    def _start_moving(self, direction):
        """Begin tracking a movement.  direction: +1 (open) or -1 (close)."""
        self._cancel_scheduled_stop()
        current = self.current_cover_position
        self._move_start_time = time.time()
        self._move_start_position = (
            current if current is not None else (0 if direction > 0 else 100)
        )
        self._move_direction = direction
        self._is_opening = direction > 0
        self._is_closing = direction < 0
        self._closed = False
        _LOGGER.debug(
            "%s: start moving %s from position %s",
            self._attr_name,
            "up" if direction > 0 else "down",
            self._move_start_position,
        )

    def _stop_moving(self, final_position=None):
        """Finalise a movement."""
        self._cancel_scheduled_stop()
        if final_position is not None:
            self._position = final_position
        elif self._move_start_time is not None:
            self._position = self.current_cover_position
        self._move_start_time = None
        self._move_start_position = None
        self._move_direction = 0
        self._is_opening = False
        self._is_closing = False
        if self._position is not None:
            self._closed = self._position == 0
        _LOGGER.debug(
            "%s: stopped at position %s", self._attr_name, self._position
        )

    # ── cover commands ──────────────────────────────────────────────────

    def update(self):
        """Poll the device for its current state."""
        self._transmitter.info(self._channel)

    def open_cover(self, **kwargs):
        self._transmitter.up(self._channel)
        self._start_moving(+1)
        self.hass.loop.call_later(self._travel_time + 1, self.update)

    def close_cover(self, **kwargs):
        self._transmitter.down(self._channel)
        self._start_moving(-1)
        self.hass.loop.call_later(self._travel_time + 1, self.update)

    def stop_cover(self, **kwargs):
        self._transmitter.stop(self._channel)
        self._stop_moving()

    def set_cover_position(self, **kwargs):
        """Move to a specific position (time-based approximation)."""
        target = kwargs.get(ATTR_POSITION)
        if target is None:
            return
        target = max(0, min(100, target))

        current = self.current_cover_position
        if current is None:
            _LOGGER.warning(
                "%s: position unknown — opening fully to calibrate", self._attr_name
            )
            self.open_cover()
            self._scheduled_stop = self.hass.loop.call_later(
                self._travel_time + 2,
                lambda: self.set_cover_position(position=target),
            )
            return

        diff = target - current
        if abs(diff) < 2:
            return

        move_time = abs(diff) / 100.0 * self._travel_time

        if diff > 0:
            self._transmitter.up(self._channel)
            self._start_moving(+1)
        else:
            self._transmitter.down(self._channel)
            self._start_moving(-1)

        def _finish_move():
            _LOGGER.debug(
                "%s: timed move complete — stopping at target %s",
                self._attr_name,
                target,
            )
            self.hass.async_add_executor_job(self._execute_timed_stop, target)

        self._scheduled_stop = self.hass.loop.call_later(move_time, _finish_move)

    def _execute_timed_stop(self, target):
        self._transmitter.stop(self._channel)
        self._stop_moving(final_position=target)
        self._scheduled_stop = None

    def cover_ventilation_tilting_position(self, **kwargs):
        self._transmitter.ventilation_tilting(self._channel)
        self._cancel_scheduled_stop()

    def cover_intermediate_position(self, **kwargs):
        self._transmitter.intermediate(self._channel)
        self._cancel_scheduled_stop()

    def close_cover_tilt(self, **kwargs):
        """Tilt the slats slightly.

        On many Elero remotes the ventilation/tilting button is reprogrammed
        to perform a small slat tilt rather than a move to a fixed position.
        If ``tilt_step`` is configured (>0), nudge the tracked position by
        that many percent (capped at 100, no-op if already fully open).
        """
        self.cover_ventilation_tilting_position()
        if (
            self._tilt_step > 0
            and self._position is not None
            and self._position < POSITION_OPEN
        ):
            new_pos = max(0, min(100, round(self._position + self._tilt_step)))
            self._position = new_pos
            self._closed = new_pos == 0
            self._move_start_time = None
            self._move_start_position = None
            self._move_direction = 0
            self._is_opening = False
            self._is_closing = False
            self._tilt_step_lock_until = time.time() + 10.0
            _LOGGER.debug(
                "%s: tilt step (+%s%%) applied — position now %s",
                self._attr_name,
                self._tilt_step,
                new_pos,
            )

    def open_cover_tilt(self, **kwargs):
        self.cover_intermediate_position()

    def stop_cover_tilt(self, **kwargs):
        self.stop_cover()

    def set_cover_tilt_position(self, **kwargs):
        tilt_position = kwargs.get(ATTR_TILT_POSITION)
        if tilt_position is None:
            return
        if tilt_position < 50:
            self.cover_ventilation_tilting_position()
        else:
            self.cover_intermediate_position()

    # ── response handling ───────────────────────────────────────────────

    def response_handler(self, response):
        """Callback invoked by the transmitter with a device response."""
        self._response = response
        self._set_states()

    def _set_states(self):
        """Update cover state from the last device response."""
        status = self._response.get("status")
        if status is None:
            return

        self._elero_state = status
        _LOGGER.debug(
            "%s ch %s: status=%s", self._attr_name, self._channel, status
        )

        if status == INFO_TOP_POSITION_STOP:
            self._stop_moving(final_position=POSITION_OPEN)
            self._tilt_position = None
            self._closed = False

        elif status == INFO_BOTTOM_POSITION_STOP:
            self._stop_moving(final_position=POSITION_CLOSED)
            self._tilt_position = None
            self._closed = True

        elif status == INFO_INTERMEDIATE_POSITION_STOP:
            self._stop_moving(final_position=POSITION_INTERMEDIATE)
            self._tilt_position = POSITION_INTERMEDIATE
            self._closed = False

        elif status == INFO_TILT_VENTILATION_POS_STOP:
            if time.time() < self._tilt_step_lock_until:
                _LOGGER.debug(
                    "%s: ignoring TILT_VENTILATION_POS_STOP — tilt_step lock active",
                    self._attr_name,
                )
            else:
                self._stop_moving(final_position=POSITION_TILT_VENTILATION)
                self._tilt_position = POSITION_TILT_VENTILATION
                self._closed = False

        elif status == INFO_TOP_POS_STOP_WICH_TILT_POS:
            if time.time() < self._tilt_step_lock_until:
                _LOGGER.debug(
                    "%s: ignoring TOP_POS_STOP_WICH_TILT_POS — tilt_step lock active",
                    self._attr_name,
                )
            else:
                self._stop_moving(final_position=POSITION_TILT_VENTILATION)
                self._tilt_position = POSITION_TILT_VENTILATION
                self._closed = False

        elif status == INFO_BOTTOM_POS_STOP_WICH_INT_POS:
            self._stop_moving(final_position=POSITION_INTERMEDIATE)
            self._tilt_position = POSITION_INTERMEDIATE
            self._closed = False

        elif status in (INFO_START_TO_MOVE_UP, INFO_MOVING_UP):
            if self._move_start_time is None:
                self._start_moving(+1)
            self._tilt_position = None

        elif status in (INFO_START_TO_MOVE_DOWN, INFO_MOVING_DOWN):
            if self._move_start_time is None:
                self._start_moving(-1)
            self._tilt_position = None

        elif status == INFO_STOPPED_IN_UNDEFINED_POSITION:
            self._stop_moving()
            self._tilt_position = None

        elif status == INFO_NO_INFORMATION:
            self._stop_moving()
            self._position = None
            self._tilt_position = None
            self._closed = None

        elif status in (INFO_BLOCKING, INFO_OVERHEATED, INFO_TIMEOUT):
            self._stop_moving()
            self._position = None
            self._tilt_position = None
            self._closed = None
            _LOGGER.error(
                "Transmitter '%s' ch %s error: %s",
                self._transmitter.get_serial_number(),
                self._channel,
                status,
            )

        elif status in (
            INFO_SWITCHING_DEVICE_SWITCHED_ON,
            INFO_SWITCHING_DEVICE_SWITCHED_OFF,
        ):
            self._stop_moving()
            self._position = None
            self._tilt_position = None
            self._closed = None

        else:
            self._stop_moving()
            self._position = None
            self._tilt_position = None
            self._closed = None
            _LOGGER.error(
                "Transmitter '%s' ch %s unhandled response: %s",
                self._transmitter.get_serial_number(),
                self._channel,
                status,
            )
