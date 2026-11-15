#!/usr/bin/env bash
set -euo pipefail

output=${1:?Supply the output path for motion-detector.tar.gz}
python=${MOTION_PYTHON:-.venv/bin/python}
bundle=$("$python" -c 'from server.motion_adapter import ROOT, verify_frozen; verify_frozen(); print(ROOT)')
tar -C "$bundle" -czf "$output" \
  requirements.txt frozen-source-manifest.json source-validation-manifest.json \
  frozen_revised/{__init__,archive,baseline,calibration,coverage,proposals}.py
