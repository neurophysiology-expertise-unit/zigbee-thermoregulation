from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional


class Plug(ABC):
    @abstractmethod
    def set(self, on: bool) -> None:
        """Command the plug from OUTSIDE the event loop thread (e.g. the
        Watchdog, which runs on its own thread). Must raise on failure --
        never silently no-op."""

    async def set_async(self, on: bool) -> None:
        """Command the plug from CODE ALREADY RUNNING ON THE EVENT LOOP.

        Do not call set() from there instead: implementations that bridge to
        an asyncio backend (e.g. ZigbeePlug) do so via
        run_coroutine_threadsafe + a blocking future.result(), which
        deadlocks if called from the same loop/thread it targets. This
        default just delegates to the synchronous set(), which is correct
        for actuators (like DummyPlug) with no real event-loop bridging.
        """
        self.set(on)

    @abstractmethod
    def state(self) -> Optional[bool]:
        """Last CONFIRMED state reported by the device, None if unknown."""

    def power_w(self) -> Optional[float]:
        """Actuator feedback: did the lamp actually draw current? None if unsupported."""
        return None

    def last_seen_age(self, now: float) -> Optional[float]:
        """Seconds since the device last sent ANYTHING (any packet, not just
        an on/off report) -- a link-health signal distinct from state(),
        which only reflects the last CONFIRMED on/off value. None if
        unsupported (e.g. DummyPlug) or nothing has been heard yet.
        `now` must be time.time() (wall clock), not time.monotonic() --
        zigpy's last_seen is a Unix timestamp.
        """
        return None

    def close(self) -> None:
        pass
