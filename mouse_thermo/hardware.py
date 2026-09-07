"""Hardware registry + scenario resolution.

Two layers, so several device brands can coexist and each experiment
("scenario") picks which hardware it uses:

  * a **hardware registry** (per machine, e.g. `hardware.local.json`) lists the
    devices physically present, each with a `type` and its connection params;
  * a **scenario** (the config YAML) sets the protocol (mode, setpoints, safety)
    and *references* devices by their registry id (`actuator`,
    `ambient_sensor`, `body_sensor`).

`apply()` resolves those references into the existing `Config` device blocks
(`zigbee` / `rfid` / `esp32`), so nothing downstream (main.py wiring, the
control loop, safety) has to change. Adding a NEW device BRAND is done here:
add its `type` to ACTUATOR_TYPES / SENSOR_TYPES with the params it needs and a
branch in `apply`/`_apply_sensor` (and, for a genuinely different transport, a
new `Plug`/`SensorSource` adapter it maps onto). Unknown types and missing
params crash loudly -- a rig must never run half-configured.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict

log = logging.getLogger(__name__)

# The extension point for new brands. Each entry: required param keys + optional
# params with their defaults. `type` selects which adapter the id maps onto.
ACTUATOR_TYPES: Dict[str, dict] = {
    # Sonoff (and any zigpy-driven) smart plug -> ZigbeePlug in zigbee/app.py.
    "zigbee_plug": {"required": ("ieee",), "optional": {"endpoint": 1}},
}
SENSOR_TYPES: Dict[str, dict] = {
    # hamsterpod single-board ESP32-S2 -> Esp32Source (binary USB-CDC frames).
    "esp32_hamsterpod": {"required": ("port",),
                         "optional": {"baudrate": 115200, "probe": "t1",
                                      "sensor_id": ""}},
    # UID Devices URH-2 reader -> RfidChipSource (AnyCage serial).
    "urh2_rfid": {"required": ("port",),
                  "optional": {"baudrate": 38400, "transponder_id": ""}},
    # Sonoff SNZB-02 ambient sensor over Zigbee -> ZigbeeSensorListener.
    "snzb02_zigbee": {"required": ("ieee",), "optional": {"endpoint": 1}},
}


@dataclass
class DeviceSpec:
    id: str
    type: str
    params: dict


@dataclass
class HardwareRegistry:
    coordinator: dict = field(default_factory=dict)   # zigbee dongle
    actuators: Dict[str, DeviceSpec] = field(default_factory=dict)
    sensors: Dict[str, DeviceSpec] = field(default_factory=dict)

    # ---- construction ------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: dict) -> "HardwareRegistry":
        def specs(section: str) -> Dict[str, DeviceSpec]:
            out: Dict[str, DeviceSpec] = {}
            for dev_id, d in (raw.get(section) or {}).items():
                d = dict(d)
                typ = d.pop("type", None)
                if not typ:
                    raise ValueError(f"hardware {section}.{dev_id}: missing 'type'")
                out[dev_id] = DeviceSpec(dev_id, typ, d)
            return out

        reg = cls(coordinator=dict(raw.get("coordinator") or {}),
                  actuators=specs("actuators"), sensors=specs("sensors"))
        reg.validate()
        return reg

    @classmethod
    def load(cls, path: str) -> "HardwareRegistry":
        with open(path) as f:
            return cls.from_dict(json.load(f) or {})

    # ---- validation --------------------------------------------------------

    def validate(self) -> None:
        for dev in self.actuators.values():
            self._check(dev, ACTUATOR_TYPES, "actuator")
        for dev in self.sensors.values():
            self._check(dev, SENSOR_TYPES, "sensor")

    @staticmethod
    def _check(dev: DeviceSpec, table: Dict[str, dict], kind: str) -> None:
        if dev.type not in table:
            raise ValueError(
                f"{kind} '{dev.id}': unknown type {dev.type!r}; "
                f"known: {sorted(table)}")
        for req in table[dev.type]["required"]:
            if req not in dev.params:
                raise ValueError(
                    f"{kind} '{dev.id}' (type {dev.type}): missing required "
                    f"param {req!r}")

    # ---- lookup ------------------------------------------------------------

    def actuator(self, dev_id: str) -> DeviceSpec:
        if dev_id not in self.actuators:
            raise ValueError(
                f"scenario references actuator {dev_id!r} not in the hardware "
                f"registry (have: {sorted(self.actuators)})")
        return self.actuators[dev_id]

    def sensor(self, dev_id: str) -> DeviceSpec:
        if dev_id not in self.sensors:
            raise ValueError(
                f"scenario references sensor {dev_id!r} not in the hardware "
                f"registry (have: {sorted(self.sensors)})")
        return self.sensors[dev_id]


def _param(spec: DeviceSpec, key: str, table: Dict[str, dict]):
    """Value for `key`: the spec's own, else the type's documented default."""
    if key in spec.params:
        return spec.params[key]
    return table[spec.type]["optional"][key]


def apply(registry: HardwareRegistry, cfg) -> None:
    """Resolve a scenario's device references (`cfg.actuator` /
    `cfg.ambient_sensor` / `cfg.body_sensor`) into cfg's zigbee/rfid/esp32
    blocks. Called by Config.load BEFORE validate(), so a bad reference or an
    incoherent result still crashes loudly."""
    co = registry.coordinator
    if co.get("device"):
        cfg.zigbee.device = co["device"]
    if "baudrate" in co:
        cfg.zigbee.baudrate = co["baudrate"]
    if "flow_control" in co:
        cfg.zigbee.flow_control = co["flow_control"]
    if co.get("database"):
        cfg.zigbee.database = co["database"]

    if cfg.actuator:
        spec = registry.actuator(cfg.actuator)
        if spec.type == "zigbee_plug":
            cfg.zigbee.plug_ieee = spec.params["ieee"]
            cfg.zigbee.plug_endpoint = _param(spec, "endpoint", ACTUATOR_TYPES)
        else:  # unreachable while zigbee_plug is the only actuator type
            raise ValueError(f"actuator type {spec.type!r} has no wiring yet")

    if cfg.ambient_sensor:
        _apply_sensor(cfg, registry.sensor(cfg.ambient_sensor), role="ambient")
    if cfg.body_sensor:
        _apply_sensor(cfg, registry.sensor(cfg.body_sensor), role="body")


def _apply_sensor(cfg, spec: DeviceSpec, role: str) -> None:
    if spec.type == "snzb02_zigbee":
        if role != "ambient":
            raise ValueError(f"snzb02_zigbee '{spec.id}' can only be an ambient "
                             f"sensor, not {role}")
        cfg.zigbee.sensor_ieee = spec.params["ieee"]
        cfg.zigbee.sensor_endpoint = _param(spec, "endpoint", SENSOR_TYPES)
    elif spec.type == "esp32_hamsterpod":
        cfg.esp32.enabled = True
        cfg.esp32.port = spec.params["port"]
        cfg.esp32.baudrate = _param(spec, "baudrate", SENSOR_TYPES)
        cfg.esp32.role = role
        cfg.esp32.probe = _param(spec, "probe", SENSOR_TYPES)
        cfg.esp32.sensor_id = _param(spec, "sensor_id", SENSOR_TYPES)
    elif spec.type == "urh2_rfid":
        if role != "body":
            raise ValueError(f"urh2_rfid '{spec.id}' reads body temp; it can't "
                             f"be the {role} sensor")
        cfg.rfid.enabled = True
        cfg.rfid.port = spec.params["port"]
        cfg.rfid.baudrate = _param(spec, "baudrate", SENSOR_TYPES)
        cfg.rfid.transponder_id = _param(spec, "transponder_id", SENSOR_TYPES)
    else:  # unreachable; validate() rejects unknown types first
        raise ValueError(f"sensor type {spec.type!r} has no wiring yet")
