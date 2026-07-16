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
from collections import deque
from typing import Optional

from PySide6.QtGui import QDoubleValidator
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from .config import Config
from .main import SessionHandle, run

log = logging.getLogger("mouse_thermo.gui")

PLOT_WINDOW_OPTIONS_S = (1, 2, 4, 10)  # selectable rolling-plot window widths
DEFAULT_PLOT_WINDOW_S = 4
UI_PERIOD_MS = 500  # UI refresh rate; independent of the control loop's own period


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

        # Captured before any GUI override, so unchecking "use custom
        # setpoints" has something to revert to.
        self._orig_body_setpoint_c = cfg.control.body_setpoint_c
        self._orig_ambient_setpoint_c = cfg.control.ambient_setpoint_c

        self._t0 = time.monotonic()
        self._t_hist: deque = deque()
        self._body_hist: deque = deque()
        self._amb_hist: deque = deque()
        self._plot_window_s: float = DEFAULT_PLOT_WINDOW_S

        self._build_ui()

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

        grid = QGridLayout()
        self.lbl_body = QLabel("--")
        self.lbl_ambient = QLabel("--")
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
        self.btn_on = QPushButton("Manual ON")
        self.btn_off = QPushButton("Manual OFF")
        self.btn_auto = QPushButton("Resume AUTO")
        self.btn_on.clicked.connect(self._manual_on)
        self.btn_off.clicked.connect(self._manual_off)
        self.btn_auto.clicked.connect(self._resume_auto)
        freerun_layout.addWidget(self.btn_on)
        freerun_layout.addWidget(self.btn_off)
        freerun_layout.addWidget(self.btn_auto)
        outer.addWidget(freerun_box)
        self.freerun_widgets = [self.btn_on, self.btn_off, self.btn_auto]

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
        self.radio_closed = QRadioButton("Closed loop (automatic control)")
        self.radio_open = QRadioButton("Open loop (fixed lamp state, no feedback)")
        self.radio_closed.setChecked(True)
        mode_row.addWidget(self.radio_closed)
        mode_row.addWidget(self.radio_open)
        rec_layout.addLayout(mode_row)

        rec_btn_row = QHBoxLayout()
        self.btn_record = QPushButton("Start Recording")
        self.btn_record.clicked.connect(self._toggle_recording)
        self.lbl_recording = QLabel("not recording")
        rec_btn_row.addWidget(self.btn_record)
        rec_btn_row.addWidget(self.lbl_recording)
        rec_layout.addLayout(rec_btn_row)
        outer.addWidget(rec_box)
        self.recording_mode_widgets = [self.radio_closed, self.radio_open, self.edit_animal_id]

        window_box = QGroupBox("Plot window (recent-only, not the whole session)")
        window_layout = QHBoxLayout(window_box)
        self.plot_window_group = QButtonGroup(self)
        self.plot_window_group.setExclusive(True)
        default_btn = None
        for seconds in PLOT_WINDOW_OPTIONS_S:
            btn = QPushButton(f"{seconds}s")
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, s=seconds: self._set_plot_window(s))
            self.plot_window_group.addButton(btn)
            window_layout.addWidget(btn)
            if seconds == DEFAULT_PLOT_WINDOW_S:
                default_btn = btn
        # Must setChecked() AFTER every button has joined the exclusive
        # QButtonGroup -- doing it during construction (before the group had
        # more than one member) left the wrong button visually highlighted,
        # even though self._plot_window_s itself was already correct.
        if default_btn is not None:
            default_btn.setChecked(True)
        outer.addWidget(window_box)

        self.fig = Figure(figsize=(6, 3))
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("time (s)")
        self.ax.set_ylabel("temp (C)")
        (self.line_body,) = self.ax.plot([], [], label="body")
        (self.line_amb,) = self.ax.plot([], [], label="ambient")
        self.ax.legend(loc="upper right")
        self.canvas = FigureCanvas(self.fig)
        outer.addWidget(self.canvas)

    # ---- plot window --------------------------------------------------------

    def _set_plot_window(self, seconds: float) -> None:
        self._plot_window_s = seconds

    # ---- manual control buttons --------------------------------------------

    def _manual_on(self) -> None:
        self.manual_on.set()
        self.manual_override.set()

    def _manual_off(self) -> None:
        self.manual_on.clear()
        self.manual_override.set()

    def _resume_auto(self) -> None:
        self.manual_override.clear()

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
    def _next_session_number(date_str: str, animal_id: str) -> int:
        pattern = re.compile(rf"^{re.escape(date_str)}_{re.escape(animal_id)}_(\d+)\.jsonl$")
        try:
            existing = os.listdir("recordings")
        except FileNotFoundError:
            return 1
        nums = [int(m.group(1)) for name in existing if (m := pattern.match(name))]
        return max(nums, default=0) + 1

    def _start_recording(self) -> None:
        animal_id = self._sanitize_animal_id(self.edit_animal_id.text())
        if not animal_id:
            self.lbl_recording.setText("enter an Animal ID before recording")
            self.lbl_recording.setStyleSheet("color: red; font-weight: bold;")
            return

        mode = "closed_loop" if self.radio_closed.isChecked() else "open_loop"

        if mode == "closed_loop":
            # Honest closed-loop data: the automatic controller must be the
            # only thing commanding the lamp.
            self.manual_override.clear()
        else:
            # Open loop: freeze whatever the lamp is doing right now (set it
            # via Freerun first if you want a specific state) and hold it --
            # no automatic regulation, no further manual changes once
            # recording starts.
            current = self.handle.plug.state()
            if current:
                self.manual_on.set()
            else:
                self.manual_on.clear()
            self.manual_override.set()

        date_str = datetime.datetime.now().strftime("%y%m%d")
        session_num = self._next_session_number(date_str, animal_id)
        path = f"recordings/{date_str}_{animal_id}_{session_num}.jsonl"
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

        self.lbl_body.setText(f"{body.value:.2f}" if body is not None else "stale/unknown")
        self.lbl_ambient.setText(f"{amb.value:.2f}" if amb is not None else "stale/unknown")
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

        t = now - self._t0
        self._t_hist.append(t)
        self._body_hist.append(body.value if body is not None else float("nan"))
        self._amb_hist.append(amb.value if amb is not None else float("nan"))
        # Deliberately only the selected recent window, not the whole
        # session -- drop anything older than that off the left, every tick.
        while self._t_hist and t - self._t_hist[0] > self._plot_window_s:
            self._t_hist.popleft()
            self._body_hist.popleft()
            self._amb_hist.popleft()

        self.line_body.set_data(self._t_hist, self._body_hist)
        self.line_amb.set_data(self._t_hist, self._amb_hist)
        self.ax.set_xlim(max(0.0, t - self._plot_window_s), max(t, self._plot_window_s))
        self.ax.relim()
        self.ax.autoscale_view(scalex=False)  # y only; x is the fixed sweep window above
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
