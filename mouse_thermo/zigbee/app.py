"""zigpy + bellows layer for the Sonoff ZBDongle-E (EFR32MG21 / EZSP).

NOTE ON DONGLE CHOICE: ZBDongle-E -> bellows (EZSP).
                       ZBDongle-P -> zigpy-znp (CC2652).
Some ZBDongle-E firmware revisions need software flow control (XON/XOFF);
if the app fails to start, that is the first thing to try.

This is the fiddliest layer in the project because it must be validated
against your actual paired devices. Pair once with pair.py, then the IEEE
addresses go in config.yaml and never change.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import zigpy.types as t
from zigpy.application import ControllerApplication
from zigpy.zcl.clusters.general import OnOff
from zigpy.zcl.clusters.measurement import TemperatureMeasurement

from ..actuators.base import Plug
from ..bus import SensorChannel
from ..config import ZigbeeConfig

log = logging.getLogger(__name__)


async def start_app(cfg: ZigbeeConfig) -> ControllerApplication:
    import bellows.zigbee.application

    app_cfg = {
        "device": {
            "path": cfg.device,
            "baudrate": cfg.baudrate,
            "flow_control": cfg.flow_control,
        },
        "database_path": cfg.database,
    }
    App = bellows.zigbee.application.ControllerApplication
    # App.new() -> __init__ already runs config through App.SCHEMA internally;
    # pre-validating here would double-validate and corrupt the OTA provider
    # list (dicts get turned into provider objects, then choke on a second pass).
    app = await App.new(app_cfg, auto_form=True, start_radio=True)
    log.info("zigbee network up on %s", cfg.device)
    return app


def _dev(app: ControllerApplication, ieee_str: str):
    ieee = t.EUI64.convert(ieee_str)
    dev = app.devices.get(ieee)
    if dev is None:
        raise RuntimeError(
            f"device {ieee_str} not in the zigpy database. Pair it first "
            f"(pair.py), and check it against `app.devices`."
        )
    return dev


class ZigbeePlug(Plug):
    """Sonoff S26/S40-class Zigbee smart plug via OnOff cluster.

    set() is async underneath; we expose a sync-ish API by scheduling onto the
    loop. Confirmed state comes from attribute reports, not from our command --
    a command that is sent is NOT a lamp that turned on.
    """

    def __init__(self, app: ControllerApplication, cfg: ZigbeeConfig,
                 loop: asyncio.AbstractEventLoop):
        self.app = app
        self.cfg = cfg
        self.loop = loop
        self.dev = _dev(app, cfg.plug_ieee)
        self.ep = self.dev.endpoints[cfg.plug_endpoint]
        self._confirmed: Optional[bool] = None
        self._power_w: Optional[float] = None

    async def bind_and_configure(self) -> None:
        onoff = self.ep.in_clusters[OnOff.cluster_id]
        await onoff.bind()
        await onoff.configure_reporting(
            OnOff.AttributeDefs.on_off.id, min_interval=0,
            max_interval=60, reportable_change=1,
        )
        # Optional: power metering, if this plug model has it (S31/S40 do not
        # all report power -- absence is fine, we just lose actuator feedback).
        try:
            from zigpy.zcl.clusters.homeautomation import ElectricalMeasurement
            em = self.ep.in_clusters.get(ElectricalMeasurement.cluster_id)
            if em is not None:
                await em.bind()
                await em.configure_reporting(
                    ElectricalMeasurement.AttributeDefs.active_power.id,
                    min_interval=5, max_interval=60, reportable_change=1,
                )
                log.info("power metering available on plug")
            else:
                log.info("plug has no ElectricalMeasurement cluster; "
                         "no actuator current feedback")
        except Exception:
            log.exception("power metering setup failed (non-fatal)")

        self.dev.add_listener(_PlugListener(self))

    async def set_async(self, on: bool) -> None:
        onoff = self.ep.in_clusters[OnOff.cluster_id]
        # Let exceptions propagate: a failed command must be visible.
        await (onoff.on() if on else onoff.off())

    def set(self, on: bool) -> None:
        fut = asyncio.run_coroutine_threadsafe(self.set_async(on), self.loop)
        fut.result(timeout=10)

    def state(self) -> Optional[bool]:
        return self._confirmed

    def power_w(self) -> Optional[float]:
        return self._power_w

    def close(self) -> None:
        # No redundant off-command here: main.py's finally block already
        # awaits set_async(False) and logs loudly if that fails. A second
        # attempt via the synchronous set() would deadlock for 10s if called
        # from the event loop's own thread (as main.py does) -- see set_async
        # docs on actuators.base.Plug.
        pass


class _PlugListener:
    def __init__(self, plug: ZigbeePlug):
        self.plug = plug

    def attribute_updated(self, cluster, attrid, value, timestamp=None):
        if cluster.cluster_id == OnOff.cluster_id and attrid == 0x0000:
            self.plug._confirmed = bool(value)
        elif cluster.cluster_id == 0x0B04 and attrid == 0x050B:  # active_power
            self.plug._power_w = float(value)  # often deci-watts; verify per model


class ZigbeeSensorListener:
    """Sonoff SNZB-02-class: TemperatureMeasurement, value in 0.01 C."""

    def __init__(self, app: ControllerApplication, cfg: ZigbeeConfig,
                 channel: SensorChannel):
        self.ch = channel
        self.dev = _dev(app, cfg.sensor_ieee)
        self.ep = self.dev.endpoints[cfg.sensor_endpoint]
        self.dev.add_listener(self)

    async def configure(self) -> None:
        cl = self.ep.in_clusters[TemperatureMeasurement.cluster_id]
        await cl.bind()
        # Battery device: it reports on its own schedule; aggressive intervals
        # are mostly ignored. THIS IS WHY IT CANNOT BE THE ONLY SAFETY LAYER.
        await cl.configure_reporting(
            TemperatureMeasurement.AttributeDefs.measured_value.id,
            min_interval=10, max_interval=300, reportable_change=20,  # 0.2 C
        )

    def attribute_updated(self, cluster, attrid, value, timestamp=None):
        if cluster.cluster_id == TemperatureMeasurement.cluster_id and attrid == 0x0000:
            self.ch.push(value / 100.0, meta={"src": "zigbee_snzb02"})
