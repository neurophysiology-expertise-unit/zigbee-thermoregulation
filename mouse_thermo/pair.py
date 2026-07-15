"""One-off pairing helper. Run, press the pair button, note the IEEE addresses.

    python pair.py --config config.yaml --seconds 120
"""
import argparse, asyncio, logging
from mouse_thermo.config import Config
from mouse_thermo.zigbee.app import start_app


async def main(cfg, seconds):
    app = await start_app(cfg.zigbee)
    print(f"permit_join for {seconds}s -- press the button on each device now")
    await app.permit(seconds)
    await asyncio.sleep(seconds)
    print("\n--- paired devices ---")
    for ieee, dev in app.devices.items():
        print(f"{ieee}  {dev.manufacturer} {dev.model}")
        for eid, ep in dev.endpoints.items():
            if eid == 0:
                continue
            print(f"   ep{eid}: in={[hex(c) for c in ep.in_clusters]}")
    await app.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--seconds", type=int, default=120)
    a = p.parse_args()
    asyncio.run(main(Config.load(a.config, require_plug_ieee=False), a.seconds))
