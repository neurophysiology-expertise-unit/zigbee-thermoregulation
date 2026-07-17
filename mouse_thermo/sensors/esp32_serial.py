"""Adapter for the hamsterpod ESP32-S2 / ESP-NOW gateway.

Protocol (per hamsterpod's gateway/Gateway.ino + sensor/Sensor.ino):
the gateway forwards each ESP-NOW payload over USB-CDC as RAW BINARY:

    offset  size  field
    0       6     source MAC
    6       4     uint32 LE timestamp (micros() on the gateway)
    10      16    char id[16], NUL-padded (e.g. "esp32-s2-sensor1")
    26      4     float t1   (DS18B20 probe 1)
    30      4     float t2   (DS18B20 probe 2)
    34      256   float ir[64]  (AMG8833 8x8 array)
    ---------------------------------------------------------------
    290 bytes total

FRAMING: there is NO sync marker or length prefix in the wire format.
hamsterpod's own reader_esps_influx_final.py just does read(10) then
read(280) and trusts that it started aligned. If it attaches mid-frame,
or a single byte is ever dropped, it stays misaligned forever and every
subsequent "temperature" is garbage reinterpreted from the middle of the
IR array -- garbage that can easily land in a plausible-looking range.
That is acceptable for a Grafana dashboard and NOT acceptable for
something a heat lamp's control loop reads, so this adapter re-derives
alignment structurally instead of assuming it (see _find_frame).
"""
from __future__ import annotations

import logging
import math
import struct
from typing import Optional, Tuple

from .base import SensorSource
from ..bus import SensorChannel
from ..config import Esp32Config

log = logging.getLogger(__name__)

MAC_LEN = 6
TS_LEN = 4
ID_LEN = 16
N_IR = 64
PAYLOAD_LEN = ID_LEN + 4 + 4 + 4 * N_IR      # 280
FRAME_LEN = MAC_LEN + TS_LEN + PAYLOAD_LEN   # 290

_ID_OFF = MAC_LEN + TS_LEN                   # 10
_T1_OFF = _ID_OFF + ID_LEN                   # 26
_IR_OFF = _T1_OFF + 8                        # 34

# DS18B20 sentinels that are NOT real measurements:
#   -127.0 = DEVICE_DISCONNECTED_C (probe missing / bus fault)
#    85.0  = power-on reset value never overwritten by a conversion
# The bus's plausibility gate would pass 85.0 as an "ambient" reading, so
# these must be rejected here, at the source, where we know what they mean.
DS18B20_DISCONNECTED = -127.0
DS18B20_POWER_ON_RESET = 85.0

# Structural plausibility bounds used only for FRAME ALIGNMENT, deliberately
# wide -- this is "does this look like a temperature float at all", not the
# safety range (that stays the bus's job, via sensors.*_valid_range).
_ALIGN_MIN_C, _ALIGN_MAX_C = -60.0, 150.0


def _looks_like_id(raw: bytes) -> bool:
    """char id[16], NUL-padded: printable ASCII then NULs, nothing after."""
    if not raw or raw[0] == 0:
        return False
    body, _, tail = raw.partition(b"\x00")
    if not body or any(c not in range(0x20, 0x7F) for c in body):
        return False
    return all(c == 0 for c in tail)


def _plausible_align_temp(v: float) -> bool:
    return math.isfinite(v) and _ALIGN_MIN_C <= v <= _ALIGN_MAX_C


def _frame_valid_at(buf: bytes, off: int) -> bool:
    """Structural check that a real frame starts at `off`."""
    if off + FRAME_LEN > len(buf):
        return False
    if not _looks_like_id(buf[off + _ID_OFF: off + _ID_OFF + ID_LEN]):
        return False
    t1, t2 = struct.unpack_from("<ff", buf, off + _T1_OFF)
    # A disconnected probe legitimately reads -127, so accept the sentinels
    # here (alignment) and reject them later (measurement).
    for t in (t1, t2):
        if not (_plausible_align_temp(t) or t == DS18B20_DISCONNECTED):
            return False
    # The IR array is a strong corroborator: 64 floats that must all look
    # like temperatures. Random misaligned bytes essentially never do.
    ir = struct.unpack_from("<%df" % N_IR, buf, off + _IR_OFF)
    return all(_plausible_align_temp(v) for v in ir)


