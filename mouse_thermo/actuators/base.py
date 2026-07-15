from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional


class Plug(ABC):
    @abstractmethod
    def set(self, on: bool) -> None:
        """Command the plug. Must raise on failure -- never silently no-op."""

    @abstractmethod
    def state(self) -> Optional[bool]:
        """Last CONFIRMED state reported by the device, None if unknown."""

    def power_w(self) -> Optional[float]:
        """Actuator feedback: did the lamp actually draw current? None if unsupported."""
        return None

    def close(self) -> None:
        pass
