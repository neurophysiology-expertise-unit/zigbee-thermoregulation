# CLAUDE.md

Context for Claude Code working in this repository. Read before making changes.

## What this is

Closed-loop heat-lamp control for mouse thermoregulation experiments.
**A live animal sits under the lamp this code controls.** Bugs here can cook a
mouse. Treat safety-relevant changes with the caution that implies.

Hardware: Sonoff ZBDongle-E (EFR32MG21 / EZSP → `bellows`, *not* zigpy-znp),
Sonoff Zigbee smart plug driving a heat lamp, Sonoff SNZB-02 ambient
temp/humidity sensor, implanted RFID temperature transponder read over serial
via a separate repo, optional ESP32 temperature probe.

**`config.yaml`'s defaults (body_max_c 38.5, body_setpoint_c 36.5) are for
normal thermoregulation** -- keeping an animal warm without overheating it.
`config.local.yaml` (gitignored, this machine's real experiment) is currently
configured for a **hyperthermia/seizure-induction protocol**: body_setpoint_c
42.0, body_max_c 43.5 (a deliberate 1.5C margin above target, not a typo), and
sensors.body_valid_range raised to (30, 46) so real readings at/above the new
hard max aren't discarded by the plausibility gate as an implausible sensor
fault -- those are exactly the readings that must reach the safety supervisor
to trigger LOCKOUT. Don't "fix" config.local.yaml's numbers to match
config.yaml's; they're intentionally different protocols.

## Architecture

```
RFID/UID reader ──┐
ESP32 probe ──────┼──► SensorChannel ──► Safety ──► Controller ──► ZigbeePlug ──► lamp
Sonoff SNZB-02 ───┘    (stale+range)     (veto)      (hysteresis)   (zigpy/bellows)
                                            │
                                       Watchdog ──► force OFF
                                            │
                                    SessionLogger (one JSONL, one clock)
```

| File | Role |
|---|---|
| `bus.py` | Thread-safe `SensorChannel`: staleness + plausibility gating |
| `safety.py` | `SafetySupervisor`. Hard limits, latching lockout. **Veto only.** |
| `controller.py` | State machine: NORMAL / FALLBACK / LOCKOUT, hysteresis, dwell |
| `main.py` | Async entry point, wiring, fail-safe shutdown |
| `zigbee/app.py` | zigpy/bellows: plug actuator + SNZB-02 listener |
| `watchdog.py` | Software watchdog, forces lamp off if main loop stalls |
| `logger.py` | Unified JSONL, one monotonic clock |
| `sensors/rfid_chip.py` | Adapter for the UID Devices URH-2 reader (AnyCage protocol) |
| `sensors/esp32_serial.py` | Adapter for the hamsterpod ESP32-S2/ESP-NOW gateway (binary frames) |
| `gui.py` | PySide6 live monitor + manual override + read-only Review tab, runs `main.run()` in-process |

`gui.py` is not a separate tool -- only one process can hold the Zigbee
dongle / serial ports at a time, so it drives `main.run()` in a background
thread via `on_ready`/`SessionHandle` rather than opening its own device
connections. `pip install -r requirements-gui.txt` (kept separate from the
core `requirements.txt` so headless deployments don't need Qt). Note:
PySide6 6.11.1 failed to import on Windows here with a DLL load error
(likely a packaging issue in that specific release); 6.8.0.2 works, hence
the `<6.11` pin -- re-check with `python -c "from PySide6 import QtCore"`
before loosening it.

## Invariants — do not violate without discussion

These are deliberate. If a change appears to require breaking one, stop and
raise it rather than working around it.

1. **Never suppress errors.** No bare `except: pass`. A dead sensor thread logs
   loudly and lets its channel go stale. Staleness is the mechanism that makes
   the system fail cold.
2. **Never fabricate a fallback value.** `SensorChannel.get()` returns `None`,
   never a last-known value, never a zero. `None` means "I don't know" and is
   treated as unsafe. Do not add a "sensible default."
3. **Crash loudly on incoherent config.** `Config.validate()` refuses rather
   than degrading.
4. **Safety only vetoes, never commands ON.** The asymmetry is the point: a bug
   in `safety.py` fails cold. Never add a path where `safety.py` turns heat on.
5. **Dwell time never delays an OFF.** `min_on_s` / `min_off_s` are anti-chatter
   for turn-*on* only. Safety shutoffs bypass dwell (`force=True`).
6. **The ambient cap is active in every state, including NORMAL.** A cold mouse
   does not license an overheated box.
7. **The stuck-on latch is sticky.** It requires operator `reset_latch()` after
   physical inspection. Do not make it auto-release — that oscillates.
8. **Lamp OFF at startup, on any exception, on any signal, on any exit path.**
9. **`ground_truth` selects the REGULATION source, never what safety sees.**
   `Controller.step(..., ground_truth=)` picks which reading the controller
   pursues (`auto`/`body`/`ambient`), but `safety.evaluate()` always runs on
   BOTH real readings. A body hard-ceiling breach must still LOCKOUT while
   regulating on ambient, and vice versa. Choosing a source that is then
   absent is a non-latched LOCKOUT (fail cold, self-corrects) — never a
   silent switch to the other source.

## GUI mode model

The GUI (`gui.py`) has ONE mode toggle: **Freerun** or **Auto**.
- **Freerun** = `manual_override` set; operator drives the lamp with Lamp
  ON/OFF (default OFF). A recording started here is `open_loop`.
- **Auto** = `manual_override` clear; the controller regulates, on the source
  chosen in the **ground truth** dropdown. A recording started here is
  `closed_loop`. Loop mode is DERIVED from the mode — there is no separate
  open/closed selector (that was redundant and has been removed).

Ground truth defaults to **ambient**: the RFID/body reader is killed by the
lamp's EMI while it runs (confirmed — `raw_rfid_age_s` climbs to tens of
seconds the instant the lamp comes on, reads resume the instant it's off), so
body is not a trustworthy continuous regulation source during heating on this
rig. This is a hardware/EMI limitation, not a software one.

**Pulse ("chopped lamp")**: heat in `pulse_on_s`/`pulse_off_s` bursts (default
1s/1s, live-adjustable via GUI spinboxes) so the RFID reader recovers in the
OFF gaps and reads body temp there
(body stays fresh for `body_stale_after_s` through the next ON burst). Two
forms, both computed in `main.py`'s desired-lamp logic, both riding inside the
same LOCKOUT gating (a safety veto forces OFF mid-cycle — tested):
- **Freerun pulse** (`pulse_active`): a manual-override button; pulses
  regardless of the controller.
- **Auto pulse** (`auto_pulse`, default ON): in AUTO the controller's *heat*
  decision (`decision.lamp_on`) is *delivered* as pulses instead of steady-on;
  when the controller wants OFF (body at setpoint) the lamp is off, no pulsing.
  This is what makes closed-loop body/RFID regulation possible at all, since
  the lamp otherwise blinds the reader. Uncheck it when regulating on ambient,
  where pulsing gives no benefit and just halves heating + wears the relay.
The loop shortens its wait to wake on pulse edges rather than aliasing against
`loop_period_s`. Engaging pulse has up to `loop_period_s` latency, and rapid
relay cycling wears the plug's mechanical relay.

## Cool mode (Peltier) — the mirror of heat mode

`control.mode` (config) is `heat` (default) or `cool`. Heat mode is the original
heat-lamp behaviour, untouched. Cool mode drives a **Peltier switched by the
same on/off Sonoff plug** (on this rig: a USB Peltier through a USB hub, so the
plug's ON also runs the Peltier's hot-side fan) to pull temperature DOWN — the
current experiment cools an animal's **brain from 36→32 °C** via the headbar
holder (conductive). It is **cooling-only, not true bidirectional**: one on/off
actuator, no polarity reversal (a plug can't reverse DC; true bidirectional
would need an H-bridge/TEC controller behind a new `Plug` adapter).

Cool mode is the exact mirror of heat mode — every comparison direction flips on
`mode`:

| | heat | cool |
|---|---|---|
| actuator ON → | temp up | temp down |
| hard limit | ceiling (`*_max_c`) | **floor** (`*_min_c`) |
| controller ON when | value < sp−db | value > sp+db |
| latch releases on | cooling evidence | **warming** evidence |
| fail-safe OFF drifts to | room (cooler) | room (**warmer**) ✅ |

Because OFF drifts to survivable room temp in BOTH modes, the whole fail-cold
skeleton (invariants 1–8) is unchanged and cool mode adds **no** new "safety
turns something on" path — invariant 4 holds. The one hazard that inverts is a
**stuck-ON actuator** (runaway cold instead of hot); `max_continuous_on_s` +
the hardware cutoff (now a **low-temp** cutoff, see below) guard it.

Config rules (enforced by `Config.validate()`, which crashes loudly):
`body_setpoint_c` must be **above** `body_min_c`; the sensor `*_valid_range` LOW
end must be **below** the floor so a floor breach still reaches the supervisor
(mirror of the hyperthermia protocol raising the range's HIGH end). `mode` is
wired to `SafetySupervisor(mode=)` and read by `Controller` from
`cfg.control.mode`; both come from the one `control.mode`, so they can't
disagree. `test_safety.py` has a full mirror cool suite (`mk_cool`, `test_cool_*`).

The **hardware cutoff** requirement inverts too: heat mode wants a bimetallic
thermostat a few °C ABOVE `ambient_max_c`; cool mode wants a **low-temperature
cutoff** that kills the Peltier supply below a floor. Still a hardware
requirement, not a software one.

## Review tab (gui.py) — read-only recording playback

A third GUI tab, independent of the live loop (pure file read). **Load
recording…** parses a session/recording `.jsonl` into the whole time series
(not the live sweep window); the operator scrubs a cursor across it to read
body/ambient/actuator/state at any instant, with control-quality stats
(mean/SD/range, % time within deadband of setpoint, actuator duty, LOCKOUT
count). A **Window** combo (4 s … All) zooms and a **Pan** slider scrolls the
zoom window across the recording. Parser (`_parse_recording`) is tolerant of a
truncated final line (interrupted session) and missing keys (older files).

## Before any change to safety.py / controller.py / bus.py

Run these from the repo root (the parent of `mouse_thermo/`) — `main.py` uses
package-relative imports, so it must be invoked as `-m mouse_thermo.main`, not
run directly from inside the package directory.

```bash
pytest mouse_thermo/test_safety.py -q     # 11 tests, all must pass
```

Then verify in simulation before touching hardware:

```bash
python -m mouse_thermo.main --config mouse_thermo/config.yaml --simulate
```

If you change control behaviour, add a test that pins the new behaviour. The
test suite is the specification.

## Current state

Working: safety supervisor, controller, bus, watchdog, logger, zigpy layer,
pairing helper, simulation mode, RFID adapter, **heat+cool modes**, **GUI
Review tab**. 29/29 tests pass. Heat sim loop and RFID reader verified end to
end against real hardware; cool mode verified in simulation only (held box ~20°C
around setpoint; floor LOCKOUT fires) — **not yet run on the real Peltier**.

Windows bring-up (drivers, COM ports, re-pairing) is documented in
`INSTALL_WINDOWS.md`.

Zigbee devices paired: SONOFF S60ZBTPF plug, SONOFF SNZB-02P ambient sensor
(a spare SNZB-02D is also paired but unused). IEEE addresses and the reader's
COM port live in the gitignored `config.local.yaml`, not `config.yaml`.

**Open work:**
1. DONE — ESP32 ambient probe live. `config.local.yaml` has `esp32.enabled:
   true`, `port: COM6`, `role: ambient`, `probe: t1`; verified end to end
   (adapter -> ambient channel ~22.9C). See the ESP32 section below.
2. Tune `ambient_setpoint_c` / `body_setpoint_c` against the real box.
3. **Cooling rig on a 2nd Windows PC.** New brain 36→32 °C protocol with a USB
   Peltier on the headbar (see "Cool mode" above). To do on the new machine:
   install per `INSTALL_WINDOWS.md`, re-pair the Zigbee devices, write a
   `config.local.yaml` with `control.mode: cool` + floors + a valid range whose
   low end sits below the floor, then dry-run cool mode before an animal.
4. **Re-test RFID EMI with the Peltier.** The pulse/"chopped lamp" feature was
   specific to the heat lamp's EMI blinding the reader; a DC Peltier may not,
   in which case pulse is unnecessary for cooling. Check `raw_rfid_age_s` with
   the Peltier running before assuming pulse is needed.
5. Consider relabelling "Lamp"/"heat" wording in `gui.py`/`main.py` to the
   mode-neutral "actuator" (cosmetic; behaviour is already mode-correct).

## The ESP32 (hamsterpod) path

`sensors/esp32_serial.py` speaks the **binary** USB-CDC format — 290-byte
frames of `MAC(6) | ts_us(4) | id[16] | t1 | t2 | ir[64]` — not newline text.
Config: `esp32.probe` selects `t1|t2|ir_mean|ir_max`.

**This rig uses a SINGLE board, not the stock two-board design.** hamsterpod
ships a "sensor" node that transmits over ESP-NOW and a "gateway" node that
forwards to USB — so one board emits nothing over USB. We flashed custom
single-board firmware (`hamsterpod/sensor_usb/sensor_usb.ino`) that reads the
probes and writes the *identical* 290-byte frame straight to USB-CDC, so the
adapter below is unchanged. Two ambient DS18B20 probes: **t1 = east (GPIO15),
t2 = west (GPIO16)**; `probe: t1` drives control, t2 is logged only. Board is
COM6 (ESP32-S2 native USB, VID_303A). No AMG8833 fitted -> IR is zero-filled.

**The wire format has no sync marker or length prefix.** hamsterpod's own
`reader_esps_influx_final.py` does `read(10)` then `read(280)` and assumes it
started aligned; attach mid-stream or drop one byte and it is misaligned
*forever*, silently reporting floats reinterpreted from the middle of the IR
array. Verified: doing it that way on a mid-frame join reports `0.00C` — no
error, just a lie. Fine for a Grafana panel, not for something a heat lamp
obeys. So this adapter re-derives alignment structurally every frame
(`_frame_valid_at`: the `id[16]` field must be NUL-padded ASCII *and* all 66
floats must look like temperatures). Do not "simplify" this to a bare
sequential read.

DS18B20 sentinels are rejected at the source, not left to the plausibility
gate: `-127.0` (probe disconnected) and `85.0` (power-on value, no conversion
completed). The bus's range would catch these *today*, but the ranges are
operator-tunable, and these mean "no measurement" at any range.

## Known limitations — do not paper over these in code

The software watchdog covers a stalled loop. It does **not** cover process
death, host power loss, USB dongle drop, or Zigbee link failure. In all of
those the plug **stays in its last commanded state**. If that was ON, nothing
in this repo turns it off.

This is mitigated by an **inline bimetallic thermostat / thermal cutoff on the
lamp circuit**, set a few degrees above `ambient_max_c`. That is a hardware
requirement, not a software one. Do not attempt to solve it in Python.

The SNZB-02 is a battery *sleepy* device: it reports on-change every ~30s–few
minutes and routinely ignores the `configure_reporting` request. It is a
logging and fallback input, **not** a fast safety sensor. Do not write code
that assumes it is fresh.

Its bind/configure/read transactions can each hang ~55s (message timeout) if
the device is asleep, so they run in a **background task**
(`ZigbeeSensorListener.run_maintenance`), NEVER inline in startup — doing them
inline previously stalled the whole session for minutes. The task retries the
bind and then actively re-reads (~20s) to compensate for the device ignoring
its reporting config. The report callback is registered the instant the
listener is constructed, so spontaneous reports are captured regardless of
whether the bind has succeeded yet. **Never move ambient setup back onto the
control-loop / startup critical path** — a sleepy device must not be able to
block the safety loop. The real fix for continuous ambient is the wired ESP32
probe, not this sensor.

## Style

- `from __future__ import annotations`, type hints throughout
- stdlib logging, module-level `log = logging.getLogger(__name__)`
- dataclasses for config and value objects
- all thresholds in `config.yaml` → `config.py`; no magic numbers in logic
