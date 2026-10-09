#!/usr/bin/env bash
# Full quality gate: lint, format check, strict types, unit + CLI tests, and GUI end-to-end tests under Xvfb.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin
$PY/ruff check chatbridge tests scripts
$PY/ruff format --check chatbridge tests scripts
$PY/mypy
$PY/pytest -W ignore --deselect tests/test_gui_e2e.py
xvfb-run -a $PY/pytest -W ignore tests/test_gui_e2e.py
rm -f dist/chatbridge_*_all.deb
packaging/deb/build-deb.sh dist >/dev/null
dpkg-deb --info dist/chatbridge_*_all.deb >/dev/null
echo "ALL CHECKS PASSED"
