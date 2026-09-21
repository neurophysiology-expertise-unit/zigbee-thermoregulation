"""Recording-summary tests. Read-only analysis, but the numbers reach a paper,
so they are pinned here."""
import json

from mouse_thermo import recordings


def write_session(tmp_path, name, samples, config, events=()):
    """Write a session .jsonl the way SessionLogger does, then corrupt the last
    line -- an interrupted session is the normal case, not the exotic one."""
    p = tmp_path / name
    with open(p, "w") as f:
        f.write(json.dumps({"type": "session_start", "wall_clock": 1.0,
                            "config": config}) + "\n")
        for s in samples:
            f.write(json.dumps({"type": "sample", **s}) + "\n")
        for e in events:
            f.write(json.dumps({"type": "event", **e}) + "\n")
        f.write("\n")                       # blank line
        f.write('{"type": "sample", "body_c":')   # truncated final write
    return str(p)


COOL_CFG = {"control": {"mode": "cool", "ambient_deadband_c": 0.2,
                        "body_deadband_c": 0.3}}

# A closed-loop cool run: ambient walked 30.0 -> 29.45 against a 29.5 setpoint.
# The body reader is blind while the actuator runs (EMI) and reads in the gaps.
COOL_SAMPLES = [
    {"t_mono": 1000.0 + i, "ambient_c": v, "ambient_setpoint_c": 29.5,
     "body_c": None if on else 29.8, "raw_rfid_age_s": 40.0 if on else 1.0,
     "lamp_cmd": on, "state": st, "record_mode": "closed_loop",
     "ground_truth": "ambient"}
    for i, (v, on, st) in enumerate([
        (30.00, True,  "NORMAL"),
        (29.90, True,  "NORMAL"),
        (29.80, True,  "NORMAL"),
        (29.70, True,  "NORMAL"),   # first sample in band (|v-sp| <= 0.2)
        (29.60, True,  "NORMAL"),
        (29.50, True,  "NORMAL"),
        (29.45, False, "NORMAL"),
        (29.50, False, "LOCKOUT"),
    ])
]


def test_parse_survives_blank_and_truncated_lines(tmp_path):
    path = write_session(tmp_path, "cool.jsonl", COOL_SAMPLES, COOL_CFG)
    rec = recordings.parse(path)
    assert len(rec["ambient"]) == 8, "the truncated tail must be skipped, not counted"
    assert rec["config"]["control"]["mode"] == "cool"


def test_summary_pins_the_control_quality_numbers(tmp_path):
    path = write_session(tmp_path, "cool.jsonl", COOL_SAMPLES, COOL_CFG)
    row = recordings.summarize(recordings.parse(path))

    assert row["mode"] == "cool"
    assert row["record_mode"] == "closed_loop"
    assert row["duration_s"] == 7.0
    assert row["ambient_setpoint_c"] == 29.5
    # in band from the 29.70 sample onward: 5 of 8
    assert row["ambient_pct_in_band"] == 62.5
    assert row["ambient_time_to_band_s"] == 3.0
    # 6 of the 7 one-second intervals had the actuator on -- TIME weighted,
    # not 6/8 samples
    assert round(row["actuator_duty_pct"], 1) == 85.7
    assert row["actuator_transitions"] == 1
    assert row["lockout_samples"] == 1 and row["lockout_episodes"] == 1
    assert round(row["ambient_min_c"], 2) == 29.45


def test_body_dropout_splits_by_actuator_state(tmp_path):
    """The EMI finding as a number: blind under the actuator, reading in the gaps."""
    path = write_session(tmp_path, "cool.jsonl", COOL_SAMPLES, COOL_CFG)
    row = recordings.summarize(recordings.parse(path))
    assert row["body_dropout_pct_actuator_on"] == 100.0
    assert row["body_dropout_pct_actuator_off"] == 0.0
    assert row["rfid_age_median_s_actuator_on"] == 40.0
    assert row["rfid_age_median_s_actuator_off"] == 1.0


