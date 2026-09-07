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

    def _regulate(self, value: float, setpoint: float, deadband: float,
                  label: str) -> tuple[bool, str]:
        """Hysteresis toward `setpoint`, direction set by mode.
          heat: actuator ON when value < sp-db, OFF when value > sp+db
          cool: actuator ON when value > sp+db, OFF when value < sp-db  (mirror)
        In-deadband holds the current command. Returns (want_on, reason)."""
        if self.cfg.mode == "cool":
            if value > setpoint + deadband:
                return True, f"{label} {value:.2f}C > sp+db"
            if value < setpoint - deadband:
                return False, f"{label} {value:.2f}C < sp-db"
        else:
            if value < setpoint - deadband:
                return True, f"{label} {value:.2f}C < sp-db"
            if value > setpoint + deadband:
                return False, f"{label} {value:.2f}C > sp+db"
        return self._cmd, f"{label} {value:.2f}C in deadband, hold"

    def step(
        self,
        body: Optional[Reading],
        ambient: Optional[Reading],
        now: Optional[float] = None,
        ground_truth: str = "auto",
    ) -> Decision:
        """ground_truth selects which reading REGULATES the lamp:
          "auto"    -- body if present, else ambient (original behaviour)
          "body"    -- regulate on body only; if body is absent, refuse to
                       heat (a non-latched LOCKOUT, self-correcting when body
                       returns). Chosen when body is the deliberate target.
          "ambient" -- regulate on ambient only, ignoring body entirely for
                       regulation. Used when body cannot be trusted during
                       heating (e.g. RFID killed by lamp EMI).

        Safety ALWAYS evaluates BOTH real readings regardless of this: a body
        hard-ceiling breach still fires even when regulating on ambient, and
        vice versa. ground_truth changes only which value the controller
        pursues, never what the safety supervisor is allowed to see.
        """
        now = now if now is not None else time.monotonic()
        c = self.cfg

        # --- Safety first. Always. On the TRUE readings, never the
        #     ground-truth-filtered ones. -----------------------------------
        verdict = self.safety.evaluate(body, ambient, now)
        if not verdict.allow_heat:
            on = self._apply(False, now, force=True)   # OFF ignores dwell
            self.safety.note_lamp_command(on, now)
            return Decision(on, State.LOCKOUT, verdict.reason, latched=verdict.latched)

        # --- Apply the ground-truth source restriction ---------------------
        # reg_body is what the regulator is allowed to pursue; None routes it
        # to ambient-only control. The chosen source going missing is a
        # non-latched LOCKOUT (fail cold, recovers on its own) rather than a
        # silent switch to the other source, which would violate the operator's
        # explicit choice of what "ground truth" means.
        if ground_truth == "body":
            if body is None:
                on = self._apply(False, now, force=True)
                self.safety.note_lamp_command(on, now)
                return Decision(on, State.LOCKOUT,
                                "ground_truth=body but no usable body reading",
                                latched=False)
            reg_body = body
        elif ground_truth == "ambient":
            if ambient is None:
                on = self._apply(False, now, force=True)
                self.safety.note_lamp_command(on, now)
                return Decision(on, State.LOCKOUT,
                                "ground_truth=ambient but no usable ambient reading",
                                latched=False)
            reg_body = None            # force the ambient-only regulation path
        else:  # "auto"
            reg_body = body

        # --- Ambient guard as a soft regulator, always active ---------------
        # heat: refuse to add heat if ambient is at/above its setpoint
        #       ("a cold mouse does not license an overheated box").
        # cool: mirror -- refuse to remove more heat if ambient is at/below its
        #       setpoint ("a warm mouse does not license an over-cooled box").
        if c.mode == "cool":
            ambient_blocks = (
                ambient is not None
                and ambient.value <= c.ambient_setpoint_c - c.ambient_deadband_c
            )
            ambient_edge = f"floor {c.ambient_setpoint_c}C (-{c.ambient_deadband_c})"
        else:
            ambient_blocks = (
                ambient is not None
                and ambient.value >= c.ambient_setpoint_c + c.ambient_deadband_c
            )
            ambient_edge = f"cap {c.ambient_setpoint_c}C (+{c.ambient_deadband_c})"

        if reg_body is not None:
            body = reg_body
            state = State.NORMAL
            if ambient_blocks:
                want, why = False, (
                    f"ambient {ambient.value:.2f}C at {ambient_edge} "
                    f"-- body {body.value:.2f}C not pursued"
                )
            else:
                want, why = self._regulate(
                    body.value, c.body_setpoint_c, c.body_deadband_c, "body")
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
            want, why = self._regulate(
                ambient.value, c.ambient_setpoint_c, c.ambient_deadband_c,
                "fallback: ambient")

        on = self._apply(want, now)
        if on != want:
            why += " [dwell hold]"
        self.safety.note_lamp_command(on, now)
        return Decision(on, state, why)

    @property
    def commanded(self) -> bool:
        return self._cmd
