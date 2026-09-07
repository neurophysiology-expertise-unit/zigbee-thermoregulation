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
    # allow_heat is a historical name: it means "allow the actuator ON". In
    # heat mode that is the lamp; in cool mode it is the Peltier. Either way a
    # False here forces the actuator OFF -- this layer NEVER commands it on.
    allow_heat: bool
    reason: str
    latched: bool = False


class SafetySupervisor:
    def __init__(self, cfg: SafetyConfig, mode: str = "heat"):
        # mode mirrors ControlConfig.mode. "heat" -> hard limits are ceilings;
        # "cool" -> hard limits are floors. Wired from cfg.control.mode in
        # main.run(); validate() guarantees the two agree.
        self.cfg = cfg
        self.mode = mode
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

        # 1. Hard limits -> latch. Ceilings in heat mode, floors in cool mode.
        if self.mode == "cool":
            if ambient is not None and ambient.value <= c.ambient_min_c:
                return self._latch(
                    f"ambient {ambient.value:.2f}C <= hard min {c.ambient_min_c}C"
                )
            if body is not None and body.value <= c.body_min_c:
                return self._latch(
                    f"body {body.value:.2f}C <= hard min {c.body_min_c}C"
                )
        else:
            if ambient is not None and ambient.value >= c.ambient_max_c:
                return self._latch(
                    f"ambient {ambient.value:.2f}C >= hard max {c.ambient_max_c}C"
                )
            if body is not None and body.value >= c.body_max_c:
                return self._latch(
                    f"body {body.value:.2f}C >= hard max {c.body_max_c}C"
                )

        # 2. Stuck-on detector -> latch. Applies in BOTH modes: an actuator
        #    commanded ON far too long without reaching setpoint means a dead
        #    element / mis-sited probe / stuck sensor -- runaway hot (heat) or
        #    runaway cold (cool). Same mechanism, same sticky latch.
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
        """Release requires positive evidence we are back inside the safe band,
        not merely absence of evidence. Heat mode: evidence of COOLING (below
        ceiling - hysteresis). Cool mode: evidence of WARMING (above floor +
        hysteresis)."""
        if self._latch_sticky:
            return False  # requires operator reset_latch()
        c = self.cfg
        h = c.lockout_release_hysteresis_c

        if ambient is None:
            return False  # no fresh ambient -> stay latched

        if self.mode == "cool":
            if ambient.value < c.ambient_min_c + h:
                return False
            if body is not None and body.value < c.body_min_c + h:
                return False
            return True
        else:
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
