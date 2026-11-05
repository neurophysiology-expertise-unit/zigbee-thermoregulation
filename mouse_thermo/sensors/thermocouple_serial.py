"""Adapter for the Raspberry Pi Pico + MAX31856 thermocouple reader.

Line protocol (fixed in firmware, one line per reading, ~4 Hz):

    <probe_C>\t<board_C>\t<status>\r\n

  probe_C  -- tip temperature in degrees C (4 decimals), already corrected for
              swapped leads by the firmware (LEADS_SWAPPED = True). Do NOT
              correct it again here.
  board_C  -- MAX31856 cold-junction (board) temperature in degrees C.
  status   -- exactly "ok", or comma-separated faults ("voltage", "open_tc",
              "cj_range", etc.), or "TIMEOUT,<faults>".
              A failed reading prints "nan" in both temperature fields.

The board runs CircuitPython so the serial port also carries console text:
title escape sequences (ESC]0;...ESC\\), "Code done running.", tracebacks.
Any line that isn't three tab-separated fields with a parseable float first is
silently ignored.

NEVER write to the port. Ctrl-C stops the firmware; Ctrl-D reloads it.
"""
from __future__ import annotations

import logging
import math
from typing import Optional, Tuple

from .base import SensorSource
from ..bus import SensorChannel
from ..config import ThermocoupleConfig

log = logging.getLogger(__name__)


def _parse_line(raw: bytes) -> Optional[Tuple[float, float, str]]:
    """Return (probe_c, board_c, status) or None to discard.

    None for: non-3-field lines (console noise, escape sequences, tracebacks),
    non-float first two fields, partial lines. Status is passed through as-is;
    the caller decides whether to push based on it being exactly 'ok'.
    """
    try:
        text = raw.decode("ascii", errors="replace").strip()
    except Exception:
        return None
    parts = text.split("\t")
    if len(parts) != 3:
        return None
    try:
        probe_c = float(parts[0])
        board_c = float(parts[1])
    except ValueError:
        return None
    return probe_c, board_c, parts[2].strip()


class ThermocoupleSource(SensorSource):
    """Read the Pico/MAX31856 thermocouple reader over USB-CDC serial.

    Pushes probe_C into the channel only when status is exactly 'ok' and
    the value is finite.  On nan/fault/TIMEOUT the channel ages to stale and
    the controller fails cold -- same fail-cold behaviour as every other sensor.

    board_C and status are stored in the push meta so the JSONL log and the
    GUI can show them without touching the safety path.

    Port disappearance is logged loudly and retried; the channel goes stale in
    the meantime, which is the correct safe response.
    """

    def __init__(self, cfg: ThermocoupleConfig, channel: SensorChannel) -> None:
        super().__init__("thermocouple_serial")
        self.cfg = cfg
        self.channel = channel

    def read_loop(self) -> None:
        import serial

        while not self._stop.is_set():
            port = None
            try:
                port = serial.Serial(
                    self.cfg.port,
                    baudrate=self.cfg.baudrate,
                    timeout=5.0,
                )
                log.info("thermocouple reader opened on %s", self.cfg.port)
                while not self._stop.is_set():
                    raw = port.readline()
                    if not raw:
                        # 5-s readline timeout expired with no data -- log and
                        # loop so the channel ages to stale rather than hanging
                        # forever if the firmware stops sending.
                        log.warning("thermocouple: no data for 5 s on %s",
                                    self.cfg.port)
                        continue
                    parsed = _parse_line(raw)
                    if parsed is None:
                        # CircuitPython console noise, partial line, etc.
                        continue
                    probe_c, board_c, status = parsed
                    if status == "ok" and math.isfinite(probe_c) and math.isfinite(board_c):
                        self.channel.push(
                            probe_c,
                            meta={"board_c": board_c,
                                  "tc_status": status,
                                  "src": "thermocouple_serial"},
                        )
                    else:
                        # nan / fault / TIMEOUT,... -> don't push; let the channel age.
                        log.warning(
                            "thermocouple: probe=%s board=%s status=%r -> not pushed "
                            "(channel will go stale)",
                            probe_c, board_c, status,
                        )
            except Exception as exc:
                log.error(
                    "thermocouple serial error on %s: %r -- will retry in 5 s",
                    self.cfg.port, exc,
                )
            finally:
                if port is not None:
                    try:
                        port.close()
                    except Exception:
                        pass
                    port = None
            if not self._stop.is_set():
                self._stop.wait(5.0)
