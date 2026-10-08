"""Per-stick command queue between Home Assistant and the blocking transmitter.

The stick handles one request at a time and every request blocks until the
drive answers (or retries time out). All traffic for a stick therefore goes
through a single worker that runs requests in the executor, ordered by
priority: STOP first, then movement commands, then status polls. Duplicate
polls for a channel are merged. Device responses arrive on the executor
thread and are handed to the cover on the event loop.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .transmitter import EleroTransmitter

_LOGGER = logging.getLogger(__name__)

PRIORITY_STOP = 0
PRIORITY_COMMAND = 1
PRIORITY_POLL = 2

# A poll that hits a silent drive should not hold up the queue for long.
POLL_ATTEMPTS = 2

COMMANDS = {"up", "down", "stop", "intermediate", "ventilation_tilting"}


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    func: Callable[[], Any] = field(compare=False)
    future: asyncio.Future | None = field(compare=False, default=None)
    poll_channel: int | None = field(compare=False, default=None)


class EleroHub:
    """Owns one transmitter stick and serialises all access to it."""

    def __init__(self, hass: HomeAssistant, transmitter: EleroTransmitter) -> None:
        self.hass = hass
        self.transmitter = transmitter
        self.serial_number: str = transmitter.get_serial_number()
        self.hub_device_id: str | None = None
        # Cover entities by sub-entry id, for the button platform.
        self.covers: dict[str, Any] = {}
        self._listeners: dict[int, Callable[[str], None]] = {}
        self._queue: asyncio.PriorityQueue[_Job] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._pending_polls: set[int] = set()

    @callback
    def async_start(self, entry: ConfigEntry) -> None:
        entry.async_create_background_task(
            self.hass, self._async_worker(), f"elero {self.serial_number} queue"
        )

    def learned_channels(self) -> tuple[int, ...]:
        return self.transmitter.get_learned_channels()

    @callback
    def async_register(self, channel: int, listener: Callable[[str], None]) -> bool:
        """Route status reports for `channel` to `listener` (on the event loop).

        Returns False if the channel is not taught-in on the stick.
        """
        self._listeners[channel] = listener

        def _from_executor(response: dict) -> None:
            status = response.get("status")
            if status is not None:
                self.hass.loop.call_soon_threadsafe(self._dispatch, channel, status)

        return self.transmitter.set_channel(channel, _from_executor)

    @callback
    def async_unregister(self, channel: int) -> None:
        self._listeners.pop(channel, None)

    @callback
    def _dispatch(self, channel: int, status: str) -> None:
        if listener := self._listeners.get(channel):
            listener(status)

    async def async_command(self, channel: int, command: str) -> None:
        """Send a movement command and wait until the stick has handled it."""
        if command not in COMMANDS:
            raise ValueError(command)
        future = self.hass.loop.create_future()
        priority = PRIORITY_STOP if command == "stop" else PRIORITY_COMMAND
        func = getattr(self.transmitter, command)
        self._queue.put_nowait(
            _Job(priority, next(self._seq), lambda: func(channel), future)
        )
        await future

    @callback
    def async_request_poll(self, channel: int) -> None:
        """Queue a status request for `channel` unless one is already pending."""
        if channel in self._pending_polls:
            return
        self._pending_polls.add(channel)
        self._queue.put_nowait(
            _Job(
                PRIORITY_POLL,
                next(self._seq),
                lambda: self.transmitter.info(channel, POLL_ATTEMPTS),
                poll_channel=channel,
            )
        )

    @callback
    def async_request_check(self) -> None:
        """Queue an Easy Check (keep-alive / learned-channel refresh)."""
        self._queue.put_nowait(
            _Job(PRIORITY_POLL, next(self._seq), self.transmitter.check)
        )

    async def _async_worker(self) -> None:
        try:
            await self._async_run()
        finally:
            # Unloading cancels the worker; don't leave callers waiting.
            while not self._queue.empty():
                job = self._queue.get_nowait()
                if job.future and not job.future.done():
                    job.future.cancel()

    async def _async_run(self) -> None:
        while True:
            job = await self._queue.get()
            if job.poll_channel is not None:
                self._pending_polls.discard(job.poll_channel)
            try:
                await self.hass.async_add_executor_job(job.func)
            except Exception as exc:  # noqa: BLE001 - keep the queue alive
                _LOGGER.exception("Elero %s request failed", self.serial_number)
                if job.future and not job.future.done():
                    job.future.set_exception(exc)
            else:
                if job.future and not job.future.done():
                    job.future.set_result(None)
