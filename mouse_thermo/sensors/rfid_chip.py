"""ADAPTER STUB for the UID / implanted transponder reader.

Fill in _read_one() using your existing serial repo. Contract:
  returns (transponder_id: str, body_temp_c: float) or None on timeout.
Everything else -- validation, staleness, safety -- is already handled.
"""
from __future__ import annotations
import logging, time
from typing import Optional, Tuple
from .base import SensorSource
from ..bus import SensorChannel
from ..config import RfidConfig

log = logging.getLogger(__name__)


class RfidChipSource(SensorSource):
    def __init__(self, cfg: RfidConfig, channel: SensorChannel):
        super().__init__("rfid_chip")
        self.cfg = cfg
        self.ch = channel
        self._ser = None

    def _open(self):
        import serial  # pyserial
        self._ser = serial.Serial(self.cfg.port, self.cfg.baudrate, timeout=1.0)

    def _read_one(self) -> Optional[Tuple[str, float]]:
        # >>> REPLACE WITH YOUR REPO'S CALL <<<
        # e.g.  tag = uid_reader.read(self._ser); return tag.id, tag.temp_c
        raise NotImplementedError(
            "wire this to the UID serial repo: return (id, temp_c) or None"
        )

    def read_loop(self) -> None:
        self._open()
        while not self._stop.is_set():
            got = self._read_one()
            if got is None:
                time.sleep(0.05)
                continue
            tag_id, temp_c = got
            if self.cfg.transponder_id and tag_id != self.cfg.transponder_id:
                log.debug("ignoring foreign tag %s", tag_id)
                continue
            self.ch.push(temp_c, meta={"tag": tag_id})
        if self._ser:
            self._ser.close()
