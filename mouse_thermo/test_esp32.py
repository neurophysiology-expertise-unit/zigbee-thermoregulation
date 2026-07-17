"""Framing/decoding tests for the hamsterpod ESP32 gateway adapter.

The wire format has no sync marker, so alignment is inferred structurally.
These tests exist because a misaligned frame does not fail loudly -- it
silently yields a plausible-looking temperature reinterpreted from the
middle of the IR array, which a heat-lamp control loop would then act on.
"""
import struct

import pytest

from mouse_thermo.bus import SensorChannel
from mouse_thermo.config import Config, Esp32Config
from mouse_thermo.sensors.esp32_serial import (
    DS18B20_DISCONNECTED,
    DS18B20_POWER_ON_RESET,
    FRAME_LEN,
    Esp32Source,
)


def make_frame(t1=24.5, t2=31.0, ir_val=25.0, sensor_id="esp32-s2-sensor1",
               mac=b"\xAA\xBB\xCC\xDD\xEE\xFF", ts_us=123456):
    """Build a byte-exact frame the way Gateway.ino emits it."""
    out = bytearray()
    out += mac
    out += struct.pack("<I", ts_us)
    out += sensor_id.encode().ljust(16, b"\0")
    out += struct.pack("<ff", t1, t2)
    out += struct.pack("<64f", *([ir_val] * 64))
    assert len(out) == FRAME_LEN, len(out)
    return bytes(out)


def mk_source(probe="t1", sensor_id=""):
    cfg = Esp32Config(enabled=True, port="COM_TEST", probe=probe, sensor_id=sensor_id)
    ch = SensorChannel("ambient_test", 30.0, (5.0, 60.0))
    return Esp32Source(cfg, ch), ch


def test_decodes_a_clean_frame():
    src, _ = mk_source()
    src._buf = make_frame(t1=24.5, t2=31.0)
    frame = src._find_frame()
    assert frame is not None
    f = src._decode(frame)
    assert f["id"] == "esp32-s2-sensor1"
    assert round(f["t1"], 2) == 24.5
    assert round(f["t2"], 2) == 31.0
    assert len(f["ir"]) == 64


def test_resyncs_when_attached_mid_stream():
    """The real hazard: connecting while the gateway is already mid-frame.
    hamsterpod's own reader would stay misaligned forever."""
    src, _ = mk_source()
    # Simulate joining 137 bytes into a frame, then a clean frame follows.
    src._buf = make_frame(t1=99.0)[137:] + make_frame(t1=24.5)
    frame = src._find_frame()
    assert frame is not None
    f = src._decode(frame)
    # Must have discarded the partial and locked onto the *whole* next frame,
    # not decoded garbage from the seam.
    assert round(f["t1"], 2) == 24.5


def test_recovers_after_a_dropped_byte():
    src, _ = mk_source()
    src._buf = make_frame(t1=24.5)
    assert src._find_frame() is not None
    assert src._synced is True
    # A single byte vanishes mid-stream -> everything after is shifted.
    src._buf = b"\x00" + make_frame(t1=30.25)
    f = src._decode(src._find_frame())
    assert round(f["t1"], 2) == 30.25, "must resync rather than emit garbage"


def test_streams_multiple_frames_back_to_back():
    src, _ = mk_source()
    src._buf = make_frame(t1=20.0) + make_frame(t1=21.0) + make_frame(t1=22.0)
    got = []
    while (fr := src._find_frame()) is not None:
        got.append(round(src._decode(fr)["t1"], 2))
    assert got == [20.0, 21.0, 22.0]


def test_rejects_disconnected_probe_sentinel():
    """-127C means 'no probe', not 'it is -127 degrees'."""
    src, _ = mk_source(probe="t1")
    f = src._decode(make_frame(t1=DS18B20_DISCONNECTED))
    assert src._measurement(f) is None


def test_rejects_power_on_reset_sentinel():
    """85.0C is the DS18B20's power-on value when no conversion has completed.

    With today's ambient_valid_range (5-60C) the bus would also reject it, so
    this check is defence-in-depth rather than the only thing standing in the
    way. It earns its place by being range-independent: the sentinel means
    'no measurement happened' whatever the configured range is, and these
    ranges are operator-tunable (ambient_max_c was widened to 50C this week).
    Proven below against a channel wide enough to accept 85.0.
    """
    src, _ = mk_source(probe="t1")
    f = src._decode(make_frame(t1=DS18B20_POWER_ON_RESET))
    assert src._measurement(f) is None

    # A channel whose range does NOT exclude 85.0 -- here the source-level
    # check is the only thing preventing a fake reading being acted on.
    wide = SensorChannel("wide", 30.0, (-100.0, 200.0))
    assert wide.push(DS18B20_POWER_ON_RESET) is True, (
        "the bus accepts 85.0 when the range allows it -- which is exactly "
        "why the sentinel must be caught at the source"
    )


def test_probe_selection_t2_and_ir():
    src, _ = mk_source(probe="t2")
    f = src._decode(make_frame(t1=10.0, t2=33.0, ir_val=27.0))
    assert round(src._measurement(f), 2) == 33.0

    src, _ = mk_source(probe="ir_mean")
    assert round(src._measurement(f), 2) == 27.0

    src, _ = mk_source(probe="ir_max")
    assert round(src._measurement(f), 2) == 27.0


def test_sensor_id_filter_is_available():
    src, _ = mk_source(sensor_id="esp32-s2-sensor1")
    f = src._decode(make_frame(sensor_id="esp32-s2-sensor1"))
    assert f["id"] == "esp32-s2-sensor1"
    other = src._decode(make_frame(sensor_id="other-node"))
    assert other["id"] == "other-node"


def test_unsynced_buffer_does_not_grow_without_bound():
    src, _ = mk_source()
    src._buf = b"\x7f" * (10 * FRAME_LEN)  # pure noise, never validates
    assert src._find_frame() is None
    assert len(src._buf) <= 4 * FRAME_LEN


def test_config_rejects_bad_probe():
    cfg = Config(simulate=True)
    cfg.esp32.enabled = True
    cfg.esp32.probe = "t3"
    with pytest.raises(ValueError, match="esp32.probe"):
        cfg.validate()
