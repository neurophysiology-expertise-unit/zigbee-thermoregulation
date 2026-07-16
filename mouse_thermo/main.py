"""Entry point.

  python -m mouse_thermo.main --config config.yaml
  python -m mouse_thermo.main --config config.yaml --simulate   # no hardware

FAIL-SAFE CONTRACT
  - lamp is commanded OFF at startup, before anything else
  - lamp is commanded OFF on any exception, signal, or normal exit
  - lamp is commanded OFF by the watchdog if the loop stalls
  - if the process is SIGKILLed or the host dies, NONE of the above run.
    That case is covered by hardware only. See README.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from typing import Optional

from .bus import SensorChannel
from .config import Config
from .controller import Controller, State
from .logger import SessionLogger
from .safety import SafetySupervisor
from .watchdog import Watchdog

log = logging.getLogger("mouse_thermo")


async def run(cfg: Config, max_seconds: Optional[float] = None) -> int:
    cfg.validate()

    body_ch = SensorChannel("body_temp", cfg.sensors.body_stale_after_s,
                            cfg.sensors.body_valid_range)
    amb_ch = SensorChannel("ambient_temp", cfg.sensors.ambient_stale_after_s,
                           cfg.sensors.ambient_valid_range)

    slog = SessionLogger(cfg.log_path, cfg.to_dict())
    safety = SafetySupervisor(cfg.safety)
    ctrl = Controller(cfg.control, safety, cfg.safety)

    sources = []
    app = None
    plug = None

    try:
        # ---- actuator + zigbee sensor ------------------------------------
        if cfg.simulate:
            from .actuators.dummy_plug import DummyPlug
            plug = DummyPlug()
            log.warning("SIMULATION MODE -- no hardware is being driven")
        else:
            from .zigbee.app import start_app, ZigbeePlug, ZigbeeSensorListener
            app = await start_app(cfg.zigbee)
            plug = ZigbeePlug(app, cfg.zigbee, asyncio.get_running_loop())
            await plug.bind_and_configure()
            if cfg.zigbee.sensor_ieee:
                listener = ZigbeeSensorListener(app, cfg.zigbee, amb_ch)
                await listener.configure()

        # ---- OFF before anything else ------------------------------------
        plug.set(False)
        slog.event("startup_lamp_off")

        # ---- optional sensor threads -------------------------------------
        if cfg.rfid.enabled:
            from .sensors.rfid_chip import RfidChipSource
            sources.append(RfidChipSource(cfg.rfid, body_ch))
        if cfg.esp32.enabled:
            from .sensors.esp32_serial import Esp32Source
            target = {"ambient": amb_ch, "body": body_ch}.get(cfg.esp32.role)
            if target is None:
                raise ValueError(f"esp32.role must be ambient|body, got {cfg.esp32.role}")
            sources.append(Esp32Source(cfg.esp32, target))
        for s in sources:
            s.start()

        # ---- watchdog -----------------------------------------------------
        def panic_off():
            try:
                plug.set(False)
                slog.event("watchdog_lamp_off")
            except Exception as e:
                slog.event("watchdog_lamp_off_FAILED", error=repr(e))
                raise

        wd = Watchdog(cfg.safety.watchdog_timeout_s, panic_off)
        wd.start()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # add_signal_handler is Unix-only (e.g. unsupported on Windows'
            # default event loop). A Ctrl-C there still raises KeyboardInterrupt
            # into this coroutine and is caught by the fail-safe handler below.
            log.warning("graceful signal handling unavailable on this platform")

        if max_seconds is not None:
            # In-process auto-stop, independent of OS signal support -- this is
            # what bench/dry-run testing on Windows relies on for a clean
            # shutdown (through this same try/finally), since an external kill
            # there would bypass the fail-safe lamp-off path entirely.
            loop.call_later(max_seconds, stop.set)
            log.info("auto-stop armed for %.1fs from now", max_seconds)

        log.info("control loop starting (period %.1fs)", cfg.control.loop_period_s)

        # ---- main loop ----------------------------------------------------
        while not stop.is_set():
            now = time.monotonic()
            body = body_ch.get(now)
            amb = amb_ch.get(now)

            if cfg.simulate:
                plug.tick(cfg.control.loop_period_s)
                amb_ch.push(plug.ambient)
                if cfg.rfid.enabled:
                    body_ch.push(plug.body)
                body, amb = body_ch.get(), amb_ch.get()

            decision = ctrl.step(body, amb, now)

            if decision.lamp_on != plug.state():
                plug.set(decision.lamp_on)

            wd.kick()
            slog.sample(
                body=None if body is None else body.value,
                body_age=body_ch.age(now),
                ambient=None if amb is None else amb.value,
                ambient_age=amb_ch.age(now),
                lamp_cmd=decision.lamp_on,
                lamp_state=plug.state(),
                power_w=plug.power_w(),
                state=decision.state.value,
                reason=decision.reason,
            )
            if decision.state is State.LOCKOUT:
                log.warning("LOCKOUT: %s", decision.reason)

            try:
                await asyncio.wait_for(stop.wait(), cfg.control.loop_period_s)
            except asyncio.TimeoutError:
                pass

        return 0

    except Exception as e:
        log.exception("fatal error -- shutting down cold")
        slog.event("fatal", error=repr(e))
        return 1

    finally:
        # Belt and braces. Every path lands here.
        for s in sources:
            s.stop()
        if plug is not None:
            try:
                plug.set(False)
                slog.event("shutdown_lamp_off")
            except Exception as e:
                log.critical("COULD NOT TURN LAMP OFF: %r -- CHECK THE RIG NOW", e)
                slog.event("shutdown_lamp_off_FAILED", error=repr(e))
            plug.close()
        if app is not None:
            await app.shutdown()
        slog.event("sensor_stats", body=body_ch.stats(), ambient=amb_ch.stats())
        slog.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--simulate", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--max-seconds", type=float, default=None,
                    help="Auto-stop after N seconds, still through the normal "
                         "fail-safe shutdown path (bench/dry-run testing)")
    a = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    cfg = Config.load(a.config, simulate=a.simulate)
    return asyncio.run(run(cfg, max_seconds=a.max_seconds))


if __name__ == "__main__":
    sys.exit(main())
