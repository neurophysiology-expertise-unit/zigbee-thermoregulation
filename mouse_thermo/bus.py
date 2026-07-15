"""Thread-safe shared state between sensor threads and the control loop.

Design rules (deliberate):
  - A reading is only returned if it is BOTH fresh and plausible.
  - Out-of-range values are rejected at push time and counted, never silently
    coerced. No fabricated fallback values, ever.
  - get() returning None means "I do not know" -- callers must treat that as a
    safety condition, not as a zero.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True)
class Reading:
    value: float
    t: float                       # time.monotonic() at receipt
    meta: dict = field(default_factory=dict)

    def age(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.monotonic()) - self.t


class SensorChannel:
    """One named scalar channel (e.g. body_temp, ambient_temp)."""

    def __init__(
        self,
        name: str,
        stale_after_s: float,
        valid_range: Tuple[float, float],
    ):
        self.name = name
        self.stale_after_s = float(stale_after_s)
        self.lo, self.hi = valid_range
        self._lock = threading.Lock()
        self._last: Optional[Reading] = None
        self.n_pushed = 0
        self.n_rejected = 0
        self.last_reject_reason: Optional[str] = None

    def push(self, value: float, meta: Optional[dict] = None) -> bool:
        """Called from sensor threads. Returns True if accepted."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            with self._lock:
                self.n_rejected += 1
                self.last_reject_reason = f"non-numeric: {value!r}"
            return False

        if not (v == v):  # NaN
            with self._lock:
                self.n_rejected += 1
                self.last_reject_reason = "NaN"
            return False

        if not (self.lo <= v <= self.hi):
            with self._lock:
                self.n_rejected += 1
                self.last_reject_reason = (
                    f"{v:.2f} outside plausible [{self.lo}, {self.hi}]"
                )
            return False

        with self._lock:
            self._last = Reading(v, time.monotonic(), meta or {})
            self.n_pushed += 1
        return True

    def get(self, now: Optional[float] = None) -> Optional[Reading]:
        """Fresh + plausible reading, else None."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            last = self._last
        if last is None:
            return None
        if last.age(now) > self.stale_after_s:
            return None
        return last

    def get_raw(self) -> Optional[Reading]:
        """Last accepted reading regardless of staleness. For logging only."""
        with self._lock:
            return self._last

    def age(self, now: Optional[float] = None) -> Optional[float]:
        r = self.get_raw()
        return None if r is None else r.age(now)

    def stats(self) -> dict:
        with self._lock:
            return {
                "pushed": self.n_pushed,
                "rejected": self.n_rejected,
                "last_reject": self.last_reject_reason,
            }
