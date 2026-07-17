"""One unified JSONL stream: every sensor, command, state and reason, one clock."""
from __future__ import annotations
import json, threading, time
from typing import Optional


class SessionLogger:
    def __init__(self, path: str, config_dump: dict):
        self._f = open(path, "a", buffering=1)
        self._lock = threading.Lock()
        self._t0_wall = time.time()
        self._t0_mono = time.monotonic()
        self.write({"type": "session_start",
                    "wall_clock": self._t0_wall,
                    "config": config_dump})

    def write(self, rec: dict) -> None:
        rec.setdefault("t_mono", time.monotonic())
        rec.setdefault("t_wall", self._t0_wall + (rec["t_mono"] - self._t0_mono))
        with self._lock:
            self._f.write(json.dumps(rec, default=str) + "\n")

    def sample(self, *, body, body_age, ambient, ambient_age,
               lamp_cmd, lamp_state, power_w, state, reason,
               controller_wanted=None, manual_override=False,
               record_mode=None, body_setpoint_c=None, ambient_setpoint_c=None,
               safety_bypass_active=False, lamp_commanded=None,
               raw_rfid_id=None, raw_rfid_c=None, raw_rfid_age_s=None,
               ground_truth=None, pulse_active=False) -> None:
        self.write({
            "type": "sample",
            "body_c": body, "body_age_s": body_age,
            # Raw = what the reader actually reported, before the plausibility
            # gate. raw_rfid_age_s growing while body_c is null means the
            # reader has stopped reading entirely (vs. reading an out-of-range
            # value) -- the two have very different causes.
            "raw_rfid_id": raw_rfid_id, "raw_rfid_c": raw_rfid_c,
            "raw_rfid_age_s": raw_rfid_age_s,
            "ambient_c": ambient, "ambient_age_s": ambient_age,
            "lamp_cmd": lamp_cmd, "lamp_state": lamp_state,
            "lamp_commanded": lamp_commanded, "power_w": power_w,
            "state": state, "reason": reason,
            "controller_wanted": controller_wanted, "manual_override": manual_override,
            "record_mode": record_mode,
            "body_setpoint_c": body_setpoint_c, "ambient_setpoint_c": ambient_setpoint_c,
            "safety_bypass_active": safety_bypass_active,
            "ground_truth": ground_truth,
            "pulse_active": pulse_active,
        })

    def event(self, kind: str, **kw) -> None:
        self.write({"type": "event", "kind": kind, **kw})

    def close(self) -> None:
        self.write({"type": "session_end"})
        with self._lock:
            self._f.close()
