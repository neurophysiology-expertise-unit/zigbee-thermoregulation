"""Adapter for the UID Devices URH-2 (LabScan) reader, AnyCage serial protocol.

Confirmed against real hardware on COM5 @ 38400 baud: keepalive lines of bare
'X' characters, '0'/'10' acks, and readings as either a single "TAG,TEMP" line
or a TAG line followed by a separate TEMP line. Protocol and arm sequence per
https://github.com/neurophysiology-expertise-unit/anycage-influx-gateway.
"""
from __future__ import annotations
import logging, re, time
from typing import Optional, Tuple
from .base import SensorSource
from ..bus import SensorChannel
from ..config import RfidConfig

log = logging.getLogger(__name__)

_FULL_RE = re.compile(r"^\s*([0-9A-F]{6,16})\s*,\s*(<25|\d+(?:\.\d+)?)\s*$", re.IGNORECASE)
_TAG_RE = re.compile(r"^[0-9A-F]{6,16}$", re.IGNORECASE)
_TEMP_RE = re.compile(r"^(<25|\d+(?:\.\d+)?)$")

_ARM_COMMANDS = ("CN 0", "TOR 10", "CID 0", "MD 0")
_ARM_COMMAND_DELAY_S = 0.08
_ARM_INIT_WAIT_S = 0.25


def _temp_to_number(token: str) -> float:
    return 24.0 if token == "<25" else float(token)


class RfidChipSource(SensorSource):
    def __init__(self, cfg: RfidConfig, channel: SensorChannel):
        super().__init__("rfid_chip")
        self.cfg = cfg
        self.ch = channel
        self._ser = None
        self._buf = b""
        self._pending_tag: Optional[str] = None
        # Raw (tag_id, temp_c, monotonic_t) for every reading the reader
        # produces, BEFORE the channel's plausibility gate -- lets a GUI show
        # "the reader is alive and reading X" during bring-up even when X is
        # outside body_valid_range (e.g. a bench chip at room temperature)
        # and therefore correctly never reaches the validated channel.
        self.last_raw_reading: Optional[Tuple[str, float, float]] = None

    def _open(self):
        import serial  # pyserial
        self._ser = serial.Serial(self.cfg.port, self.cfg.baudrate, timeout=0.2)
        time.sleep(_ARM_INIT_WAIT_S)
        self._ser.reset_input_buffer()
        for cmd in _ARM_COMMANDS:
            self._ser.write((cmd + "\r").encode())
            time.sleep(_ARM_COMMAND_DELAY_S)
        self._ser.reset_input_buffer()

    def _read_one(self) -> Optional[Tuple[str, float]]:
        chunk = self._ser.read(4096)
        if not chunk:
            return None
        self._buf += chunk

        while b"\r" in self._buf:
            raw, self._buf = self._buf.split(b"\r", 1)
            line = raw.decode(errors="ignore").strip()

            if not line or set(line) <= {"X"} or line in {"0", "10"}:
                continue
            if line.startswith("UI Devices"):
                continue

            m = _FULL_RE.match(line)
            if m:
                self._pending_tag = None
                return m.group(1).upper(), _temp_to_number(m.group(2))

            if _TAG_RE.match(line):
                self._pending_tag = line.upper()
                continue

            if _TEMP_RE.match(line) and self._pending_tag:
                tag_id = self._pending_tag
                self._pending_tag = None
                return tag_id, _temp_to_number(line)

            log.debug("unrecognized line from reader: %r", line)

        return None

    def read_loop(self) -> None:
        self._open()
        while not self._stop.is_set():
            got = self._read_one()
            if got is None:
                time.sleep(0.05)
                continue
            tag_id, temp_c = got
            self.last_raw_reading = (tag_id, temp_c, time.monotonic())
            if self.cfg.transponder_id and tag_id != self.cfg.transponder_id:
                log.debug("ignoring foreign tag %s", tag_id)
                continue
            self.ch.push(temp_c, meta={"tag": tag_id})
        if self._ser:
            self._ser.close()
