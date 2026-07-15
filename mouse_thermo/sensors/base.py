"""Sensor source interface. Every reader is a thread that push()es into a channel."""
from __future__ import annotations
import logging, threading
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class SensorSource(ABC, threading.Thread):
    def __init__(self, name: str):
        threading.Thread.__init__(self, name=name, daemon=True)
        self.name_ = name
        self._stop = threading.Event()
        self.last_error = None

    @abstractmethod
    def read_loop(self) -> None:
        """Blocking loop: read device, push to channel(s), until self._stop set."""

    def run(self) -> None:
        try:
            self.read_loop()
        except Exception as e:                       # noqa: BLE001
            # Do NOT swallow. Record, log loudly. The channel then goes stale,
            # and staleness is what makes the controller fail cold.
            self.last_error = repr(e)
            log.exception("sensor %s died", self.name_)

    def stop(self) -> None:
        self._stop.set()
