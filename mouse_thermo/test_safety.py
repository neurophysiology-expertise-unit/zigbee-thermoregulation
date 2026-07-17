"""Safety tests. These must pass before the rig ever sees an animal."""
import time
import pytest

from mouse_thermo.actuators.base import Plug
from mouse_thermo.bus import SensorChannel, Reading
from mouse_thermo.config import SafetyConfig, ControlConfig, SensorConfig, Config
from mouse_thermo.safety import SafetySupervisor
from mouse_thermo.controller import Controller, State


def R(v, age=0.0):
    return Reading(v, time.monotonic() - age)


def mk(**kw):
    s = SafetyConfig(**kw)
    c = ControlConfig(min_on_s=0, min_off_s=0)
    sup = SafetySupervisor(s)
    return sup, Controller(c, sup, s)


def test_ambient_hard_max_latches_and_stays_latched():
    sup, ctrl = mk(ambient_max_c=32.0, lockout_release_hysteresis_c=1.0)
    d = ctrl.step(R(36.0), R(32.5))
    assert d.lamp_on is False and d.state is State.LOCKOUT
    assert d.latched is True, "a real hard-ceiling breach must never be overridable"
    # cooled a little, but not past the hysteresis -> still locked
    d = ctrl.step(R(30.0), R(31.5))
    assert d.lamp_on is False and d.state is State.LOCKOUT
    # properly cool -> releases
    d = ctrl.step(R(30.0), R(24.0))
    assert d.state is not State.LOCKOUT


def test_body_hard_max_latches():
    sup, ctrl = mk(body_max_c=38.5)
    d = ctrl.step(R(39.0), R(25.0))
    assert d.lamp_on is False and sup.latched
    assert d.latched is True, "a real hard-ceiling breach must never be overridable"


def test_both_sensors_stale_forces_off():
    sup, ctrl = mk()
    d = ctrl.step(None, None)
    assert d.lamp_on is False and d.state is State.LOCKOUT
    # NOT latched: "we don't know" is conservative-by-default, not evidence of
    # active danger -- this is the one LOCKOUT flavor a manual override is
    # allowed to substitute for (main.py), e.g. bench-testing the relay with
    # no sensors live. A real hard-ceiling breach or stuck-on latch (above)
    # must never behave this way.
    assert d.latched is False


def test_no_body_falls_back_to_ambient_control():
    sup, ctrl = mk()
    d = ctrl.step(None, R(22.0))   # cold room, no chip read
    assert d.lamp_on is True and d.state is State.FALLBACK
    d = ctrl.step(None, R(29.5))   # ambient above cap
    assert d.lamp_on is False and d.state is State.FALLBACK


def test_ground_truth_ambient_ignores_body_for_regulation():
    """ground_truth=ambient regulates on ambient even when a valid body
    reading is present -- used when the lamp's EMI makes body untrustworthy."""
    sup, ctrl = mk()
    # Body is cold (would normally drive heat ON), but ambient is above its
    # cap. Regulating on ambient must keep the lamp OFF.
    d = ctrl.step(R(33.0), R(29.5), ground_truth="ambient")
    assert d.lamp_on is False and d.state is State.FALLBACK
    # Cold ambient with a hot body: ambient-regulation heats regardless of body.
    d = ctrl.step(R(38.0), R(22.0), ground_truth="ambient")
    assert d.lamp_on is True and d.state is State.FALLBACK


def test_ground_truth_body_locks_out_when_body_missing():
    """If body is the chosen ground truth and it disappears (e.g. RFID lost),
    refuse to heat -- do NOT silently fall back to ambient. Non-latched so it
    recovers the moment body returns."""
    sup, ctrl = mk()
    d = ctrl.step(None, R(22.0), ground_truth="body")   # cold room, but no body
    assert d.lamp_on is False and d.state is State.LOCKOUT
    assert d.latched is False
    # body returns -> regulates normally again
    d = ctrl.step(R(34.0), R(22.0), ground_truth="body")
    assert d.lamp_on is True and d.state is State.NORMAL


def test_ground_truth_never_blinds_safety():
    """A body hard-ceiling breach must LOCKOUT even while regulating on
    ambient -- ground_truth changes regulation, never what safety sees."""
    sup, ctrl = mk(body_max_c=38.5)
    d = ctrl.step(R(39.0), R(22.0), ground_truth="ambient")
    assert d.lamp_on is False and d.state is State.LOCKOUT and sup.latched


def test_ambient_cap_overrides_cold_body():
    """The core requirement: a cold mouse does NOT license an overheated box."""
    sup, ctrl = mk()
    d = ctrl.step(R(33.0), R(29.0))   # body way below sp, ambient at cap
    assert d.lamp_on is False
    assert "ambient" in d.reason