def test_baseline_with_no_setpoint_reports_none_not_zero(tmp_path):
    """A baseline session regulates nothing. Band metrics must be absent, never
    0 -- a 0 there would read as 'it held the setpoint 0% of the time'."""
    samples = [{"t_mono": 1000.0 + i, "ambient_c": 30.0 + 0.05 * i,
                "lamp_cmd": False, "state": "NORMAL", "record_mode": "open_loop"}
               for i in range(6)]
    path = write_session(tmp_path, "baseline.jsonl", samples, COOL_CFG)
    row = recordings.summarize(recordings.parse(path))

    assert row["ambient_pct_in_band"] is None
    assert row["ambient_time_to_band_s"] is None
    assert row["actuator_duty_pct"] == 0.0
    assert row["ambient_n"] == 6
    assert row["record_mode"] == "open_loop"


def test_duty_is_time_weighted_not_sample_weighted(tmp_path):
    """A 5 s ON burst and a 1 s OFF gap give one sample each. Counting samples
    calls that 50%; it is 83%."""
    samples = []
    t, on = 0.0, True
    for _ in range(11):   # 10 intervals: five 5 s ON, five 1 s OFF
        samples.append({"t_mono": 1000.0 + t, "ambient_c": 30.0, "lamp_cmd": on,
                        "state": "NORMAL"})
        t += 5.0 if on else 1.0
        on = not on
    path = write_session(tmp_path, "pulsed.jsonl", samples, COOL_CFG)
    row = recordings.summarize(recordings.parse(path))
    assert round(row["actuator_duty_pct"]) == 83


def test_gated_body_readings_are_counted_and_kept_raw(tmp_path):
    """The gate nulls body_c below body_valid_range; raw_rfid_c still carries
    the excursion. Losing it silently is how a real cooling run looks like no
    data at all."""
    samples = [{"t_mono": 1000.0 + i, "body_c": None, "raw_rfid_c": 30.0 - 0.1 * i,
                "lamp_cmd": True, "state": "NORMAL"} for i in range(5)]
    path = write_session(tmp_path, "gated.jsonl", samples, COOL_CFG)
    row = recordings.summarize(recordings.parse(path))
    assert row["body_n"] == 0, "the gated channel really is empty"
    assert row["body_gated_pct"] == 100.0
    assert row["raw_body_first_c"] == 30.0
    assert round(row["raw_body_last_c"], 1) == 29.6


def test_csv_round_trip(tmp_path):
    a = write_session(tmp_path, "baseline.jsonl",
                      [{"t_mono": 1.0, "ambient_c": 30.0, "lamp_cmd": False,
                        "state": "NORMAL"}], COOL_CFG)
    b = write_session(tmp_path, "cool.jsonl", COOL_SAMPLES, COOL_CFG)
    out = tmp_path / "sessions.csv"
    assert recordings.main([a, b, "--csv", str(out)]) == 0
    text = out.read_text().splitlines()
    assert len(text) == 3, "header + one row per session"
    assert text[0].startswith("session,mode,record_mode")


def test_stats_text_reports_mode_and_band(tmp_path):
    path = write_session(tmp_path, "cool.jsonl", COOL_SAMPLES, COOL_CFG)
    text = recordings.control_stats_text(recordings.parse(path))
    assert text.startswith("Mode: cool")
    assert "within ±0.2°C of setpoint" in text
    assert "LOCKOUT" in text


def test_xlsx_has_readme_summary_and_one_sheet_per_session(tmp_path):
    import pytest
    openpyxl = pytest.importorskip("openpyxl")
    path = write_session(tmp_path, "cool.jsonl", COOL_SAMPLES, COOL_CFG)
    out = tmp_path / "out.xlsx"
    recordings.write_xlsx([recordings.parse(path)], str(out))
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["README", "Summary", "cool"]
    ws = wb["cool"]
    header = [c.value for c in ws[1]]
    assert header[:4] == ["time_s", "time_utc", "chip_temp_raw_c", "chip_temp_c"]
    assert ws.max_row == 9, "header + 8 samples"
    # a gated/missing reading is an EMPTY cell, never a fake 0
    assert ws.cell(row=2, column=header.index("chip_temp_c") + 1).value is None
