"""Tests for the hardware registry + scenario resolution (hardware.py)."""
import json
import pytest

from mouse_thermo.config import Config
from mouse_thermo.hardware import HardwareRegistry, apply


def reg_dict():
    return {
        "coordinator": {"device": "COM4", "baudrate": 115200, "database": "zigbee.db"},
        "actuators": {
            "plug_a": {"type": "zigbee_plug", "ieee": "aa:bb", "endpoint": 1},
        },
        "sensors": {
            "esp_east": {"type": "esp32_hamsterpod", "port": "COM6", "probe": "t1"},
            "snzb": {"type": "snzb02_zigbee", "ieee": "cc:dd"},
            "reader": {"type": "urh2_rfid", "port": "COM5", "baudrate": 38400},
        },
    }


def test_registry_loads_and_validates():
    reg = HardwareRegistry.from_dict(reg_dict())
    assert reg.actuator("plug_a").params["ieee"] == "aa:bb"
    assert reg.sensor("esp_east").type == "esp32_hamsterpod"


def test_unknown_type_rejected():
    d = reg_dict()
    d["actuators"]["bad"] = {"type": "mystery_brand", "ieee": "x"}
    with pytest.raises(ValueError, match="unknown type"):
        HardwareRegistry.from_dict(d)


def test_missing_required_param_rejected():
    d = reg_dict()
    d["sensors"]["noport"] = {"type": "esp32_hamsterpod"}   # no 'port'
    with pytest.raises(ValueError, match="missing required"):
        HardwareRegistry.from_dict(d)


def test_missing_type_rejected():
    d = reg_dict()
    d["actuators"]["notype"] = {"ieee": "x"}
    with pytest.raises(ValueError, match="missing 'type'"):
        HardwareRegistry.from_dict(d)


def test_apply_fills_zigbee_and_esp32():
    reg = HardwareRegistry.from_dict(reg_dict())
    cfg = Config(simulate=True)
    cfg.actuator = "plug_a"
    cfg.ambient_sensor = "esp_east"
    apply(reg, cfg)
    assert cfg.zigbee.device == "COM4"
    assert cfg.zigbee.plug_ieee == "aa:bb" and cfg.zigbee.plug_endpoint == 1
    assert cfg.esp32.enabled is True and cfg.esp32.port == "COM6"
    assert cfg.esp32.role == "ambient" and cfg.esp32.probe == "t1"


def test_apply_body_rfid_and_snzb_ambient():
    reg = HardwareRegistry.from_dict(reg_dict())
    cfg = Config(simulate=True)
    cfg.actuator = "plug_a"
    cfg.ambient_sensor = "snzb"
    cfg.body_sensor = "reader"
    apply(reg, cfg)
    assert cfg.zigbee.sensor_ieee == "cc:dd"
    assert cfg.rfid.enabled is True
    assert cfg.rfid.port == "COM5" and cfg.rfid.baudrate == 38400


def test_missing_reference_rejected():
    reg = HardwareRegistry.from_dict(reg_dict())
    cfg = Config(simulate=True)
    cfg.actuator = "nope"
    with pytest.raises(ValueError, match="not in the hardware registry"):
        apply(reg, cfg)


def test_role_mismatch_rejected():
    """An ambient-only device can't be assigned as the body sensor."""
    reg = HardwareRegistry.from_dict(reg_dict())
    cfg = Config(simulate=True)
    cfg.actuator = "plug_a"
    cfg.body_sensor = "snzb"     # SNZB-02 is ambient-only
    with pytest.raises(ValueError, match="ambient"):
        apply(reg, cfg)


def test_full_scenario_via_config_load(tmp_path):
    """End to end: a scenario YAML referencing a hardware.json resolves into a
    valid Config with the device blocks filled and validate() passing."""
    (tmp_path / "hardware.json").write_text(json.dumps(reg_dict()))
    (tmp_path / "cool.yaml").write_text(
        "hardware_file: hardware.json\n"
        "actuator: plug_a\n"
        "ambient_sensor: esp_east\n"
        "control:\n"
        "  mode: cool\n"
        "  body_setpoint_c: 32.0\n"
        "  ambient_setpoint_c: 20.0\n"
        "safety:\n"
        "  body_min_c: 30.0\n"
        "  ambient_min_c: 15.0\n"
        "sensors:\n"
        "  body_valid_range: [26.0, 43.0]\n"
        "  ambient_valid_range: [5.0, 60.0]\n"
    )
    # Not simulate: plug_ieee must be filled from the registry for validate to pass.
    cfg = Config.load(str(tmp_path / "cool.yaml"))
    assert cfg.zigbee.plug_ieee == "aa:bb"
    assert cfg.esp32.enabled is True and cfg.esp32.role == "ambient"
    assert cfg.control.mode == "cool"


def reg_dict_with_tc():
    """Registry dict that includes a thermocouple_serial sensor."""
    d = reg_dict()
    d["sensors"]["tc_probe"] = {"type": "thermocouple_serial", "port": "COM7"}
    return d


def test_thermocouple_type_resolves():
    reg = HardwareRegistry.from_dict(reg_dict_with_tc())
    cfg = Config(simulate=True)
    cfg.actuator = "plug_a"
    cfg.body_sensor = "tc_probe"
    apply(reg, cfg)
    assert cfg.thermocouple.enabled is True
    assert cfg.thermocouple.port == "COM7"
    assert cfg.thermocouple.role == "body"


def test_thermocouple_missing_port_rejected():
    d = reg_dict_with_tc()
    d["sensors"]["tc_noport"] = {"type": "thermocouple_serial"}  # no port
    with pytest.raises(ValueError, match="missing required"):
        HardwareRegistry.from_dict(d)


def test_noambient_scenario_loads(tmp_path):
    """A scenario with no ambient_sensor must load and validate without error."""
    reg = reg_dict_with_tc()
    # Also need a plug and the thermocouple in the registry.
    (tmp_path / "hardware.json").write_text(json.dumps(reg))
    (tmp_path / "noambient.yaml").write_text(
        "hardware_file: hardware.json\n"
        "actuator: plug_a\n"
        "body_sensor: tc_probe\n"
        "control:\n"
        "  mode: cool\n"
        "  body_setpoint_c: 29.5\n"
        "  ambient_setpoint_c: 20.0\n"
        "safety:\n"
        "  body_min_c: 28.0\n"
        "  ambient_min_c: 15.0\n"
        "sensors:\n"
        "  body_valid_range: [24.0, 42.0]\n"
        "  ambient_valid_range: [5.0, 60.0]\n"
    )
    cfg = Config.load(str(tmp_path / "noambient.yaml"))
    assert cfg.thermocouple.enabled is True
    assert cfg.ambient_sensor is None   # no ambient sensor reference
    assert cfg.esp32.enabled is False   # ESP32 not referenced
