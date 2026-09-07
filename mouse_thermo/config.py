"""Configuration. Every threshold is here -- no magic numbers in logic files."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional, Tuple

import yaml


@dataclass
class SafetyConfig:
    # Hard ceilings (mode: heat). Crossing these latches a lockout.
    ambient_max_c: float = 32.0
    body_max_c: float = 38.5
    # Hard FLOORS (mode: cool). The mirror image of the ceilings: with a
    # Peltier/cooler the hazard is hypothermia, so a temperature AT/BELOW these
    # latches a lockout. Unused in heat mode. For a floor breach to actually
    # reach this supervisor, the plausibility gate must accept sub-floor values
    # -- so sensors.*_valid_range's LOW end must sit below the floor (validate()
    # enforces this), exactly mirroring how the hyperthermia protocol raises the
    # HIGH end above the ceiling.
    ambient_min_c: float = 15.0
    body_min_c: float = 30.0
    # Lockout releases only when temp recovers this far past the limit:
    # in heat mode below (ceiling - hysteresis); in cool mode above
    # (floor + hysteresis). Positive evidence of returning to safety.
    lockout_release_hysteresis_c: float = 1.0
    # If BOTH temperature sources are unusable for this long -> lamp off.
    all_sensors_stale_s: float = 60.0
    # Lamp commanded ON continuously for this long -> off + alert.
    # Catches dead bulb, stuck sensor, mis-sited probe.
    max_continuous_on_s: float = 900.0
    # Main loop must kick the watchdog at least this often.
    watchdog_timeout_s: float = 30.0


@dataclass
class ControlConfig:
    # "heat" -- actuator ON adds heat (heat lamp): regulate UP to setpoint,
    #           hard limits are ceilings, fail-safe OFF drifts DOWN to room.
    # "cool" -- actuator ON removes heat (Peltier): regulate DOWN to setpoint,
    #           hard limits are floors, fail-safe OFF drifts UP to room.
    # The two are exact mirrors; every comparison direction flips on this flag.
    # OFF is the safe state in BOTH modes because the box passively returns
    # toward (survivable) room temperature -- so the whole fail-cold skeleton
    # (OFF at startup/exception/exit, watchdog forces OFF, safety only vetoes)
    # is unchanged. The residual hazard is a STUCK-ON actuator (runaway hot in
    # heat mode, runaway cold in cool mode); that is what max_continuous_on_s
    # and the hardware cutoff guard against.
    mode: str = "heat"
    body_setpoint_c: float = 36.5
    body_deadband_c: float = 0.3          # heat: on below sp-db, off above sp+db
                                          # cool: on above sp+db, off below sp-db
    ambient_setpoint_c: float = 28.0      # used in FALLBACK mode only
    ambient_deadband_c: float = 0.5
    loop_period_s: float = 5.0
    min_on_s: float = 30.0                # relay protection / anti-chatter
    min_off_s: float = 30.0
    # Pulse ("chopped lamp") mode: heat in short bursts so the RFID reader,
    # which the lamp's EMI silences while it runs, recovers in the OFF gaps
    # and reads body temp there. Adjustable live from the GUI.
    pulse_on_s: float = 1.0
    pulse_off_s: float = 1.0


@dataclass
class SensorConfig:
    # Plausibility gates. Values outside are rejected, not clamped.
    body_valid_range: Tuple[float, float] = (30.0, 43.0)
    ambient_valid_range: Tuple[float, float] = (5.0, 60.0)
    # Staleness. Sonoff SNZB-02 reports on-change/slowly -> generous.
    body_stale_after_s: float = 30.0
    ambient_stale_after_s: float = 300.0


@dataclass
class ZigbeeConfig:
    device: str = "/dev/ttyACM0"          # ZBDongle-E
    baudrate: int = 115200
    flow_control: Optional[str] = None    # "software" for some ZBDongle-E fw
    database: str = "zigbee.db"
    plug_ieee: str = ""                   # e.g. "00:12:4b:00:2a:bc:de:f0"
    plug_endpoint: int = 1
    sensor_ieee: str = ""
    sensor_endpoint: int = 1
    permit_join_s: int = 0                # >0 only when pairing


@dataclass
class RfidConfig:
    enabled: bool = False
    port: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    transponder_id: str = ""              # empty = accept any


@dataclass
class Esp32Config:
    enabled: bool = False
    port: str = "/dev/ttyUSB1"
    baudrate: int = 115200
    role: str = "ambient"                 # "ambient" | "body" | "log_only"
    # Which value from the hamsterpod frame feeds the channel:
    #   t1 / t2  -- the two DS18B20 probes
    #   ir_mean  -- mean of the AMG8833 8x8 array
    #   ir_max   -- hottest pixel of the array
    probe: str = "t1"
    # Optional: only accept frames from this ESP-NOW node id (the firmware's
    # packet.id, e.g. "esp32-s2-sensor1"). Empty accepts any node -- fine with
    # one node, but set it once more than one is broadcasting.
    sensor_id: str = ""


@dataclass
class NeucamsConfig:
    # UDP trigger to the neucams camera software. When a recording starts, send
    # the run name + `start`; on stop send `stop`, so cameras acquire in sync
    # with the temperature log. neucams must have udp_enable + matching port.
    enabled: bool = False
    host: str = "127.0.0.1"               # neucams machine; 127.0.0.1 if same PC
    port: int = 9999                      # neucams server_port


@dataclass
class Config:
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    control: ControlConfig = field(default_factory=ControlConfig)
    sensors: SensorConfig = field(default_factory=SensorConfig)
    zigbee: ZigbeeConfig = field(default_factory=ZigbeeConfig)
    rfid: RfidConfig = field(default_factory=RfidConfig)
    esp32: Esp32Config = field(default_factory=Esp32Config)
    neucams: NeucamsConfig = field(default_factory=NeucamsConfig)
    log_path: str = "session.jsonl"
    # Default folder the GUI proposes for recordings (operator can still change
    # it per session). Relative paths resolve against the CWD.
    recordings_dir: str = "recordings"
    simulate: bool = False

    def validate(self, *, require_plug_ieee: bool = True) -> None:
        """Crash loudly on incoherent config rather than run unsafely."""
        c, s, sen = self.control, self.safety, self.sensors

        if c.mode not in ("heat", "cool"):
            raise ValueError(f"control.mode must be 'heat' or 'cool', got {c.mode!r}")

        if c.mode == "cool":
            # Mirror of the heat-mode checks below: in cool mode the binding
            # hard limits are the FLOORS, and the setpoint sits above them.
            if c.body_setpoint_c <= s.body_min_c:
                raise ValueError(
                    f"cool mode: body_setpoint_c ({c.body_setpoint_c}) must be "
                    f"above body_min_c ({s.body_min_c})"
                )
            if c.ambient_setpoint_c <= s.ambient_min_c:
                raise ValueError(
                    f"cool mode: ambient_setpoint_c ({c.ambient_setpoint_c}) must "
                    f"be above ambient_min_c ({s.ambient_min_c})"
                )
            # A floor breach can only trigger a lockout if a sub-floor reading
            # survives the plausibility gate -- so the valid range must extend
            # BELOW the floor. Mirror of the hyperthermia protocol raising the
            # range's HIGH end above the ceiling (see CLAUDE.md).
            if not (sen.body_valid_range[0] < s.body_min_c):
                raise ValueError(
                    f"cool mode: body_valid_range low end "
                    f"({sen.body_valid_range[0]}) must be below body_min_c "
                    f"({s.body_min_c}) so a floor breach is not discarded as "
                    f"implausible before safety can see it"
                )
            if not (sen.ambient_valid_range[0] < s.ambient_min_c):
                raise ValueError(
                    f"cool mode: ambient_valid_range low end "
                    f"({sen.ambient_valid_range[0]}) must be below ambient_min_c "
                    f"({s.ambient_min_c})"
                )
        else:  # heat
            if c.body_setpoint_c >= s.body_max_c:
                raise ValueError(
                    f"body_setpoint_c ({c.body_setpoint_c}) must be below "
                    f"body_max_c ({s.body_max_c})"
                )
            if c.ambient_setpoint_c >= s.ambient_max_c:
                raise ValueError(
                    f"ambient_setpoint_c ({c.ambient_setpoint_c}) must be below "
                    f"ambient_max_c ({s.ambient_max_c})"
                )
        if not (sen.body_valid_range[0] < sen.body_valid_range[1]):
            raise ValueError("body_valid_range malformed")
        if not (sen.ambient_valid_range[0] < sen.ambient_valid_range[1]):
            raise ValueError("ambient_valid_range malformed")
        if not (sen.body_valid_range[0] <= c.body_setpoint_c <= sen.body_valid_range[1]):
            raise ValueError("body_setpoint_c outside body_valid_range")
        if s.watchdog_timeout_s <= c.loop_period_s:
            raise ValueError("watchdog_timeout_s must exceed loop_period_s")
        if c.pulse_on_s <= 0 or c.pulse_off_s <= 0:
            raise ValueError("pulse_on_s and pulse_off_s must be positive")
        if self.esp32.enabled:
            # Crash loudly rather than degrade: a typo here would otherwise
            # only surface as a silently dead sensor at runtime.
            if self.esp32.probe not in ("t1", "t2", "ir_mean", "ir_max"):
                raise ValueError(
                    f"esp32.probe must be t1|t2|ir_mean|ir_max, got {self.esp32.probe!r}")
            if self.esp32.role not in ("ambient", "body", "log_only"):
                raise ValueError(
                    f"esp32.role must be ambient|body|log_only, got {self.esp32.role!r}")
        if self.neucams.enabled:
            if not self.neucams.host:
                raise ValueError("neucams.host required when neucams.enabled")
            if not (0 < self.neucams.port < 65536):
                raise ValueError(f"neucams.port out of range: {self.neucams.port}")
        if require_plug_ieee and not self.simulate and not self.zigbee.plug_ieee:
            raise ValueError("zigbee.plug_ieee required when not simulating")

    @classmethod
    def load(
        cls, path: str, *, simulate: bool | None = None, require_plug_ieee: bool = True
    ) -> "Config":
        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        def sub(kls, key):
            d = raw.get(key, {}) or {}
            known = {f for f in kls.__dataclass_fields__}
            unknown = set(d) - known
            if unknown:
                raise ValueError(f"unknown keys in '{key}': {sorted(unknown)}")
            return kls(**d)

        cfg = cls(
            safety=sub(SafetyConfig, "safety"),
            control=sub(ControlConfig, "control"),
            sensors=sub(SensorConfig, "sensors"),
            zigbee=sub(ZigbeeConfig, "zigbee"),
            rfid=sub(RfidConfig, "rfid"),
            esp32=sub(Esp32Config, "esp32"),
            neucams=sub(NeucamsConfig, "neucams"),
            log_path=raw.get("log_path", "session.jsonl"),
            recordings_dir=raw.get("recordings_dir", "recordings"),
            simulate=raw.get("simulate", False),
        )
        # tuples survive YAML as lists
        cfg.sensors.body_valid_range = tuple(cfg.sensors.body_valid_range)
        cfg.sensors.ambient_valid_range = tuple(cfg.sensors.ambient_valid_range)
        if simulate:
            cfg.simulate = True
        cfg.validate(require_plug_ieee=require_plug_ieee)
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)
