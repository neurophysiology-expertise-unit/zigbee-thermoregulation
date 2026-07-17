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
| `gui.py` | PySide6 live monitor + manual override, runs `main.run()` in-process |

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
pairing helper, simulation mode, RFID adapter. 11/11 tests pass. Sim loop and
RFID reader verified end to end against real hardware.

Zigbee devices paired: SONOFF S60ZBTPF plug, SONOFF SNZB-02P ambient sensor
(a spare SNZB-02D is also paired but unused). IEEE addresses and the reader's
COM port live in the gitignored `config.local.yaml`, not `config.yaml`.

**Open work:**
1. Plug in the hamsterpod ESP32-S2 gateway, set `esp32.port`/`enabled` in
   `config.local.yaml`, and confirm live frames decode (adapter + tests are
   done; only untested against the physical gateway).
2. Tune `ambient_setpoint_c` / `body_setpoint_c` against the real box.

## The ESP32 (hamsterpod) path

`sensors/esp32_serial.py` speaks the gateway's **binary** USB-CDC format —
290-byte frames of `MAC(6) | ts_us(4) | id[16] | t1 | t2 | ir[64]` — not
newline text. Config: `esp32.probe` selects `t1|t2|ir_mean|ir_max`.

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

The SNZB-02 is a battery device reporting on-change every ~30s–few minutes. It
is a logging and fallback input, **not** a fast safety sensor. Do not write
code that assumes it is fresh.

## Style

- `from __future__ import annotations`, type hints throughout
- stdlib logging, module-level `log = logging.getLogger(__name__)`
- dataclasses for config and value objects
- all thresholds in `config.yaml` → `config.py`; no magic numbers in logic
