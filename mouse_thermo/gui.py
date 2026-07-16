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
        # Apply the combo's default selection ("Manual OFF") to the actual
        # flags -- connecting currentIndexChanged happens after addItem(),
        # so the initial index-0 selection never fired the handler on its own.
        self._on_freerun_mode_changed(self.combo_freerun.currentIndex())

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
                safety_bypass=self.safety_bypass,
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

        freerun_box = QGroupBox("Freerun (overrides the automatic controller, "
                                 "never a safety LOCKOUT)")
        freerun_layout = QHBoxLayout(freerun_box)
        freerun_layout.addWidget(QLabel("Lamp mode:"))
        self.combo_freerun = QComboBox()
        # "Manual OFF" first/default: on startup you're just watching the
        # signals come in, lamp held off, before ever engaging AUTO -- not
        # AUTO-by-default, which immediately evaluates LOCKOUT before you've
        # had a chance to check anything.
        self.combo_freerun.addItem("Manual OFF", userData="off")
        self.combo_freerun.addItem("Manual ON", userData="on")
        self.combo_freerun.addItem("AUTO (automatic control)", userData="auto")
        self.combo_freerun.currentIndexChanged.connect(self._on_freerun_mode_changed)
        freerun_layout.addWidget(self.combo_freerun)
        outer.addWidget(freerun_box)
        self.freerun_widgets = [self.combo_freerun]

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

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Loop mode:"))
        self.combo_loop_mode = QComboBox()
        self.combo_loop_mode.addItem("Closed loop (automatic control)", userData="closed_loop")
        self.combo_loop_mode.addItem("Open loop (fixed lamp state, no feedback)", userData="open_loop")
        mode_row.addWidget(self.combo_loop_mode)
        rec_layout.addLayout(mode_row)

        rec_btn_row = QHBoxLayout()
        self.btn_record = QPushButton("Start Recording")
        self.btn_record.clicked.connect(self._toggle_recording)
        self.lbl_recording = QLabel("not recording")
        rec_btn_row.addWidget(self.btn_record)
        rec_btn_row.addWidget(self.lbl_recording)
        rec_layout.addLayout(rec_btn_row)
        outer.addWidget(rec_box)
        self.recording_mode_widgets = [
            self.combo_loop_mode, self.edit_animal_id, self.edit_output_dir, self.btn_browse_output,
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

    # ---- manual control ------------------------------------------------------

    def _on_freerun_mode_changed(self, index: int) -> None:
        mode = self.combo_freerun.itemData(index)
        if mode == "on":
            self.manual_on.set()
            self.manual_override.set()
        elif mode == "off":
            self.manual_on.clear()
            self.manual_override.set()
        else:  # "auto"
            self.manual_override.clear()

    def _set_freerun_combo(self, mode: str) -> None:
        """Update the displayed selection without re-triggering the handler
        (used when Recording start/stop changes manual_override/manual_on
        itself, so the dropdown doesn't silently drift out of sync)."""
        i = self.combo_freerun.findData(mode)
        if i >= 0 and self.combo_freerun.currentIndex() != i:
            self.combo_freerun.blockSignals(True)
            self.combo_freerun.setCurrentIndex(i)
            self.combo_freerun.blockSignals(False)

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

        mode = self.combo_loop_mode.currentData()

        if mode == "closed_loop":
            # Honest closed-loop data: the automatic controller must be the
            # only thing commanding the lamp.
            self.manual_override.clear()
            self._set_freerun_combo("auto")
        else:
            # Open loop: freeze whatever the lamp is doing right now (set it
            # via Freerun first if you want a specific state) and hold it --
            # no automatic regulation, no further manual changes once
            # recording starts.
            current = self.handle.plug.state()
            if current:
                self.manual_on.set()
                self._set_freerun_combo("on")
            else:
                self.manual_on.clear()
                self._set_freerun_combo("off")
            self.manual_override.set()

        output_dir = self.edit_output_dir.text().strip() or os.path.abspath("recordings")
        date_str = datetime.datetime.now().strftime("%y%m%d")
        session_num = self._next_session_number(output_dir, date_str, animal_id)
        path = os.path.join(output_dir, f"{date_str}_{animal_id}_{session_num}.jsonl")
        self.handle.recording.start(path, mode, self.cfg.to_dict())

        for w in self.freerun_widgets + self.recording_mode_widgets:
            w.setEnabled(False)
        self.btn_record.setText("Stop Recording")
        self.lbl_recording.setText(f"recording [{mode}] -> {path}")
        self.lbl_recording.setStyleSheet("")

    def _stop_recording(self) -> None:
        self.handle.recording.stop()
        for w in self.freerun_widgets + self.recording_mode_widgets:
            w.setEnabled(True)
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
        if self.handle.recording.active:
            self.lbl_mode.setText(f"RECORDING [{self.handle.recording.mode}]")
        elif self.manual_override.is_set():
            self.lbl_mode.setText("FREERUN (manual override)")
        else:
            self.lbl_mode.setText("AUTO")
        if self.safety_bypass.is_set():
            self.lbl_mode.setText(self.lbl_mode.text() + "  [SAFETY BYPASS ACTIVE]")

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
