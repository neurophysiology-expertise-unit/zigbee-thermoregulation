"""ADAPTER STUB for the ESP32 temperature reader.

Assumes newline-delimited output. Adjust _parse() to your firmware's format.
"""
from __future__ import annotations
import logging
from typing import Optional
from .base import SensorSource
from ..bus import SensorChannel
from ..config import Esp32Config

log = logging.getLogger(__name__)


class Esp32Source(SensorSource):
    def __init__(self, cfg: Esp32Config, channel: SensorChannel):
        super().__init__("esp32")
        self.cfg = cfg
        self.ch = channel
        self._ser = None

    def _parse(self, line: str) -> Optional[float]:
        # >>> ADJUST TO YOUR FIRMWARE <<<
        line = line.strip()
        if not line:
            return None
        try:
            return float(line)
        except ValueError:
            import json
            try:
                return float(json.loads(line)["t"])
            except Exception:
                log.warning("unparsed esp32 line: %r", line)
                return None

    def read_loop(self) -> None:
        import serial
        self._ser = serial.Serial(self.cfg.port, self.cfg.baudrate, timeout=1.0)
        while not self._stop.is_set():
            raw = self._ser.readline().decode("utf-8", "replace")
            v = self._parse(raw)
            if v is not None:
                self.ch.push(v, meta={"src": "esp32"})
        self._ser.close()
