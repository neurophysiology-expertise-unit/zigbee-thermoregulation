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
import logging
import sys
import threading
import time
from collections import deque
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
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

HISTORY_S = 600.0   # rolling window for the live plot
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

        self._t0 = time.monotonic()
        self._t_hist: deque = deque()
        self._body_hist: deque = deque()
        self._amb_hist: deque = deque()

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
        self.lbl_state = QLabel("--")
        self.lbl_reason = QLabel("--")
        self.lbl_mode = QLabel("starting...")
        rows = [
            ("Body temp (C)", self.lbl_body),
            ("Ambient temp (C)", self.lbl_ambient),
            ("Lamp state", self.lbl_lamp),
            ("Power (W)", self.lbl_power),
            ("Controller state", self.lbl_state),
            ("Reason", self.lbl_reason),
            ("Mode", self.lbl_mode),
        ]
        for i, (name, lbl) in enumerate(rows):
            grid.addWidget(QLabel(name + ":"), i, 0)
            grid.addWidget(lbl, i, 1)
        outer.addLayout(grid)

        btn_box = QGroupBox("Manual control (overrides the automatic controller, "
                             "never a safety LOCKOUT)")
        btn_layout = QHBoxLayout(btn_box)
        self.btn_on = QPushButton("Manual ON")
        self.btn_off = QPushButton("Manual OFF")
        self.btn_auto = QPushButton("Resume AUTO")
        self.btn_on.clicked.connect(self._manual_on)
        self.btn_off.clicked.connect(self._manual_off)
        self.btn_auto.clicked.connect(self._resume_auto)
        btn_layout.addWidget(self.btn_on)
        btn_layout.addWidget(self.btn_off)
        btn_layout.addWidget(self.btn_auto)
        outer.addWidget(btn_box)

        self.fig = Figure(figsize=(6, 3))
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("time (s)")
        self.ax.set_ylabel("temp (C)")
        (self.line_body,) = self.ax.plot([], [], label="body")
        (self.line_amb,) = self.ax.plot([], [], label="ambient")
        self.ax.legend(loc="upper right")
        self.canvas = FigureCanvas(self.fig)
        outer.addWidget(self.canvas)

    # ---- manual control buttons --------------------------------------------

    def _manual_on(self) -> None:
        self.manual_on.set()
        self.manual_override.set()

    def _manual_off(self) -> None:
        self.manual_on.clear()
        self.manual_override.set()

    def _resume_auto(self) -> None:
        self.manual_override.clear()

    # ---- polling tick -------------------------------------------------------

    def _tick(self) -> None:
        if self.handle is None:
            self.lbl_mode.setText("starting...")
            return

        now = time.monotonic()
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
        if decision is not None:
            self.lbl_state.setText(decision.state.value)
            self.lbl_reason.setText(decision.reason)
        self.lbl_mode.setText("MANUAL OVERRIDE" if self.manual_override.is_set() else "AUTO")

        t = now - self._t0
        self._t_hist.append(t)
        self._body_hist.append(body.value if body is not None else float("nan"))
        self._amb_hist.append(amb.value if amb is not None else float("nan"))
        while self._t_hist and t - self._t_hist[0] > HISTORY_S:
            self._t_hist.popleft()
            self._body_hist.popleft()
            self._amb_hist.popleft()

        self.line_body.set_data(self._t_hist, self._body_hist)
        self.line_amb.set_data(self._t_hist, self._amb_hist)
        self.ax.relim()
        self.ax.autoscale_view()
        self.canvas.draw_idle()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self.handle is not None:
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
