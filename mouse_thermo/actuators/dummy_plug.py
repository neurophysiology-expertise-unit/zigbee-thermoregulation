"""Simulated plug + simulated thermal plant, for testing safety logic with no animal."""
from __future__ import annotations
import time
from typing import Optional
from .base import Plug


class DummyPlug(Plug):
    def __init__(self, ambient_start=22.0, body_start=36.0):
        self._on = False
        self.ambient = ambient_start
        self.body = body_start
        self.log = []

    def set(self, on: bool) -> None:
        if on != self._on:
            self.log.append((time.monotonic(), on))
        self._on = on

    def state(self) -> Optional[bool]:
        return self._on

    def power_w(self) -> Optional[float]:
        return 150.0 if self._on else 0.4

    def tick(self, dt: float, room=21.0) -> None:
        """Crude first-order plant: lamp heats ambient, ambient drags body."""
        gain = 0.35 if self._on else 0.0
        self.ambient += (gain * 1.0 - 0.03 * (self.ambient - room)) * dt
        self.body += 0.02 * (self.ambient + 12.0 - self.body) * dt