class Esp32Source(SensorSource):
    def __init__(self, cfg: Esp32Config, channel: SensorChannel):
        super().__init__("esp32")
        self.cfg = cfg
        self.ch = channel
        self._ser = None
        self._buf = b""
        self._synced = False
        # Latest decoded frame, pre-plausibility-gate, for GUI bring-up --
        # same purpose as RfidChipSource.last_raw_reading.
        self.last_raw_reading: Optional[Tuple[str, float, float, float]] = None

    # ---- framing ------------------------------------------------------------

    def _find_frame(self) -> Optional[bytes]:
        """Pop one validated frame from the buffer, resyncing if needed."""
        # Fast path: we believe we're aligned, so frame 0 should validate.
        if self._synced and len(self._buf) >= FRAME_LEN:
            if _frame_valid_at(self._buf, 0):
                frame, self._buf = self._buf[:FRAME_LEN], self._buf[FRAME_LEN:]
                return frame
            # Alignment was lost (dropped byte, gateway reset, noise).
            log.warning("esp32: frame alignment lost -- resyncing")
            self._synced = False

        # Slow path: slide byte-by-byte until a frame validates.
        limit = len(self._buf) - FRAME_LEN
        for off in range(0, max(limit + 1, 0)):
            if _frame_valid_at(self._buf, off):
                if off:
                    log.info("esp32: resynced, discarded %d stray byte(s)", off)
                frame = self._buf[off:off + FRAME_LEN]
                self._buf = self._buf[off + FRAME_LEN:]
                self._synced = True
                return frame

        # Nothing valid yet. Don't let the buffer grow without bound while
        # unsynced; keep only enough to still find a frame spanning the seam.
        if len(self._buf) > 4 * FRAME_LEN:
            self._buf = self._buf[-(2 * FRAME_LEN):]
        return None

    @staticmethod
    def _decode(frame: bytes) -> dict:
        mac = ":".join(f"{b:02X}" for b in frame[:MAC_LEN])
        ts_us = struct.unpack_from("<I", frame, MAC_LEN)[0]
        sensor_id = frame[_ID_OFF:_ID_OFF + ID_LEN].split(b"\0", 1)[0].decode("ascii", "replace")
        t1, t2 = struct.unpack_from("<ff", frame, _T1_OFF)
        ir = struct.unpack_from("<%df" % N_IR, frame, _IR_OFF)
        return {"mac": mac, "ts_us": ts_us, "id": sensor_id, "t1": t1, "t2": t2, "ir": ir}

    # ---- measurement selection ----------------------------------------------

    def _measurement(self, f: dict) -> Optional[float]:
        probe = getattr(self.cfg, "probe", "t1")
        if probe == "ir_mean":
            vals = [v for v in f["ir"] if math.isfinite(v)]
            return sum(vals) / len(vals) if vals else None
        if probe == "ir_max":
            vals = [v for v in f["ir"] if math.isfinite(v)]
            return max(vals) if vals else None
        v = f.get(probe)
        if v is None:
            log.error("esp32.probe=%r is not one of t1|t2|ir_mean|ir_max", probe)
            return None
        if v == DS18B20_DISCONNECTED:
            log.warning("esp32: probe %s reads -127C (DISCONNECTED) -- ignoring", probe)
            return None
        if v == DS18B20_POWER_ON_RESET:
            log.warning("esp32: probe %s reads exactly 85.0C (DS18B20 power-on "
                        "reset, not a real conversion) -- ignoring", probe)
            return None
        if not math.isfinite(v):
            return None
        return v

    # ---- main loop ----------------------------------------------------------

    def read_loop(self) -> None:
        import serial  # pyserial
        self._ser = serial.Serial(self.cfg.port, self.cfg.baudrate, timeout=0.2)
        log.info("esp32 gateway open on %s @ %d, probe=%s",
                 self.cfg.port, self.cfg.baudrate, getattr(self.cfg, "probe", "t1"))
        try:
            while not self._stop.is_set():
                chunk = self._ser.read(4096)
                if chunk:
                    self._buf += chunk
                while True:
                    frame = self._find_frame()
                    if frame is None:
                        break
                    f = self._decode(frame)
                    want_id = getattr(self.cfg, "sensor_id", "") or ""
                    if want_id and f["id"] != want_id:
                        log.debug("ignoring esp32 node %s", f["id"])
                        continue
                    self.last_raw_reading = (f["id"], f["t1"], f["t2"], f["ts_us"])
                    v = self._measurement(f)
                    if v is not None:
                        self.ch.push(v, meta={"src": "esp32", "id": f["id"],
                                              "probe": getattr(self.cfg, "probe", "t1")})
        finally:
            if self._ser:
                self._ser.close()
