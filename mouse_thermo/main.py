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
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .bus import SensorChannel
from .config import Config
from .controller import Controller, Decision, State
from .logger import SessionLogger
from .safety import SafetySupervisor
from .watchdog import Watchdog

log = logging.getLogger("mouse_thermo")


class RecordingBox:
    """Thread-safe start/stop for a dedicated recording file, mirrored
    alongside the main session.jsonl. start()/stop() are called from an
    external thread (e.g. the GUI); mirror() is called every tick from the
    control loop's own thread -- the lock keeps logger+mode consistent
    across that boundary.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._logger: Optional[SessionLogger] = None
        self.mode: Optional[str] = None  # "closed_loop" | "open_loop"

    def start(self, path: str, mode: str, config_dump: dict) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        logger = SessionLogger(path, config_dump)
        with self._lock:
            self._logger = logger
            self.mode = mode

    def stop(self) -> None:
        with self._lock:
            logger, self._logger, self.mode = self._logger, None, None
        if logger is not None:
            logger.close()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._logger is not None

    def mirror(self, **kwargs) -> None:
        with self._lock:
            logger, mode = self._logger, self.mode
        if logger is not None:
            logger.sample(**kwargs, record_mode=mode)


@dataclass
class SessionHandle:
    """Live, thread-safe-to-read handle for external observers (e.g. a GUI)
    running on a different thread than this module's asyncio loop.

    body_ch/amb_ch (SensorChannel.get()) and plug (state()/power_w()) are
    already safe for cross-thread reads by their own design. last_decision is
    plain attribute replacement, not append -- also safe to read (if slightly
    stale by a few ms) without a lock.
    """
    loop: asyncio.AbstractEventLoop
    stop: asyncio.Event
    body_ch: SensorChannel
    amb_ch: SensorChannel
    plug: object
    cfg: Config
    recording: RecordingBox = field(default_factory=RecordingBox)
    last_decision: Optional[Decision] = None
    ambient_sensor: Optional[object] = None  # ZigbeeSensorListener, for last_seen_age(); None if unconfigured/simulated
    rfid_source: Optional[object] = None  # RfidChipSource, for last_raw_reading; None if rfid.enabled is False

    def request_shutdown(self) -> None:
        """Safe to call from any thread."""
        self.loop.call_soon_threadsafe(self.stop.set)


async def run(
    cfg: Config,
    max_seconds: Optional[float] = None,
    manual_override: Optional[threading.Event] = None,
    manual_on: Optional[threading.Event] = None,
    safety_bypass: Optional[threading.Event] = None,
    on_ready: Optional[Callable[[SessionHandle], None]] = None,
) -> int:
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
    ambient_listener = None

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

        # ---- OFF before anything else --------------------------------
        # Must happen the instant the plug is controllable, before any other
        # network setup (e.g. the ambient sensor bind below) that can hang
        # or fail and leave the lamp's real-world state undetermined in the
        # meantime.
        #
        # await set_async(), not set(): this coroutine runs ON the plug's own
        # event loop, and set() bridges to that same loop via
        # run_coroutine_threadsafe + a blocking wait -- calling it from here
        # deadlocks until the 10s timeout (found on real hardware: every
        # single plug command here timed out).
        await plug.set_async(False)
        slog.event("startup_lamp_off")

        if not cfg.simulate and cfg.zigbee.sensor_ieee:
            # Reuse the SAME listener instance across attempts -- it's
            # constructed once and registers itself with the zigpy device;
            # reconstructing on each retry would stack duplicate listeners.
            listener = ZigbeeSensorListener(app, cfg.zigbee, amb_ch)
            for attempt in range(1, 4):
                try:
                    await listener.configure()
                    ambient_listener = listener
                    break
                except Exception:
                    log.warning("ambient sensor bind attempt %d/3 failed "
                                "(sleepy battery end device -- retrying)",
                                attempt, exc_info=True)
                    if attempt < 3:
                        await asyncio.sleep(2.0)
            else:
                # The SNZB-02 is documented (CLAUDE.md) as a fallback/logging
                # input, not the primary safety sensor -- a bind hiccup on a
                # sleepy battery end device shouldn't take down the whole
                # run. Left unconfigured, amb_ch just never gets pushed to
                # and stays permanently stale, which the controller already
                # treats as "unknown -> unsafe" (invariant 2), i.e. the
                # correct fail-cold degradation, not a crash.
                log.error("ambient sensor bind failed after 3 attempts; "
                          "continuing without it")

        # ---- optional sensor threads -------------------------------------
        rfid_source = None
        if cfg.rfid.enabled:
            from .sensors.rfid_chip import RfidChipSource
            rfid_source = RfidChipSource(cfg.rfid, body_ch)
            sources.append(rfid_source)
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

        def _handle_async_exception(loop, context) -> None:
            # An exception raised inside an asyncio CALLBACK (e.g. a serial
            # transport's connection-lost cleanup) never reaches our own
            # try/except below -- asyncio's default handler just logs it to
            # the 'asyncio' logger and keeps running, which on a Windows
            # ProactorEventLoop can leave the loop limping along with a dead
            # underlying connection for the full watchdog_timeout_s before
            # anything reacts. React immediately instead: log loudly and
            # request a clean shutdown now, rather than waiting up to
            # watchdog_timeout_s for the watchdog to notice the loop stalled.
            exc = context.get("exception")
            log.critical(
                "UNHANDLED ASYNCIO CALLBACK EXCEPTION -- requesting immediate "
                "shutdown: %s", context.get("message"), exc_info=exc,
            )
            slog.event("asyncio_callback_exception",
                       message=context.get("message"), error=repr(exc))
            stop.set()

        loop.set_exception_handler(_handle_async_exception)

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

        handle = SessionHandle(loop=loop, stop=stop, body_ch=body_ch, amb_ch=amb_ch, plug=plug, cfg=cfg,
                                ambient_sensor=ambient_listener, rfid_source=rfid_source)
        if on_ready is not None:
            on_ready(handle)

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
            handle.last_decision = decision

            # Manual override substitutes the CONTROLLER's regulation choice
            # (NORMAL/FALLBACK hysteresis) with a directly-commanded state --
            # by default it never substitutes for a LATCHED lockout (real
            # hard-ceiling breach, stuck-on, or a latch not yet released).
            # It MAY substitute for a LOCKOUT that is only "both sensors
            # stale" (decision.latched is False there) -- that veto is
            # conservative-by-default, not evidence of active danger, and
            # operator choice allows overriding it for bench-testing the
            # relay with no sensors live.
            #
            # safety_bypass is the one deliberate exception: an explicit,
            # session-only (never persisted, never default-on) operator
            # acknowledgement that lets manual override through EVEN a
            # latched hard-ceiling lockout, for closely-supervised bench
            # testing. Every tick it's active is recorded (sample_kwargs
            # below) so it's never ambiguous in the data afterward whether
            # this was engaged.
            bypass_active = bool(safety_bypass and safety_bypass.is_set())
            if (
                manual_override is not None
                and manual_override.is_set()
                and (bypass_active or not (decision.state is State.LOCKOUT and decision.latched))
            ):
                desired = bool(manual_on and manual_on.is_set())
            else:
                desired = decision.lamp_on

            if desired != plug.state():
                await plug.set_async(desired)

            wd.kick()
            sample_kwargs = dict(
                body=None if body is None else body.value,
                body_age=body_ch.age(now),
                ambient=None if amb is None else amb.value,
                ambient_age=amb_ch.age(now),
                lamp_cmd=desired,
                controller_wanted=decision.lamp_on,
                lamp_state=plug.state(),
                power_w=plug.power_w(),
                state=decision.state.value,
                reason=decision.reason,
                manual_override=bool(manual_override and manual_override.is_set()),
                safety_bypass_active=bypass_active,
                body_setpoint_c=cfg.control.body_setpoint_c,
                ambient_setpoint_c=cfg.control.ambient_setpoint_c,
            )
            slog.sample(**sample_kwargs)
            handle.recording.mirror(**sample_kwargs)
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
                await plug.set_async(False)
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
