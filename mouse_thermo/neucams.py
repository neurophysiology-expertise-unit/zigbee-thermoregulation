"""Fire-and-forget UDP trigger for the neucams camera software.

When a recording starts here we tell neucams the run name and to start
acquiring, so the cameras capture in lock-step with the temperature log; on
stop we tell it to stop. neucams listens for plain-text UDP commands (see its
`view/widgets.py::_process_server_messages`): ``folder=<name>`` sets the run
name for every camera, ``start`` begins acquisition, ``stop`` ends it. The
neucams config must have ``udp_enable: true`` and a matching ``server_port``.

This is a one-way, best-effort notifier: it is NOT on the safety path and must
never block or crash the control session. A failed send is logged loudly (per
the repo's "never suppress errors" rule) but does not stop the temperature
recording -- losing camera sync must not cost you the experiment's data.
"""
from __future__ import annotations

import logging
import socket

from .config import NeucamsConfig

log = logging.getLogger(__name__)


class NeucamsClient:
    def __init__(self, cfg: NeucamsConfig):
        self.cfg = cfg
        # Runtime on/off, toggled by the GUI checkbox. Initialised from config
        # but independent of it, so the operator can enable the trigger at
        # runtime even when the config default is off (and vice versa).
        self.enabled = bool(cfg.enabled)
        self._sock: socket.socket | None = None
        if self.enabled:
            # UDP is connectionless; the socket just holds the send buffer.
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def set_enabled(self, on: bool) -> None:
        """Enable/disable the trigger at runtime (the GUI 'Trigger neucams' box)."""
        on = bool(on)
        if on and self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.enabled = on
        log.info("neucams trigger %s (%s:%d)", "ENABLED" if on else "disabled",
                 self.cfg.host, self.cfg.port)

    def _send(self, msg: str) -> bool:
        if not self.enabled or self._sock is None:
            return False
        try:
            self._sock.sendto(msg.encode("utf-8"), (self.cfg.host, self.cfg.port))
            log.info("neucams <- %r  (%s:%d)", msg, self.cfg.host, self.cfg.port)
            return True
        except OSError as e:
            # Loud, but non-fatal: the temperature session carries on.
            log.warning("neucams send %r failed (%s:%d): %s",
                        msg, self.cfg.host, self.cfg.port, e)
            return False

    def set_run_name(self, name: str) -> None:
        self._send(f"folder={name}")

    def start(self) -> None:
        self._send("start")

    def stop(self) -> None:
        self._send("stop")

    def begin_recording(self, run_name: str) -> None:
        """Set the run name first (so cameras save under it), then start."""
        self.set_run_name(run_name)
        self.start()

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
