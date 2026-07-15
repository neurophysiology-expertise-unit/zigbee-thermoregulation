#!/usr/bin/env bash
# One-shot bootstrap. Run from the repo root.
set -euo pipefail

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r mouse_thermo/requirements.txt

echo
echo "Running safety tests..."
pytest mouse_thermo/test_safety.py -q

echo
echo "Running simulation smoke test (10s, no hardware)..."
timeout 10 python -m mouse_thermo.main --config mouse_thermo/config.yaml --simulate || true

echo
echo "Setup complete."
echo "Next: python -m mouse_thermo.pair --config mouse_thermo/config.yaml --seconds 120"
