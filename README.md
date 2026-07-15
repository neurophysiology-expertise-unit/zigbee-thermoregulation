# mouse_thermo

Closed-loop heat-lamp control for mouse thermoregulation. Pure Python, no
broker, no Home Assistant dependency.

```
RFID/UID reader ──┐
ESP32 probe ──────┼──► SensorChannel ──► Safety ──► Controller ──► ZigbeePlug ──► lamp
Sonoff SNZB-02 ───┘    (stale+range)     (veto)      (hysteresis)   (zigpy/bellows)
                                            │
                                       Watchdog ──► force OFF
                                            │
                                    SessionLogger (one JSONL, one clock)
```

## Install

```bash
pip install -r mouse_thermo/requirements.txt
```

Run all commands below from the repo root (the parent of `mouse_thermo/`) —
the package uses relative imports, so entry points must be invoked as
`-m mouse_thermo.<module>`, not run directly from inside the package directory.

## Bring-up order

1. **Pair devices.** `python -m mouse_thermo.pair --config mouse_thermo/config.yaml --seconds 120`
   Press the button on the plug and the SNZB-02. Copy the printed IEEE
   addresses into `zigbee.plug_ieee` / `zigbee.sensor_ieee`.
   If the dongle won't start, set `flow_control: software` — some ZBDongle-E
   firmware needs it.
2. **Run the tests.** `pytest mouse_thermo/test_safety.py -q` — 11 tests, all
   must pass. They cover latching, hysteresis, staleness, plausibility
   rejection, and the "dwell must never delay a safety off" invariant.
3. **Simulate.** `python -m mouse_thermo.main --config mouse_thermo/config.yaml --simulate`
   Drives a fake plug and a crude thermal plant. Exercises the whole loop with
   nothing plugged in.
4. **Dry run on real hardware, no animal.** Point the lamp at the box with the
   SNZB-02 inside. Watch it approach `ambient_setpoint_c` and confirm it
   latches when you set `ambient_max_c` low (e.g. 26) on purpose.
5. **Wire the two adapters** (below), then run with an animal.

## The two adapters you fill in

Both are the only files that touch your repos. Nothing else changes.

- `sensors/rfid_chip.py` → `_read_one()` must return `(tag_id, body_temp_c)`
  or `None` on timeout. Point me at the UID repo and I'll write this.
- `sensors/esp32_serial.py` → `_parse()` must return a float from one line.
  Currently handles a bare float or `{"t": 27.4}`. Set `esp32.role` to
  `ambient` or `body` depending on where the probe sits.

Validation, staleness, threading, and safety are already handled — the
adapters only need to produce numbers.

## Control logic

| State | Condition | Action |
|---|---|---|
| `NORMAL` | fresh, plausible body temp | heat toward `body_setpoint_c`, **still capped by ambient** |
| `FALLBACK` | no body temp (mouse away from antenna, RFID off, chip silent) | heat toward `ambient_setpoint_c` only |
| `LOCKOUT` | any hard limit, or no usable sensor at all | lamp off |

The ambient cap is active in **every** state. A cold mouse never licenses an
overheated box — `test_ambient_cap_overrides_cold_body` pins this.

Latches auto-release only on positive evidence of cooling (fresh ambient
reading, below ceiling minus hysteresis). The stuck-on latch
(`max_continuous_on_s`, catches a dead bulb or a probe that fell off the
animal) is **sticky** — it needs `reset_latch()` after you physically check the
rig, so it can't oscillate.

## Design invariants (deliberate, please keep)

- **Never suppress errors.** A dead sensor thread logs an exception and lets
  its channel go stale. Staleness is what makes the system fail cold.
- **Never fabricate a fallback value.** `get()` returns `None`, never a
  last-known or a zero. `None` means "I don't know" and is treated as unsafe.
- **Crash loudly on incoherent config.** `Config.validate()` refuses a setpoint
  above a hard max rather than running with it.
- **Safety only vetoes, never commands on.** A bug in `safety.py` fails cold.
- **Dwell time never delays an OFF.** Anti-chatter applies to turn-on only.

## What this does NOT protect against

The software watchdog covers a stalled loop. It does **not** cover:

- the Python process being killed
- the host losing power or the USB dongle dropping
- the Zigbee link failing while the plug is latched ON

In all three, **the plug stays in whatever state it was last commanded to** —
and if that was ON, nothing in this repo turns it off.

Given a live animal under a heat lamp, put an **inline bimetallic thermostat or
thermal cutoff on the lamp circuit**, set a few degrees above `ambient_max_c`.
It costs a few euro, needs no software, and is the only layer that holds when
everything here is dead. The Sonoff sensor reports on-change every 30s–few
minutes; it is a logging and fallback input, not a last line of defence.

This is also the kind of thing your ethics committee will ask about
specifically, so it's worth having the answer be "yes, hardware cutoff" rather
than "yes, Python."
