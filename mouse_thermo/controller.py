"""Control state machine.

  NORMAL   : fresh body temp -> regulate to body setpoint, ambient still capped
  FALLBACK : no body temp    -> regulate ambient only (your "second control")
  LOCKOUT  : safety veto     -> lamp off
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .bus import Reading
from .config import ControlConfig, SafetyConfig
from .safety import SafetySupervisor


class State(str, Enum):
    NORMAL = "NORMAL"
    FALLBACK = "FALLBACK"
    LOCKOUT = "LOCKOUT"


@dataclass
class Decision:
    lamp_on: bool
    state: State
    reason: str
    # True for a real hard-ceiling breach, stuck-on latch, or a latch not yet
    # released -- never overridable. False for the "blind operation, both
    # sensors stale" veto, which recovers on its own and (by operator choice)
    # can be overridden by manual control. See safety.Verdict.latched.
    latched: bool = False


class Controller:
    def __init__(self, ctrl: ControlConfig, safety: SafetySupervisor,
                 safety_cfg: SafetyConfig):
        self.cfg = ctrl
        self.safety = safety
        self.safety_cfg = safety_cfg
        self._cmd = False
        self._last_change: float = -1e9

    def _dwell_ok(self, want_on: bool, now: float) -> bool:
        """Anti-chatter. NOTE: never blocks an OFF demanded by safety --
        callers must bypass this path for lockout."""
        if want_on == self._cmd:
            return True
        held = now - self._last_change
        need = self.cfg.min_on_s if self._cmd else self.cfg.min_off_s
        return held >= need

    def _apply(self, want_on: bool, now: float, force: bool = False) -> bool:
        if want_on != self._cmd and (force or self._dwell_ok(want_on, now)):
            self._cmd = want_on
            self._last_change = now
        return self._cmd

    def step(
        self,
        body: Optional[Reading],
        ambient: Optional[Reading],
        now: Optional[float] = None,
    ) -> Decision:
        now = now if now is not None else time.monotonic()
        c = self.cfg

        # --- Safety first. Always. -----------------------------------------
        verdict = self.safety.evaluate(body, ambient, now)
        if not verdict.allow_heat:
            on = self._apply(False, now, force=True)   # OFF ignores dwell
            self.safety.note_lamp_command(on, now)
            return Decision(on, State.LOCKOUT, verdict.reason, latched=verdict.latched)

        # --- Ambient ceiling as a soft regulator, always active -------------
        # Even in NORMAL we refuse to heat if ambient is at/above its setpoint.
        ambient_blocks = (
            ambient is not None
            and ambient.value >= c.ambient_setpoint_c + c.ambient_deadband_c
        )

        if body is not None:
            state = State.NORMAL
            if ambient_blocks:
                want, why = False, (
                    f"ambient {ambient.value:.2f}C at cap "
                    f"{c.ambient_setpoint_c}C (+{c.ambient_deadband_c}) "
                    f"-- body {body.value:.2f}C not pursued"
                )
            elif body.value < c.body_setpoint_c - c.body_deadband_c:
                want, why = True, f"body {body.value:.2f}C < sp-db"
            elif body.value > c.body_setpoint_c + c.body_deadband_c:
                want, why = False, f"body {body.value:.2f}C > sp+db"
            else:
                want, why = self._cmd, f"body {body.value:.2f}C in deadband, hold"
        else:
            # No body temp: the chip was not read (animal away from antenna,
            # implant not reporting, RFID disabled). Regulate ambient only.
            state = State.FALLBACK
            if ambient is None:
                # Should be unreachable -- safety vetoes this. Crash loudly.
                raise RuntimeError(
                    "controller reached FALLBACK with no ambient reading; "
                    "safety supervisor should have vetoed. Refusing to guess."
                )
            if ambient.value < c.ambient_setpoint_c - c.ambient_deadband_c:
                want, why = True, f"fallback: ambient {ambient.value:.2f}C < sp-db"
            elif ambient.value > c.ambient_setpoint_c + c.ambient_deadband_c:
                want, why = False, f"fallback: ambient {ambient.value:.2f}C > sp+db"
            else:
                want, why = self._cmd, f"fallback: ambient in deadband, hold"

        on = self._apply(want, now)
        if on != want:
            why += " [dwell hold]"
        self.safety.note_lamp_command(on, now)
        return Decision(on, state, why)

    @property
    def commanded(self) -> bool:
        return self._cmd
