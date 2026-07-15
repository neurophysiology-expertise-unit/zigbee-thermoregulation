"""Safety supervisor. Evaluated FIRST every cycle; can only ever force OFF.

This layer never turns the lamp on. It only vetoes. That asymmetry is the
whole point: a bug here fails cold, not hot.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .bus import Reading
from .config import SafetyConfig


@dataclass(frozen=True)
class Verdict:
    allow_heat: bool
    reason: str
    latched: bool = False


class SafetySupervisor:
    def __init__(self, cfg: SafetyConfig):
        self.cfg = cfg
        self._latched = False
        self._latch_reason = ""
        self._latch_sticky = False
        self._on_since: Optional[float] = None

    # -- lamp-on bookkeeping -------------------------------------------------
    def note_lamp_command(self, on: bool, now: float) -> None:
        if on:
            if self._on_since is None:
                self._on_since = now
        else:
            self._on_since = None

    def continuous_on_s(self, now: float) -> float:
        return 0.0 if self._on_since is None else now - self._on_since

    # -- main evaluation -----------------------------------------------------
    def evaluate(
        self,
        body: Optional[Reading],
        ambient: Optional[Reading],
        now: Optional[float] = None,
    ) -> Verdict:
        now = now if now is not None else time.monotonic()
        c = self.cfg

        # 1. Hard ceilings -> latch.
        if ambient is not None and ambient.value >= c.ambient_max_c:
            return self._latch(
                f"ambient {ambient.value:.2f}C >= hard max {c.ambient_max_c}C"
            )
        if body is not None and body.value >= c.body_max_c:
            return self._latch(
                f"body {body.value:.2f}C >= hard max {c.body_max_c}C"
            )

        # 2. Stuck-on detector -> latch. Dead bulb / mis-sited probe / stuck sensor.
        if self.continuous_on_s(now) > c.max_continuous_on_s:
            return self._latch(
                f"lamp ON continuously for {self.continuous_on_s(now):.0f}s "
                f"(> {c.max_continuous_on_s}s) without reaching setpoint",
                sticky=True,
            )

        # 3. Latch release: only once we are clearly back below the ceiling.
        if self._latched:
            if self._can_release(body, ambient):
                self._latched = False
                self._latch_reason = ""
            else:
                return Verdict(False, f"LATCHED: {self._latch_reason}", latched=True)

        # 4. Blind operation -> veto (not latched; recovers when a sensor returns).
        if body is None and ambient is None:
            return Verdict(
                False,
                f"no usable temperature source (both stale/implausible)",
            )

        return Verdict(True, "ok")

    def _latch(self, reason: str, sticky: bool = False) -> Verdict:
        if not self._latched:
            self._latched = True
            self._latch_reason = reason
            self._latch_sticky = sticky
        return Verdict(False, f"LATCHED: {self._latch_reason}", latched=True)

    def reset_latch(self) -> None:
        """Operator acknowledgement. Only call after physically checking the rig."""
        self._latched = False
        self._latch_reason = ""
        self._latch_sticky = False
        self._on_since = None

    def _can_release(
        self, body: Optional[Reading], ambient: Optional[Reading]
    ) -> bool:
        """Release requires positive evidence we are cool, not absence of evidence."""
        if self._latch_sticky:
            return False  # requires operator reset_latch()
        c = self.cfg
        h = c.lockout_release_hysteresis_c

        if ambient is None:
            return False  # no fresh ambient -> stay latched
        if ambient.value > c.ambient_max_c - h:
            return False
        if body is not None and body.value > c.body_max_c - h:
            return False
        return True

    @property
    def latched(self) -> bool:
        return self._latched

    @property
    def latch_reason(self) -> str:
        return self._latch_reason
