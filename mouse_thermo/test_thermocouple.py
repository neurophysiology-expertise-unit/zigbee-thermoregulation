"""Unit tests for the thermocouple serial parser and sensor behaviour."""
import math
import pytest

from mouse_thermo.sensors.thermocouple_serial import _parse_line


# ---- _parse_line ------------------------------------------------------------

def test_ok_line_parses():
    result = _parse_line(b"29.5312\t24.3750\tok\r\n")
    assert result is not None
    probe_c, board_c, status = result
    assert math.isclose(probe_c, 29.5312, rel_tol=1e-5)
    assert math.isclose(board_c, 24.3750, rel_tol=1e-5)
    assert status == "ok"


def test_nan_timeout_discarded():
    result = _parse_line(b"nan\tnan\tTIMEOUT,voltage\r\n")
    # nan fields still parse to float("nan"), so the tuple is returned;
    # the caller is expected to check math.isfinite. Confirm the status is not "ok".
    assert result is not None
    probe_c, board_c, status = result
    assert not math.isfinite(probe_c)
    assert status == "TIMEOUT,voltage"


def test_fault_status_with_number_parses_as_fault():
    result = _parse_line(b"28.1250\t23.5000\topen_tc,cj_range\r\n")
    assert result is not None
    _, _, status = result
    assert status != "ok"


def test_escape_sequence_line_ignored():
    # CircuitPython title escape: ESC ] 0 ; ... ESC \
    result = _parse_line(b"\x1b]0;code.py\x1b\\\r\n")
    assert result is None


def test_traceback_line_ignored():
    result = _parse_line(b"Traceback (most recent call last):\r\n")
    assert result is None


def test_code_done_running_ignored():
    result = _parse_line(b"Code done running.\r\n")
    assert result is None


def test_partial_line_ignored():
    result = _parse_line(b"29.53\t")
    assert result is None


def test_empty_line_ignored():
    result = _parse_line(b"\r\n")
    assert result is None


def test_two_field_line_ignored():
    result = _parse_line(b"29.53\t24.37\r\n")
    assert result is None


def test_non_float_first_field_ignored():
    result = _parse_line(b"ERR\t24.37\tok\r\n")
    assert result is None


def test_four_field_line_ignored():
    result = _parse_line(b"29.53\t24.37\tok\textra\r\n")
    assert result is None
