"""Live monitor + manual-override GUI (PySide6), running in the SAME process
as the automated control loop.

This is not a separate tool: only one process can hold the Zigbee dongle and
the RFID reader's serial port at a time, so the GUI drives main.run() in a
background thread and reads its SessionHandle -- it does not open its own
connections to any device.

    python -m mouse_thermo.gui --config config.local.yaml
    python -m mouse_thermo.gui --config config.yaml --simulate

Install with: pip install -r requirements-gui.txt
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import os
import re
import sys
import threading
import time
from typing import Optional

from PySide6.QtGui import QDoubleValidator
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from .config import Config
from .main import SessionHandle, run

log = logging.getLogger("mouse_thermo.gui")

PLOT_WINDOW_OPTIONS_S = (1, 2, 4, 10)  # selectable sweep-plot window widths
DEFAULT_PLOT_WINDOW_S = 4
UI_PERIOD_MS = 500     # UI refresh rate; independent of the control loop's own period
SWEEP_RESOLUTION = 200  # samples across one full sweep, independent of poll rate
SWEEP_ERASE_FRACTION = 0.05  # fraction of the sweep width blanked just ahead of the cursor
PLOT_Y_MIN, PLOT_Y_MAX = 20.0, 50.0  # fixed temp axis -- not autoscaled to the data


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config):
        super().__init__()
        self.setWindowTitle("Mouse Thermo -- Live Monitor")
        self.cfg = cfg
        self.handle: Optional[SessionHandle] = None

        # Read from the GUI thread, written from the GUI thread, consulted
        # from the control loop's own thread -- threading.Event is safe for
        # exactly this cross-thread pattern (see main.py's run()).
        self.manual_override = threading.Event()
        self.manual_on = threading.Event()
        # Deliberately never read from config, never persisted, always
        # starts cleared -- an explicit per-session operator acknowledgement,
        # not a setting that could silently carry into a later real run.
        self.safety_bypass = threading.Event()
        # Pulse ("chopped lamp"): auto-cycles the lamp on/off so the RFID
        # reader recovers in the off gaps. A Freerun feature; rides inside
        # the same manual-override gating so a safety LOCKOUT still wins.
        self.pulse_active = threading.Event()

        # Plain string owned by the GUI thread, read cross-thread by the
        # control loop via a getter (safe under the GIL; no Qt widget is
        # touched from the control thread). Default ambient: body/RFID is
        # unreliable while the lamp runs (EMI), so ambient is the trustworthy
        # regulation source out of the box.
        self._ground_truth = "ambient"

        # Captured before any GUI override, so unchecking "use custom
        # setpoints" has something to revert to.
        self._orig_body_setpoint_c = cfg.control.body_setpoint_c
        self._orig_ambient_setpoint_c = cfg.control.ambient_setpoint_c

        self._t0 = time.monotonic()
        self._plot_window_s: float = DEFAULT_PLOT_WINDOW_S
        # Radar/ECG-monitor style sweep: fixed-size buffers indexed by phase
        # within the current window, NOT a scrolling history. reset whenever
        # the window size changes since the index<->time mapping changes.
        self._sweep_body = [float("nan")] * SWEEP_RESOLUTION
        self._sweep_amb = [float("nan")] * SWEEP_RESOLUTION
        self._sweep_xs = [i / SWEEP_RESOLUTION * DEFAULT_PLOT_WINDOW_S for i in range(SWEEP_RESOLUTION)]
        self._last_sweep_idx: Optional[int] = None

        self._build_ui()
        # Apply the default mode (Freerun, lamp off) and default ground truth
        # explicitly: the initial checked/selected states were set during UI
        # construction and never fired their handlers on their own.
        self._set_mode("freerun")
        self._ground_truth = self.combo_ground_truth.currentData()

        self.session_thread = threading.Thread(target=self._run_session, daemon=True)
        self.session_thread.start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(UI_PERIOD_MS)

    # ---- session plumbing --------------------------------------------------

    def _run_session(self) -> None:
        try:
            asyncio.run(run(
                self.cfg,
                manual_override=self.manual_override,
                manual_on=self.manual_on,
                pulse_active=self.pulse_active,
                safety_bypass=self.safety_bypass,
                ground_truth_getter=lambda: self._ground_truth,
                on_ready=self._on_ready,
            ))
        except Exception:
            log.exception("control session crashed")

    def _on_ready(self, handle: SessionHandle) -> None:
        self.handle = handle

    # ---- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Save recordings to:"))
        self.edit_output_dir = QLineEdit(os.path.abspath("recordings"))
        output_row.addWidget(self.edit_output_dir)
        self.btn_browse_output = QPushButton("Browse...")
        self.btn_browse_output.clicked.connect(self._browse_output_dir)
        output_row.addWidget(self.btn_browse_output)
        outer.addLayout(output_row)

        bypass_row = QHBoxLayout()
        self.chk_safety_bypass = QCheckBox("Safety bypass (TESTING ONLY) -- lets manual "
                                            "control override even a hard-ceiling lockout")
        self.chk_safety_bypass.toggled.connect(self._on_safety_bypass_toggled)
        bypass_row.addWidget(self.chk_safety_bypass)
        outer.addLayout(bypass_row)

        self.lbl_bypass_banner = QLabel(
            "⚠ SAFETY BYPASS ACTIVE -- hard-ceiling lockout can be overridden ⚠"
        )
        self.lbl_bypass_banner.setAlignment(Qt.AlignCenter)
        self.lbl_bypass_banner.setStyleSheet(
            "background-color: red; color: white; font-weight: bold; padding: 6px;"
        )
        self.lbl_bypass_banner.setVisible(False)
        outer.addWidget(self.lbl_bypass_banner)

        grid = QGridLayout()
        self.lbl_body = QLabel("--")
        self.lbl_ambient = QLabel("--")
        self.lbl_raw_rfid = QLabel("--")
        self.lbl_lamp = QLabel("--")
        self.lbl_power = QLabel("--")
        self.lbl_plug_link = QLabel("--")
        self.lbl_ambient_link = QLabel("--")
        self.lbl_state = QLabel("--")
        self.lbl_reason = QLabel("--")
        self.lbl_mode = QLabel("starting...")
        rows = [
            ("Body temp (C)", self.lbl_body),
            ("Ambient temp (C)", self.lbl_ambient),
            ("Raw RFID read (unvalidated)", self.lbl_raw_rfid),
            ("Lamp state", self.lbl_lamp),
            ("Power (W)", self.lbl_power),
            ("Plug link", self.lbl_plug_link),
            ("Ambient sensor link", self.lbl_ambient_link),
            ("Controller state", self.lbl_state),
            ("Reason", self.lbl_reason),
            ("Mode", self.lbl_mode),
        ]
        for i, (name, lbl) in enumerate(rows):
            grid.addWidget(QLabel(name + ":"), i, 0)
            grid.addWidget(lbl, i, 1)
        outer.addLayout(grid)

        mode_box = QGroupBox("Mode")
        mode_v = QVBoxLayout(mode_box)

        # One Freerun / Auto toggle, exclusive. Freerun = you drive the lamp
        # by hand; Auto = the closed-loop controller drives it. Default
        # Freerun (lamp held off) so startup is observe-only, not AUTO
        # immediately evaluating LOCKOUT before you've checked anything.
        toggle_row = QHBoxLayout()
        self.btn_mode_freerun = QPushButton("Freerun")
        self.btn_mode_auto = QPushButton("Auto")
        for b in (self.btn_mode_freerun, self.btn_mode_auto):
            b.setCheckable(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_group.addButton(self.btn_mode_freerun)
        self.mode_group.addButton(self.btn_mode_auto)
        self.btn_mode_freerun.setChecked(True)
        self.btn_mode_freerun.clicked.connect(lambda: self._set_mode("freerun"))
        self.btn_mode_auto.clicked.connect(lambda: self._set_mode("auto"))
        toggle_row.addWidget(self.btn_mode_freerun)
        toggle_row.addWidget(self.btn_mode_auto)
        mode_v.addLayout(toggle_row)

        # Freerun sub-controls: manual lamp ON / OFF, plus Pulse.
        self.freerun_row = QHBoxLayout()
        self.freerun_row.addWidget(QLabel("Freerun lamp:"))
        self.btn_lamp_on = QPushButton("Lamp ON")
        self.btn_lamp_off = QPushButton("Lamp OFF")
        pon, poff = self.cfg.control.pulse_on_s, self.cfg.control.pulse_off_s
        self.btn_pulse = QPushButton(f"Pulse ({pon:g}s on / {poff:g}s off)")
        self.btn_pulse.setCheckable(True)
        self.btn_lamp_on.clicked.connect(self._manual_lamp_on)
        self.btn_lamp_off.clicked.connect(self._manual_lamp_off)
        self.btn_pulse.clicked.connect(self._toggle_pulse)
        self.freerun_row.addWidget(self.btn_lamp_on)
        self.freerun_row.addWidget(self.btn_lamp_off)
        self.freerun_row.addWidget(self.btn_pulse)
        mode_v.addLayout(self.freerun_row)

        # Auto sub-control: which sensor is the regulation ground truth.
        gt_row = QHBoxLayout()
        gt_row.addWidget(QLabel("Auto ground truth:"))
        self.combo_ground_truth = QComboBox()
        self.combo_ground_truth.addItem("Ambient (Zigbee)", userData="ambient")
        self.combo_ground_truth.addItem("Body (RFID)", userData="body")
        self.combo_ground_truth.currentIndexChanged.connect(self._on_ground_truth_changed)
        gt_row.addWidget(self.combo_ground_truth)
        mode_v.addLayout(gt_row)

        outer.addWidget(mode_box)
        # Widgets locked while a recording is active (mode must not change
        # mid-trial). The lamp buttons are additionally gated by mode below.
        self.mode_widgets = [self.btn_mode_freerun, self.btn_mode_auto,
                             self.combo_ground_truth]

        setpoint_box = QGroupBox("Closed-loop setpoints (live-tunable, "
                                  "checked against the hard safety max before applying)")
        setpoint_grid = QGridLayout(setpoint_box)
        temp_validator = QDoubleValidator(0.0, 100.0, 2)

        setpoint_grid.addWidget(QLabel("Body setpoint (C):"), 0, 0)
        self.edit_body_setpoint = QLineEdit(f"{self.cfg.control.body_setpoint_c:.2f}")
        self.edit_body_setpoint.setValidator(temp_validator)
        self.edit_body_setpoint.editingFinished.connect(self._on_setpoint_edited)
        setpoint_grid.addWidget(self.edit_body_setpoint, 0, 1)

        setpoint_grid.addWidget(QLabel("Ambient setpoint (C):"), 1, 0)
        self.edit_ambient_setpoint = QLineEdit(f"{self.cfg.control.ambient_setpoint_c:.2f}")
        self.edit_ambient_setpoint.setValidator(temp_validator)
        self.edit_ambient_setpoint.editingFinished.connect(self._on_setpoint_edited)
        setpoint_grid.addWidget(self.edit_ambient_setpoint, 1, 1)

        self.chk_apply_setpoints = QCheckBox("Use these setpoints for closed loop")
        self.chk_apply_setpoints.toggled.connect(self._on_setpoint_toggle)
        setpoint_grid.addWidget(self.chk_apply_setpoints, 2, 0, 1, 2)

        self.lbl_setpoint_status = QLabel("using config file values")
        setpoint_grid.addWidget(self.lbl_setpoint_status, 3, 0, 1, 2)
        outer.addWidget(setpoint_box)

        rec_box = QGroupBox("Recording")
        rec_layout = QVBoxLayout(rec_box)

        animal_row = QHBoxLayout()
        animal_row.addWidget(QLabel("Animal ID:"))
        self.edit_animal_id = QLineEdit()
        self.edit_animal_id.setPlaceholderText("e.g. CA001")
        animal_row.addWidget(self.edit_animal_id)
        rec_layout.addLayout(animal_row)

        # Loop mode is no longer a separate choice -- it is derived from the
        # current Mode: recording in Auto is closed-loop, recording in Freerun
        # is open-loop. This removes the old redundant "AUTO vs Closed loop"
        # double-selector the operator flagged.
        note = QLabel("Loop mode follows Mode above: Auto -> closed loop, "
                      "Freerun -> open loop.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        rec_layout.addWidget(note)

        rec_btn_row = QHBoxLayout()
        self.btn_record = QPushButton("Start Recording")
        self.btn_record.clicked.connect(self._toggle_recording)
        self.lbl_recording = QLabel("not recording")
        rec_btn_row.addWidget(self.btn_record)
        rec_btn_row.addWidget(self.lbl_recording)
        rec_layout.addLayout(rec_btn_row)
        outer.addWidget(rec_box)
        self.recording_mode_widgets = [
            self.edit_animal_id, self.edit_output_dir, self.btn_browse_output,
        ]

        window_box = QGroupBox("Plot window (recent-only, not the whole session)")
        window_layout = QHBoxLayout(window_box)
        window_layout.addWidget(QLabel("Window:"))
        self.combo_plot_window = QComboBox()
        for seconds in PLOT_WINDOW_OPTIONS_S:
            self.combo_plot_window.addItem(f"{seconds}s", userData=seconds)
        self.combo_plot_window.setCurrentIndex(PLOT_WINDOW_OPTIONS_S.index(DEFAULT_PLOT_WINDOW_S))
        self.combo_plot_window.currentIndexChanged.connect(
            lambda i: self._set_plot_window(self.combo_plot_window.itemData(i))
        )
        window_layout.addWidget(self.combo_plot_window)
        outer.addWidget(window_box)

        self.fig = Figure(figsize=(6, 3))
        self.ax = self.fig.add_subplot(111)
        # Radar/monitor look: no box, no x-axis -- position along x is the
        # sweep itself, not a labeled time axis. Keep only the y-axis, since
        # that's the one thing you actually read a value off of.
        for side in ("top", "right", "bottom"):
            self.ax.spines[side].set_visible(False)
        self.ax.xaxis.set_visible(False)
        self.ax.set_ylabel("temp (C)")
        (self.line_body,) = self.ax.plot([], [], label="body")
        (self.line_amb,) = self.ax.plot([], [], label="ambient")
        self.cursor_line = self.ax.axvline(0.0, color="0.5", linewidth=1, linestyle="--")
        self.ax.set_xlim(0.0, self._plot_window_s)
        self.ax.set_ylim(PLOT_Y_MIN, PLOT_Y_MAX)  # fixed -- not autoscaled to the data
        self.ax.legend(loc="upper right")
        self.canvas = FigureCanvas(self.fig)
        outer.addWidget(self.canvas)

    # ---- safety bypass --------------------------------------------------------

    def _on_safety_bypass_toggled(self, checked: bool) -> None:
        if checked:
            self.safety_bypass.set()
        else:
            self.safety_bypass.clear()
        self.lbl_bypass_banner.setVisible(checked)

    # ---- plot window --------------------------------------------------------

    def _set_plot_window(self, seconds: float) -> None:
        self._plot_window_s = seconds
        # The index<->time mapping changes with the window width -- old
        # buffer contents would plot at the wrong x position otherwise.
        self._sweep_body = [float("nan")] * SWEEP_RESOLUTION
        self._sweep_amb = [float("nan")] * SWEEP_RESOLUTION
        self._sweep_xs = [i / SWEEP_RESOLUTION * seconds for i in range(SWEEP_RESOLUTION)]
        self._last_sweep_idx = None
        self.ax.set_xlim(0.0, seconds)

    # ---- mode: Freerun / Auto ------------------------------------------------

    def _set_mode(self, mode: str) -> None:
        """Freerun: manual override on, operator drives the lamp by hand.
        Auto: manual override off, the closed-loop controller drives it."""
        if mode == "freerun":
            self.manual_override.set()   # controller decision is overridden...
            # ...by whatever Lamp ON/OFF the operator last pressed; default OFF
            # until they press ON, so entering Freerun never turns heat on.
            self._sync_mode_controls("freerun")
        else:  # "auto"
            self.manual_override.clear()
            self._sync_mode_controls("auto")

    def _sync_mode_controls(self, mode: str) -> None:
        freerun = (mode == "freerun")
        # Lamp/pulse buttons only make sense in Freerun; ground truth only in
        # Auto. All are additionally disabled while a recording is active
        # (handled in _start_recording / _stop_recording).
        recording = self.handle is not None and self.handle.recording.active
        self.btn_lamp_on.setEnabled(freerun and not recording)
        self.btn_lamp_off.setEnabled(freerun and not recording)
        self.btn_pulse.setEnabled(freerun and not recording)
        self.combo_ground_truth.setEnabled((not freerun) and not recording)
        # Leaving Freerun disengages pulsing entirely.
        if not freerun and self.pulse_active.is_set():
            self.pulse_active.clear()
            self.btn_pulse.setChecked(False)
        # Keep the toggle buttons' checked state in sync (e.g. when a
        # recording forces a mode).
        self.btn_mode_freerun.setChecked(freerun)
        self.btn_mode_auto.setChecked(not freerun)

    def _set_mode_silently(self, mode: str) -> None:
        """Force the displayed mode + flags without the user having clicked --
        used by recording start/stop. Buttons are exclusive-grouped, so
        setChecked alone is enough; flags are set directly."""
        if mode == "freerun":
            self.manual_override.set()
        else:
            self.manual_override.clear()
        self._sync_mode_controls(mode)

    def _manual_lamp_on(self) -> None:
        self._cancel_pulse()          # a steady command overrides pulsing
        self.manual_on.set()
        self.manual_override.set()

    def _manual_lamp_off(self) -> None:
        self._cancel_pulse()
        self.manual_on.clear()
        self.manual_override.set()

    def _cancel_pulse(self) -> None:
        if self.pulse_active.is_set():
            self.pulse_active.clear()
        self.btn_pulse.setChecked(False)

    def _toggle_pulse(self, checked: bool) -> None:
        if checked:
            # Pulsing IS a manual override -- the controller no longer drives
            # the lamp; the on/off cycle does (subject to safety LOCKOUT).
            self.manual_override.set()
            self.pulse_active.set()
        else:
            self.pulse_active.clear()
            # Fall back to a steady OFF so releasing pulse never leaves the
            # lamp stuck on mid-cycle.
            self.manual_on.clear()
            self.manual_override.set()

    def _on_ground_truth_changed(self, index: int) -> None:
        self._ground_truth = self.combo_ground_truth.itemData(index)

    # ---- closed-loop setpoints -----------------------------------------------

    def _uncheck_silently(self) -> None:
        # Plain setChecked(False) would re-fire _on_setpoint_toggle(False),
        # which reverts to the original config values AND overwrites the
        # rejection message we just set -- block the signal for this one call.
        self.chk_apply_setpoints.blockSignals(True)
        self.chk_apply_setpoints.setChecked(False)
        self.chk_apply_setpoints.blockSignals(False)

    def _on_setpoint_toggle(self, checked: bool) -> None:
        if self.handle is None:
            return
        if checked:
            self._apply_setpoints()
        else:
            self.handle.cfg.control.body_setpoint_c = self._orig_body_setpoint_c
            self.handle.cfg.control.ambient_setpoint_c = self._orig_ambient_setpoint_c
            self.lbl_setpoint_status.setText("using config file values")
            self.lbl_setpoint_status.setStyleSheet("")

    def _on_setpoint_edited(self) -> None:
        # Only takes effect if the checkbox is already ticked -- typing
        # alone must not silently change what the controller is targeting.
        if self.chk_apply_setpoints.isChecked():
            self._apply_setpoints()

    def _apply_setpoints(self) -> None:
        if self.handle is None:
            return
        try:
            body_sp = float(self.edit_body_setpoint.text())
            amb_sp = float(self.edit_ambient_setpoint.text())
        except ValueError:
            self.lbl_setpoint_status.setText("invalid number -- not applied")
            self.lbl_setpoint_status.setStyleSheet("color: red; font-weight: bold;")
            self._uncheck_silently()
            return

        safety = self.handle.cfg.safety
        if body_sp >= safety.body_max_c:
            self.lbl_setpoint_status.setText(
                f"REJECTED: body {body_sp:.2f}C must be below hard max {safety.body_max_c:.2f}C")
            self.lbl_setpoint_status.setStyleSheet("color: red; font-weight: bold;")
            self._uncheck_silently()
            return
        if amb_sp >= safety.ambient_max_c:
            self.lbl_setpoint_status.setText(
                f"REJECTED: ambient {amb_sp:.2f}C must be below hard max {safety.ambient_max_c:.2f}C")
            self.lbl_setpoint_status.setStyleSheet("color: red; font-weight: bold;")
            self._uncheck_silently()
            return

        # Controller.step() reads cfg.control fresh every tick, and this IS
        # that same object (not a copy) -- takes effect on the next tick,
        # no restart needed. Plain attribute assignment, safe enough for a
        # setpoint float read cross-thread (same precedent as last_decision).
        self.handle.cfg.control.body_setpoint_c = body_sp
        self.handle.cfg.control.ambient_setpoint_c = amb_sp
        self.lbl_setpoint_status.setText(f"ACTIVE: body={body_sp:.2f}C, ambient={amb_sp:.2f}C")
        self.lbl_setpoint_status.setStyleSheet("color: green;")

    # ---- recording ----------------------------------------------------------

    def _toggle_recording(self) -> None:
        if self.handle is None:
            return
        if self.handle.recording.active:
            self._stop_recording()
        else:
            self._start_recording()

    @staticmethod
    def _sanitize_animal_id(raw: str) -> str:
        raw = raw.strip()
        return re.sub(r'[^A-Za-z0-9_-]', "", raw)

    @staticmethod
    def _next_session_number(directory: str, date_str: str, animal_id: str) -> int:
        pattern = re.compile(rf"^{re.escape(date_str)}_{re.escape(animal_id)}_(\d+)\.jsonl$")
        try:
            existing = os.listdir(directory)
        except FileNotFoundError:
            return 1
        nums = [int(m.group(1)) for name in existing if (m := pattern.match(name))]
        return max(nums, default=0) + 1

    def _browse_output_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select folder to save recordings", self.edit_output_dir.text()
        )
        if chosen:
            self.edit_output_dir.setText(chosen)

    def _start_recording(self) -> None:
        animal_id = self._sanitize_animal_id(self.edit_animal_id.text())
        if not animal_id:
            self.lbl_recording.setText("enter an Animal ID before recording")
            self.lbl_recording.setStyleSheet("color: red; font-weight: bold;")
            return

        # Loop mode is derived from the current Mode, not chosen separately:
        # Auto -> closed loop, Freerun -> open loop. Whatever the operator has
        # already set (including which lamp state, in Freerun) is simply
        # frozen for the trial; nothing about the lamp changes at Start.
        in_auto = self.btn_mode_auto.isChecked()
        mode = "closed_loop" if in_auto else "open_loop"

        output_dir = self.edit_output_dir.text().strip() or os.path.abspath("recordings")
        date_str = datetime.datetime.now().strftime("%y%m%d")
        session_num = self._next_session_number(output_dir, date_str, animal_id)
        path = os.path.join(output_dir, f"{date_str}_{animal_id}_{session_num}.jsonl")
        self.handle.recording.start(path, mode, self.cfg.to_dict())

        # Lock everything that defines the trial: mode toggle, ground truth,
        # lamp buttons, animal id, output dir. The mode must not change under
        # a running recording (it would contradict the file's loop-mode label).
        # Lock the trial-defining controls. pulse_active itself is NOT
        # cleared -- a Freerun/open-loop recording may legitimately run WITH
        # the lamp pulsing (that's how you capture body temp in the gaps);
        # only the button is disabled so it can't be toggled mid-trial.
        for w in (self.mode_widgets + self.recording_mode_widgets
                  + [self.btn_lamp_on, self.btn_lamp_off, self.btn_pulse]):
            w.setEnabled(False)
        self.btn_record.setText("Stop Recording")
        gt = f", ground truth = {self._ground_truth}" if mode == "closed_loop" else ""
        self.lbl_recording.setText(f"recording [{mode}{gt}] -> {path}")
        self.lbl_recording.setStyleSheet("")

    def _stop_recording(self) -> None:
        self.handle.recording.stop()
        for w in self.mode_widgets + self.recording_mode_widgets:
            w.setEnabled(True)
        # Restore lamp-button enablement per the current mode (not blanket on).
        self._sync_mode_controls("freerun" if self.btn_mode_freerun.isChecked() else "auto")
        self.btn_record.setText("Start Recording")
        self.lbl_recording.setText("not recording")

    # ---- link-quality display -------------------------------------------------

    @staticmethod
    def _set_link_label(label: QLabel, age: Optional[float], *, stale_after_s: float,
                         none_text: str = "no contact yet / simulated") -> None:
        if age is None:
            label.setText(none_text)
            label.setStyleSheet("color: gray;")
        elif age <= stale_after_s:
            label.setText(f"OK ({age:.0f}s ago)")
            label.setStyleSheet("color: green;")
        else:
            label.setText(f"STALE ({age:.0f}s ago)")
            label.setStyleSheet("color: red; font-weight: bold;")

    # ---- polling tick -------------------------------------------------------

    def _tick(self) -> None:
        if self.handle is None:
            self.lbl_mode.setText("starting...")
            return

        now = time.monotonic()
        now_wall = time.time()  # last_seen_age is wall-clock based (zigpy), unlike everything else here
        body = self.handle.body_ch.get(now)
        amb = self.handle.amb_ch.get(now)
        lamp_state = self.handle.plug.state()
        power = self.handle.plug.power_w()
        decision = self.handle.last_decision

        raw = self.handle.rfid_source.last_raw_reading if self.handle.rfid_source is not None else None

        # Shared by both the label and the plot line below -- the plot was
        # only ever drawing the validated value, so the raw fallback never
        # showed up there even after it started showing in the label.
        body_display_value: Optional[float] = None
        if body is not None:
            body_display_value = body.value
            self.lbl_body.setText(f"{body.value:.2f}")
            self.lbl_body.setStyleSheet("")
        elif raw is not None:
            # Not what the safety system is acting on -- body_ch already
            # rejected this (out of the 30-46C physiological range, or just
            # stale) and the controller still sees "no usable temperature
            # source" regardless of what's shown here. Shown anyway, clearly
            # labeled, so bring-up testing can see the reader is alive
            # without touching the actual plausibility gate.
            tag_id, temp_c, _ = raw
            body_display_value = temp_c
            self.lbl_body.setText(f"{temp_c:.2f} (raw, NOT validated/used)")
            self.lbl_body.setStyleSheet("color: darkorange;")
        else:
            self.lbl_body.setText("stale/unknown")
            self.lbl_body.setStyleSheet("")

        self.lbl_ambient.setText(f"{amb.value:.2f}" if amb is not None else "stale/unknown")

        if raw is None:
            self.lbl_raw_rfid.setText("no contact yet / rfid disabled")
        else:
            tag_id, temp_c, t_read = raw
            self.lbl_raw_rfid.setText(f"tag {tag_id}: {temp_c:.1f}C ({now - t_read:.0f}s ago)")

        self.lbl_lamp.setText(
            "ON" if lamp_state is True else "OFF" if lamp_state is False else "unknown"
        )
        self.lbl_power.setText(f"{power:.1f}" if power is not None else "--")

        self._set_link_label(self.lbl_plug_link, self.handle.plug.last_seen_age(now_wall), stale_after_s=90)
        sensor_age = (
            self.handle.ambient_sensor.last_seen_age(now_wall)
            if self.handle.ambient_sensor is not None else None
        )
        self._set_link_label(self.lbl_ambient_link, sensor_age, stale_after_s=360,
                              none_text="no contact yet / not configured / simulated")
        if decision is not None:
            self.lbl_state.setText(decision.state.value)
            self.lbl_reason.setText(decision.reason)
        if self.pulse_active.is_set():
            base = "FREERUN (PULSE)"
        elif self.manual_override.is_set():
            base = "FREERUN (manual)"
        else:
            base = f"AUTO (ground truth: {self._ground_truth})"
        if self.handle.recording.active:
            base = f"RECORDING [{self.handle.recording.mode}] -- {base}"
        if self.safety_bypass.is_set():
            base += "  [SAFETY BYPASS ACTIVE]"
        self.lbl_mode.setText(base)

        # Radar/ECG-monitor sweep: position within the CURRENT cycle of the
        # window, not elapsed session time -- wraps back to 0 every
        # plot_window_s, like a line sweeping the screen left to right.
        window = self._plot_window_s
        phase = (now - self._t0) % window / window  # 0..1
        idx = int(phase * SWEEP_RESOLUTION) % SWEEP_RESOLUTION

        body_val = body_display_value if body_display_value is not None else float("nan")
        amb_val = amb.value if amb is not None else float("nan")

        # The cursor can advance several buffer slots between UI ticks (tick
        # period vs. window/resolution) -- fill the WHOLE span it swept
        # since last tick with the current value, not just the single
        # landing index, or most of the buffer stays stale between ticks
        # and the trace reads as disconnected fragments instead of a line.
        if self._last_sweep_idx is None:
            fill = [idx]
        else:
            span = (idx - self._last_sweep_idx) % SWEEP_RESOLUTION or SWEEP_RESOLUTION
            fill = [(self._last_sweep_idx + 1 + k) % SWEEP_RESOLUTION for k in range(span)]
        for j in fill:
            self._sweep_body[j] = body_val
            self._sweep_amb[j] = amb_val
        self._last_sweep_idx = idx

        # Blank a small span just ahead of the cursor -- that gap-ahead is
        # what makes it read as a sweep erasing the previous lap's trace
        # ahead of the beam, not a static plot.
        erase_n = max(1, int(SWEEP_RESOLUTION * SWEEP_ERASE_FRACTION))
        for k in range(1, erase_n + 1):
            j = (idx + k) % SWEEP_RESOLUTION
            self._sweep_body[j] = float("nan")
            self._sweep_amb[j] = float("nan")

        self.line_body.set_data(self._sweep_xs, self._sweep_body)
        self.line_amb.set_data(self._sweep_xs, self._sweep_amb)
        self.cursor_line.set_xdata([phase * window, phase * window])
        # No relim()/autoscale_view() -- both axes are fixed (x to the sweep
        # window, y to PLOT_Y_MIN/MAX), so nothing needs to move each tick.
        self.canvas.draw_idle()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self.handle is not None:
            if self.handle.recording.active:
                self.handle.recording.stop()
            self.handle.request_shutdown()
        event.accept()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--simulate", action="store_true")
    a = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    cfg = Config.load(a.config, simulate=a.simulate)

    app = QApplication(sys.argv)
    win = MainWindow(cfg)
    win.resize(720, 640)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
