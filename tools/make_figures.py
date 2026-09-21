#!/usr/bin/env python3
"""
make_figures.py — figures for the zigbee-thermoregulation paper
================================================================
    python tools/make_figures.py      # from the repo root

Code lives here, in the code repo; the figures it writes go to the paper's
vault project (neubrain/projects/zigbee-thermoregulation/draft/figs/), where
the manuscript picks them up.

fig_cooling_pilot   DATA. MH002, 2026-09-21, session 2: brain temperature (chip on the skull)
                    under closed-loop Peltier pulses, with box ambient and the
                    actuator raster. Read from the raw recording in external/.
fig_schematic       ILLUSTRATION. The intended closed loop: head-fixed mouse,
                    Peltier on the head-bar holder, controller switches it OFF
                    once the setpoint is reached. Idealised, not data.

Recordings are parsed by the rig's own reader, mouse_thermo.recordings, so the
figure and the GUI's Review tab read the files identically. Needs matplotlib
(conda env neu-oldenlabs has it).
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse, Circle, FancyBboxPatch, FancyArrowPatch, Rectangle

# Full mount paths: $HOME differs between the servers that share this mount.
DATA = Path("/mnt/sysfs01/users/cagatay/external/zigbee-thermoregulation/260921_pilot")
OUT = Path("/mnt/sysfs01/users/cagatay/code/neubrain/projects/zigbee-thermoregulation/draft/figs")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # repo root, for mouse_thermo
from mouse_thermo import recordings  # noqa: E402

BLUE, GREY, ICE, RED = "#2471a3", "#7f8c8d", "#5dade2", "#c0392b"


def _save(fig, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(OUT / f"{name}.{ext}", dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {OUT / name}.{{png,pdf}}")


def fig_cooling_pilot() -> None:
    base = recordings.parse(str(DATA / "260921_MH002_1.jsonl"))
    rec = recordings.parse(str(DATA / "260921_MH002_2.jsonl"))
    t, raw, amb, on = rec["t"], rec["raw_body"], rec["ambient"], rec["lamp"]
    floor = rec["config"]["sensors"]["body_valid_range"][0]
    row = recordings.summarize(rec)

    fig, (ax, axa, axr) = plt.subplots(
        3, 1, figsize=(6.4, 5.2), sharex=True,
        gridspec_kw={"height_ratios": [3.2, 1.2, 0.5], "hspace": 0.12})

    # A. transponder temperature (raw: the gate discarded everything < floor)
    ax.step(t, raw, where="post", color=BLUE, linewidth=1.8,
            label="skull-mounted transponder (raw)")
    b0 = next(v for v in base["raw_body"] if v == v)
    ax.plot([-8], [b0], "o", color=GREY, markersize=5)
    ax.annotate(f"baseline\n{b0:.1f} °C", (-8, b0), xytext=(4, -26),
                textcoords="offset points", fontsize=7.5, color=GREY)
    ax.axhline(floor, color=RED, linewidth=0.8, linestyle="--")
    ax.text(t[-1], floor + 0.02, f"software plausibility floor {floor:.0f} °C — "
            "readings below were discarded", ha="right", va="bottom",
            fontsize=6.8, color=RED)
    first, last = row["raw_body_first_c"], row["raw_body_last_c"]
    ax.annotate(f"{first:.1f} → {last:.1f} °C  (Δ {last - first:+.1f} °C in "
                f"{row['duration_s'] / 60:.1f} min)",
                xy=(0.98, 0.30), xycoords="axes fraction", ha="right",
                fontsize=8.5, color=BLUE)
    ax.set_ylabel("brain temperature (°C)")
    ax.set_ylim(29.35, 30.35)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(loc="lower left", fontsize=7.5, frameon=False)

    # B. box ambient: flat -- the cooling was local (head-bar), not the box
    axa.plot(t, amb, color=GREY, linewidth=1.2)
    axa.set_ylabel("ambient\n(°C)", fontsize=8)
    axa.set_ylim(23.5, 24.5)
    axa.spines[["top", "right"]].set_visible(False)

    # C. Peltier command raster
    for i in range(len(on) - 1):
        if on[i]:
            axr.axvspan(t[i], t[i + 1], color=ICE, linewidth=0)
    axr.set_yticks([])
    axr.set_ylabel("Peltier", fontsize=8, rotation=0, ha="right", va="center")
    axr.text(1.0, 1.15, f"ON {row['actuator_duty_pct']:.0f}% of the time (pulsed)",
             transform=axr.transAxes, ha="right", fontsize=7, color=ICE)
    axr.spines[["top", "right", "left"]].set_visible(False)
    axr.set_xlabel("time (s)")
    axr.set_xlim(-14, t[-1] + 2)

    fig.suptitle("Head-bar Peltier cooling, closed loop — MH002, pilot",
                 fontsize=10, x=0.12, ha="left")
    _save(fig, "fig_cooling_pilot")


def _mouse(ax, x, y):
    """Side-view mouse, facing left. Kept simple on purpose."""
    body = Ellipse((x + 1.55, y), 3.1, 1.45, color="#bfc9ca", zorder=3)
    head = Ellipse((x - 0.05, y + 0.35), 1.45, 1.0, angle=-12, color="#bfc9ca", zorder=4)
    snout = Ellipse((x - 0.72, y + 0.15), 0.55, 0.38, angle=-15, color="#bfc9ca", zorder=4)
    ear = Ellipse((x + 0.25, y + 0.92), 0.5, 0.62, angle=20, color="#aab7b8", zorder=5)
    eye = Circle((x - 0.35, y + 0.45), 0.07, color="#1b2631", zorder=6)
    nose = Circle((x - 0.98, y + 0.12), 0.06, color="#e6b0aa", zorder=6)
    for p in (body, head, snout, ear, eye, nose):
        ax.add_patch(p)
    tail_x = [x + 3.05, x + 3.7, x + 4.3, x + 4.8]
    tail_y = [y - 0.1, y - 0.35, y - 0.25, y - 0.55]
    ax.plot(tail_x, tail_y, color="#aab7b8", linewidth=2.2, solid_capstyle="round", zorder=2)
    for lx in (x + 0.55, x + 2.4):
        ax.plot([lx, lx - 0.1], [y - 0.62, y - 1.15], color="#aab7b8", linewidth=2.4,
                solid_capstyle="round", zorder=2)


def _box(ax, x, y, w, h, text, fc, fontsize=8):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.06,rounding_size=0.12",
                                fc=fc, ec="#2c3e50", linewidth=0.9, zorder=5))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fontsize, zorder=6)


def _arrow(ax, a, b, text=None, color="#2c3e50", rad=0.0, offset=(0, 0.18)):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle="-|>", mutation_scale=11, linewidth=1.2,
                                 color=color, connectionstyle=f"arc3,rad={rad}", zorder=7))
    if text:
        ax.text((a[0] + b[0]) / 2 + offset[0], (a[1] + b[1]) / 2 + offset[1], text,
                ha="center", fontsize=7, color=color, zorder=8)


def fig_schematic() -> None:
    fig = plt.figure(figsize=(9.0, 4.6))
    ax = fig.add_axes([0.0, 0.0, 0.66, 1.0])
    ax.set_xlim(-1.6, 10.4)
    ax.set_ylim(-2.3, 5.0)
    ax.set_aspect("equal")
    ax.axis("off")

    # rig: floor post, head-bar holder, Peltier on it, running wheel/platform
    mx, my = 0.6, 0.0
    _mouse(ax, mx, my)
    ax.add_patch(Rectangle((-1.2, -1.35), 6.6, 0.18, color="#d5d8dc", zorder=1))   # platform
    ax.add_patch(Rectangle((-1.05, -1.17), 0.22, 3.55, color="#839192", zorder=1))  # post
    ax.add_patch(Rectangle((-1.05, 2.2), 1.95, 0.22, color="#839192", zorder=2))    # holder arm
    ax.add_patch(Rectangle((0.28, 0.95), 0.62, 1.28, color="#839192", zorder=2))     # clamp
    ax.add_patch(Rectangle((0.1, 0.9), 1.0, 0.14, color="#566573", zorder=6))       # head bar
    # Peltier element sandwiched in the holder, cold face towards the head bar
    ax.add_patch(Rectangle((0.22, 1.55), 0.78, 0.34, fc=ICE, ec="#1b4f72", linewidth=1.0, zorder=7))
    ax.text(1.18, 1.72, "Peltier\n(cold side → head bar)", fontsize=7, va="center", zorder=8)
    for i in range(3):   # heat carried away conductively from the head
        ax.annotate("", xy=(0.61, 1.52 - 0.0), xytext=(0.61, 1.05),
                    arrowprops=dict(arrowstyle="-|>", color=ICE, lw=1.0))
    ax.text(-0.95, 2.6, "head-fixed", fontsize=8, style="italic", color="#566573")

    # temperature transponder on the skull, over the brain, behind the head bar
    chip = (mx + 0.62, my + 0.80)
    ax.add_patch(Ellipse(chip, 0.40, 0.15, angle=-12, color="#f5b041", zorder=9))
    ax.annotate("temperature transponder\non the skull (brain)", xy=chip,
                xytext=(mx + 2.6, my + 1.9), fontsize=7, ha="center",
                arrowprops=dict(arrowstyle="-", color="#b9770e", lw=0.8))
    _box(ax, 5.3, -1.1, 1.55, 0.7, "RFID reader\n(URH-2)", "#fdebd0", 7)
    ax.plot([chip[0] + 0.2, 5.3], [chip[1], -0.75], color="#b9770e", linewidth=0.8,
            linestyle=(0, (2, 2)), zorder=4)

    # controller and actuator path
    _box(ax, 7.5, 1.5, 2.3, 1.25, "closed-loop\ncontroller\n(mouse_thermo)", "#d6eaf8", 8)
    _box(ax, 7.5, 3.55, 2.3, 0.75, "Zigbee smart plug", "#e8daef", 8)
    _box(ax, 4.2, 3.55, 1.9, 0.75, "USB hub", "#eaeded", 8)
    _arrow(ax, (6.1, -0.45), (8.35, 1.45), "brain T measured", rad=0.18, offset=(0.55, -0.1))
    _arrow(ax, (8.65, 2.8), (8.65, 3.5), None)
    ax.text(8.8, 3.1, "ON / OFF", fontsize=7, va="center")
    _arrow(ax, (7.45, 3.92), (6.15, 3.92), "power")
    _arrow(ax, (4.15, 3.92), (0.62, 1.95), "Peltier supply", rad=0.25, offset=(-0.6, 0.35))

    # right: the decision rule and an idealised trace
    ax2 = fig.add_axes([0.69, 0.56, 0.29, 0.34])
    import numpy as np
    tt = np.linspace(0, 10, 400)
    sp, t0, tau = 32.0, 36.0, 2.2
    reach = -tau * np.log((sp - 30.0) / (t0 - 30.0))     # first-order approach to 30 °C
    temp = np.where(tt < reach, 30.0 + (t0 - 30.0) * np.exp(-tt / tau), sp)
    temp = temp + np.where(tt >= reach, 0.12 * np.sin((tt - reach) * 2.2), 0.0)
    ax2.plot(tt, temp, color=BLUE, linewidth=1.6)
    ax2.axhline(sp, color=RED, linestyle="--", linewidth=0.8)
    ax2.text(10, sp + 0.25, "setpoint", ha="right", fontsize=7, color=RED)
    ax2.axvspan(0, reach, color=ICE, alpha=0.35, linewidth=0)
    ax2.text(reach / 2, 35.4, "Peltier ON", ha="center", fontsize=7, color="#1b4f72")
    ax2.text(reach + (10 - reach) / 2, 35.4, "OFF, holds\nwithin deadband", ha="center",
             fontsize=7, va="top", color="#566573")
    ax2.set_xticks([])
    ax2.set_yticks([sp, t0])
    ax2.set_yticklabels([f"{sp:.0f} °C", f"{t0:.0f} °C"], fontsize=7)
    ax2.set_xlabel("time", fontsize=7)
    ax2.set_title("idealised — not data", fontsize=7, color=GREY, loc="left")
    ax2.spines[["top", "right"]].set_visible(False)

    fig.text(0.69, 0.33, "Control rule (cool mode)", fontsize=8.5, weight="bold")
    fig.text(0.69, 0.10,
             "T > setpoint + deadband  →  Peltier ON\n"
             "T < setpoint − deadband  →  Peltier OFF\n"
             "in between               →  hold last command\n"
             "T ≤ safety floor / stale →  OFF (lockout)\n"
             "   (safety can only veto, never switch ON)",
             fontsize=7.6, family="monospace", linespacing=1.6)
    _save(fig, "fig_schematic")


if __name__ == "__main__":
    fig_cooling_pilot()
    fig_schematic()
