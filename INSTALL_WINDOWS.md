# Windows install & driver guide

How to bring `mouse_thermo` up on a fresh Windows PC (10/11), including the
USB drivers and how to re-pair the Zigbee devices when you move the rig.

> **Safety first.** A live animal sits under the actuator this software drives.
> Read [CLAUDE.md](CLAUDE.md) "Invariants" and the hardware-cutoff note at the
> bottom of this file **before** running with an animal.

---

## 1. What you need

**Hardware** (this rig's actual devices — yours may differ in COM number):

| Device | Model | Typical port | USB driver |
|---|---|---|---|
| Zigbee coordinator | SONOFF **ZBDongle-E** (EFR32MG21 / EZSP) | e.g. `COM4` @ 115200 | Silicon Labs **CP210x** VCP |
| Smart plug (actuator) | SONOFF **S60ZBTPF** | over Zigbee | — |
| Ambient temp/humidity | SONOFF **SNZB-02P** | over Zigbee | — |
| RFID / body-temp reader | UID Devices **URH-2** (AnyCage) | e.g. `COM5` @ 38400 | USB-serial (FTDI or CP210x) |
| ESP32 ambient probe | hamsterpod single-board **ESP32-S2** | e.g. `COM6` @ 115200 | none (native USB-CDC, `VID_303A`) |

The ZBDongle-**E** uses the `bellows` (EZSP) stack. If you ever swap to a
ZBDongle-**P**, that needs `zigpy-znp` instead — different library.

**Software:** Python 3.11–3.13 (this machine runs 3.13.11 from miniconda),
plus the pip dependencies below. Git to clone the repos.

---

## 2. Install the USB drivers

Plug each USB device in **one at a time** and confirm it appears in **Device
Manager → Ports (COM & LPT)** before moving on. Note the COM number each gets.

1. **SONOFF ZBDongle-E** — Silicon Labs **CP2102N**. Windows 11 usually installs
   the CP210x VCP driver automatically over Windows Update. If it shows up as an
   unknown device, install the driver manually:
   Silicon Labs "CP210x USB to UART Bridge VCP Drivers"
   → <https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers>
2. **UID Devices URH-2 reader** — a USB-serial device. If it does not enumerate
   as a COM port, install the **FTDI VCP** driver
   (<https://ftdichip.com/drivers/vcp-drivers/>) or the CP210x driver above,
   depending on the chip inside your unit.
3. **ESP32-S2 (hamsterpod probe)** — **no driver needed**. The S2 has native
   USB-CDC; it appears as a COM port on its own (hardware id `VID_303A`).

**List the ports Python can see** (after step 4 sets up the venv):

```bat
.venv\Scripts\python -c "import serial.tools.list_ports as l; [print(p.device, p.description, p.hwid) for p in l.comports()]"
```

Match each device to its COM number here — you will put these numbers into
`config.local.yaml` in step 6.

---

## 3. Clone this repo

```bat
git clone https://github.com/neurophysiology-expertise-unit/zigbee-thermoregulation.git
cd zigbee-thermoregulation
```

---

## 4. Create the virtualenv and install dependencies

> **Use PowerShell (or cmd.exe) for all commands in this guide, not Git Bash.**
> Paths use Windows backslashes — Git Bash silently mangles them — and the
> `python` App Execution Alias (Microsoft Store redirect) is not resolved there
> either. Open PowerShell with **Win + X → Terminal** or **Win + R → `powershell`**.

From the repo root in **PowerShell**:

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r mouse_thermo\requirements-gui.txt
```

> **`python` vs `py`:** On a fresh Windows install, typing `python` may open the
> Microsoft Store (Windows App Execution Alias) instead of running Python. Use
> `py` (the Python Launcher for Windows) to create the venv. Once the venv
> exists, **always call `.venv\Scripts\python` directly** — that always hits the
> venv interpreter without needing to activate.

> **If you want to activate the venv** (so bare `python` works in the session):
> ```powershell
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> .venv\Scripts\Activate.ps1
> ```
> The `Set-ExecutionPolicy` line is needed once per terminal — PowerShell blocks
> unsigned scripts by default; `-Scope Process` limits the change to that window.
> **Do not use `source .venv/Scripts/activate`** — that is bash syntax and is a
> no-op in PowerShell.

`requirements-gui.txt` pulls in the core deps (`zigpy`, `bellows`, `pyserial`,
`PyYAML`, `pytest`) **plus** the GUI (`PySide6`, `matplotlib`, `numpy`).
For a headless/service box with no GUI, use `requirements.txt` instead.

> **PySide6 pin:** 6.11.1 fails to import on Windows (DLL load error); 6.8.0.2
> and 6.10.3 both work, so the requirement is pinned `>=6.8,<6.11`. If
> `python -c "from PySide6 import QtCore"` errors after install, that pin may
> need bumping — re-check before loosening it.

Verify the install:

```powershell
.venv\Scripts\python -c "from PySide6 import QtCore; print('Qt OK')"
```

---

## 5. Run the tests and the simulator (no hardware needed)

```bat
.venv\Scripts\python -m pytest mouse_thermo\test_safety.py -q
.venv\Scripts\python -m mouse_thermo.main --config mouse_thermo\config.yaml --simulate --max-seconds 30
```

The safety suite must be **all green** before you trust the rig. The simulator
drives a fake plug + thermal plant so you can watch the whole loop with nothing
plugged in. For a cool-mode (Peltier) dry run, point it at a `mode: cool`
config (see step 7).

---

## 6. Configure this rig — `config.local.yaml`

`mouse_thermo/config.yaml` holds **safe defaults and no device addresses**. The
real per-rig values (IEEE addresses, COM ports) live in
`mouse_thermo/config.local.yaml`, which is **gitignored** — so it does *not*
come with the clone and must be created on every new machine.

```bat
copy mouse_thermo\config.yaml mouse_thermo\config.local.yaml
```

Then edit `config.local.yaml` and fill in the parts that are machine-specific:

```yaml
zigbee:
  device: COM4                 # the ZBDongle-E's COM port (step 2)
  plug_ieee: ""                # filled by pairing, step 6b
  sensor_ieee: ""              # filled by pairing, step 6b
rfid:
  enabled: true
  port: COM5                   # the URH-2 reader
  baudrate: 38400
esp32:
  enabled: true
  port: COM6                   # the ESP32-S2 probe
  baudrate: 115200
  role: ambient
  probe: t1
```

> COM numbers are assigned by Windows per USB port and **can differ on the new
> machine** — always re-check with the `list_ports` snippet above. On this rig
> they happened to be COM4 (dongle), COM5 (RFID), COM6 (ESP32).

### 6b. Pair the Zigbee devices (required on every new machine)

The plug and sensor are paired into the coordinator's own database
(`zigbee.db`) and are **bound to whichever dongle did the pairing**. Moving to a
new PC (or a new dongle) means re-pairing from scratch:

```bat
.venv\Scripts\python -m mouse_thermo.pair --config mouse_thermo\config.local.yaml --seconds 120
```

Press the pairing button on the SONOFF plug, then on the SNZB-02P, while it
scans. Copy the printed IEEE addresses into `plug_ieee` / `sensor_ieee`.
If the dongle won't start, set `zigbee.flow_control: software` — some ZBDongle-E
firmware needs it.

---

## 7. Cool mode (Peltier) vs heat mode (lamp)

`control.mode` selects the direction:

- `mode: heat` (default) — actuator adds heat (heat lamp); regulate **up** to
  setpoint; hard limits are **ceilings**.
- `mode: cool` — actuator removes heat (Peltier on the same on/off plug);
  regulate **down** to setpoint; hard limits are **floors** (`body_min_c`,
  `ambient_min_c`). Set the sensor `*_valid_range` low end **below** the floor
  so a floor breach still reaches the safety supervisor.

A minimal cool-mode block (brain 36→32 °C clamp):

```yaml
control:
  mode: cool
  body_setpoint_c: 32.0
  ambient_setpoint_c: 20.0
safety:
  body_min_c: 30.0
  ambient_min_c: 15.0
sensors:
  body_valid_range: [26.0, 43.0]     # low end below body_min_c
```

`Config.validate()` refuses an incoherent cool config (setpoint below the floor,
or a valid range that would hide a floor breach), so a typo crashes loudly
rather than running unsafely.

---

## 7b. Scenarios + hardware registry (optional, recommended for multiple rigs)

Instead of putting hardware addresses and protocol in one `config.local.yaml`,
you can split them:

- **`hardware.local.json`** (per machine, gitignored) — the devices present,
  each with a `type` + params. Copy the template and fill it in:
  ```bat
  copy hardware.example.json hardware.local.json
  ```
  Supported `type`s: `zigbee_plug`, `esp32_hamsterpod`, `urh2_rfid`,
  `snzb02_zigbee` (see `mouse_thermo/hardware.py`). You can list several — e.g.
  two plugs of different brands, or two ESP32 probes — and scenarios choose
  which to use.
- **`scenarios/*.yaml`** — the protocol. `scenarios/heat_up.yaml` and
  `scenarios/cool_down.yaml` ship as examples. Each sets `mode` / setpoints /
  safety and references devices by id (`actuator`, `ambient_sensor`,
  `body_sensor`) plus `hardware_file: ../hardware.local.json`.

Launch a scenario the same way as any config:

```bat
.venv\Scripts\python -m mouse_thermo.gui --config scenarios\cool_down.yaml
```

or pick it from the **start screen** → *New session* (which opens the
`scenarios/` folder). An unknown device `type`, a missing param, or a scenario
referencing a device id that isn't in the registry all fail loudly at load.

## 8. Launch the live monitor

Double-click **`run_gui.bat`**, or:

```bat
.venv\Scripts\python -m mouse_thermo.gui --config mouse_thermo\config.local.yaml
```

`run_gui.bat` cd's to the repo root, checks the venv and `config.local.yaml`
exist, and keeps its console open on error so a crash is readable. The GUI has
three tabs: **Monitor** (live), **Setup** (mode, setpoints, recording), and
**Review** (load a finished `.jsonl` recording and scrub it to check how well
control held). Closing the window commands the actuator **OFF** on the way out.

---

## 9. Related repos (neu suite — `neurophysiology-expertise-unit`)

This controller talks to, or reuses protocols from, three sister repos:

- **neucams** — the camera acquisition software. `mouse_thermo` triggers it over
  UDP (port 9999: `folder=<name>`, `start`, `stop`) so cameras record in sync
  with the temperature log. Install and run it separately if you use cameras.
  → `github.com/neurophysiology-expertise-unit/neucams`
- **hamsterpod** — the ESP32-S2 firmware for the ambient probe. This rig runs
  the **single-board** `sensor_usb` firmware (custom), which writes the 290-byte
  binary frame straight to USB-CDC. Flash it from the hamsterpod repo.
  → `github.com/neurophysiology-expertise-unit/hamsterpod`
- **anycage-influx-gateway** — reference for the URH-2 reader's AnyCage serial
  protocol and arm sequence, which `sensors/rfid_chip.py` implements.
  → <https://github.com/neurophysiology-expertise-unit/anycage-influx-gateway>

You do **not** need these to run heat/cool control — only if you want cameras
(neucams), the wired ambient probe (hamsterpod), or to re-derive the RFID
protocol (anycage).

---

## 10. Hardware safety cutoff — not optional

The software watchdog covers a stalled control loop. It does **not** cover the
Python process dying, the host losing power, the USB dongle dropping, or the
Zigbee link failing — in all of those the plug **stays in its last commanded
state**. If that was ON, nothing in this repo turns it off.

- **Heat mode:** fit an inline **bimetallic thermostat / thermal cutoff** on the
  lamp circuit, set a few degrees above `ambient_max_c`.
- **Cool mode:** the mirror — a stuck-ON Peltier runs the animal cold, so fit a
  **low-temperature cutoff** that kills the Peltier's supply below a floor.

Either costs a few euro, needs no software, and is the only layer that holds
when everything here is dead. Your ethics committee will ask about it
specifically.