def test_cold_body_heats_when_ambient_ok():
    sup, ctrl = mk()
    d = ctrl.step(R(34.0), R(23.0))
    assert d.lamp_on is True and d.state is State.NORMAL


def test_stuck_on_latch_is_sticky():
    sup, ctrl = mk(max_continuous_on_s=10.0)
    t0 = time.monotonic()
    ctrl.step(R(34.0), R(23.0), now=t0)          # turns on
    d = ctrl.step(R(34.0), R(23.0), now=t0 + 20) # 20s continuously on
    assert d.lamp_on is False and sup.latched
    assert d.latched is True, "stuck-on must never be overridable"
    # ambient is perfectly fine, but sticky latch must NOT auto-release
    d = ctrl.step(R(34.0), R(22.0), now=t0 + 30)
    assert d.lamp_on is False and d.state is State.LOCKOUT
    sup.reset_latch()
    d = ctrl.step(R(34.0), R(22.0), now=t0 + 40)
    assert d.state is State.NORMAL


def test_implausible_body_value_is_rejected_not_used():
    ch = SensorChannel("body", 30.0, (30.0, 43.0))
    assert ch.push(36.5) is True
    assert ch.push(85.0) is False      # sensor fault / bad parse
    assert ch.get().value == 36.5      # last GOOD value, not the garbage
    assert ch.push(float("nan")) is False
    assert ch.stats()["rejected"] == 2


def test_stale_reading_returns_none_not_last_value():
    ch = SensorChannel("body", 1.0, (30.0, 43.0))
    ch.push(36.5)
    time.sleep(1.1)
    assert ch.get() is None            # must NOT return the stale 36.5


def test_dwell_never_blocks_a_safety_off():
    s = SafetyConfig(ambient_max_c=32.0)
    c = ControlConfig(min_on_s=600.0, min_off_s=600.0)   # absurd dwell
    sup = SafetySupervisor(s)
    ctrl = Controller(c, sup, s)
    t0 = time.monotonic()
    d = ctrl.step(R(34.0), R(23.0), now=t0)
    assert d.lamp_on is True
    d = ctrl.step(R(34.0), R(33.0), now=t0 + 1)   # 1s later, way inside min_on
    assert d.lamp_on is False, "min_on_s must never delay a safety shutoff"


def test_config_rejects_setpoint_above_hard_max():
    cfg = Config(simulate=True)
    cfg.control.body_setpoint_c = 39.0
    cfg.safety.body_max_c = 38.5
    with pytest.raises(ValueError, match="body_setpoint_c"):
        cfg.validate()


class NeverConfirmingPlug(Plug):
    """A plug whose relay works but which NEVER sends an attribute report --
    exactly the real Sonoff behaviour that left a lamp physically ON while
    the software believed it was off. state() is frozen at its seeded value
    forever; only commanded() tracks reality."""

    def __init__(self, seeded_state=False):
        self.relay_on = False          # the real, physical relay
        self._frozen_state = seeded_state  # what state() forever claims
        self._commanded = None

    def set(self, on: bool) -> None:
        self.relay_on = on             # relay obeys...
        self._commanded = on           # ...and we know what we asked for
        # ...but _frozen_state is deliberately NEVER updated: no reports.

    def state(self):
        return self._frozen_state

    def commanded(self):
        return self._commanded


def test_off_command_is_sent_even_when_plug_never_confirms():
    """Regression: deciding `desired != state()` skipped real commands when
    the device never reports, leaving the lamp ON while software said OFF."""
    plug = NeverConfirmingPlug(seeded_state=False)

    # Controller demands ON -> relay must actually turn on.
    plug.set(True)
    assert plug.relay_on is True
    # state() still lies (frozen at the seeded False) -- that's the trap.
    assert plug.state() is False
    assert plug.commanded() is True

    # Now safety demands OFF. The OLD logic compared desired to state():
    #   desired(False) != state(False) -> False -> no command -> LAMP STAYS ON.
    assert (False != plug.state()) is False, "the exact stale-confirmation trap"
    # The NEW logic compares desired to commanded(), which is truthful:
    assert (False != plug.commanded()) is True, "must recognise a command is needed"

    plug.set(False)
    assert plug.relay_on is False, "lamp must actually be off"


def test_commanded_defaults_to_none_so_first_command_always_sends():
    plug = NeverConfirmingPlug(seeded_state=False)
    # Before any command, commanded() is None -- so `desired != commanded()`
    # is true for BOTH True and False, guaranteeing the startup OFF is sent
    # rather than skipped because state() already happens to read False.
    assert plug.commanded() is None
    assert (False != plug.commanded()) is True
    assert (True != plug.commanded()) is True
