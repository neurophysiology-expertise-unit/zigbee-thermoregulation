"""Simulated plug + simulated thermal plant, for testing safety logic with no animal."""
from __future__ import annotations
import time
from typing import Optional
from .base import Plug


class DummyPlug(Plug):
    def __init__(self, ambient_start=22.0, body_start=36.0, mode="heat"):
        self._on = False
        self._commanded = None
        self.ambient = ambient_start
        self.body = body_start
        self.mode = mode          # "heat": ON warms; "cool": ON chills (Peltier)
        self.log = []

    def set(self, on: bool) -> None:
        if on != self._on:
            self.log.append((time.monotonic(), on))
        self._on = on
        self._commanded = on

    def state(self) -> Optional[bool]:
        return self._on

    def commanded(self) -> Optional[bool]:
        # A simulated plug always "confirms" instantly, so these coincide --
        # unlike real hardware, where a confirmation may never arrive.
        return self._commanded

    def power_w(self) -> Optional[float]:
        return 150.0 if self._on else 0.4

    def tick(self, dt: float, room=21.0) -> None:
        """Crude first-order plant. In heat mode the actuator drives ambient
        UP; in cool mode (Peltier) it drives ambient DOWN, below room. Either
        way ambient relaxes toward room when off, and body drags after ambient."""
        if self._on:
            drive = -0.35 if self.mode == "cool" else 0.35
        else:
            drive = 0.0
        self.ambient += (drive - 0.03 * (self.ambient - room)) * dt
        self.body += 0.02 * (self.ambient + 12.0 - self.body) * dt
