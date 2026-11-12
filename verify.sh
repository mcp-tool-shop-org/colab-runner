#!/usr/bin/env bash
# One command for lint + tests + smoke. Exits non-zero on the first failure.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python}"

echo "== lint";  "$PY" -m ruff check --select F,E9,B .
echo "== tests"; "$PY" -W error -m unittest discover -s tests
echo "== smoke"; "$PY" -m colab_runner --version >/dev/null
echo "verify: ok"
