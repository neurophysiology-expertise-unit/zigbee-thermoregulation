"""Independent thread that kills the lamp if the main loop stalls.

This is the SOFTWARE watchdog. It does not protect against process death,
power loss to the host, or Zigbee link failure. Those need hardware.
"""
from __future__ import annotations
import logging, threading, time
from typing import Callable

log = logging.getLogger(__name__)


class Watchdog(threading.Thread):
    def __init__(self, timeout_s: float, on_timeout: Callable[[], None]):
        super().__init__(name="watchdog", daemon=True)
        self.timeout_s = timeout_s
        self.on_timeout = on_timeout
        self._last_kick = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.tripped = False

    def kick(self) -> None:
        with self._lock:
            self._last_kick = time.monotonic()

    def run(self) -> None:
        while not self._stop.wait(0.5):
            with self._lock:
                age = time.monotonic() - self._last_kick
            if age > self.timeout_s and not self.tripped:
                self.tripped = True
                log.critical("WATCHDOG: main loop stalled %.1fs -- forcing lamp OFF", age)
                try:
                    self.on_timeout()
                except Exception:
                    log.exception("watchdog failed to turn lamp off")

    def stop(self) -> None:
        self._stop.set()
