# AGENTS — entry point

**Read `CLAUDE.md` FIRST.** It is this repo's durable rules *and* its current
state: architecture, the numbered invariants, the GUI mode model, heat/cool
mode, the ESP32 frame format, known limitations, and the open-work list. It is
plain markdown — read it directly, whatever agent you are. Codex and Gemini do
not auto-read `CLAUDE.md`, so this file exists to point you at it.

**A live animal sits under the actuator this code controls.** Bugs here can
cook a mouse, or freeze one in cool mode. Before changing `safety.py`,
`controller.py` or `bus.py`, read CLAUDE.md's "Invariants — do not violate
without discussion" and run, from the repo root:

    pytest mouse_thermo/test_safety.py -q
    python -m mouse_thermo.main --config mouse_thermo/config.yaml --simulate

The test suite is the specification. If you change control behaviour, add a
test that pins the new behaviour.

**Do not "fix" `config.local.yaml` to match `config.yaml`.** They are different
protocols on purpose (normal thermoregulation vs hyperthermia vs brain
cooling); CLAUDE.md explains each number.

**The paper this rig feeds** is `neubrain/projects/zigbee-thermoregulation/`
(vault-side: plan, literature, `draft/manuscript.md`). This repo is recorded
there as that project's `code_repo`. Results and manuscript text live in the
vault, never here; hardware/control code lives here, never in the vault.
