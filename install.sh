#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ -z ${PYTHON_BIN:-} ]]; then
  for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,10))'; then
      PYTHON_BIN=$candidate; break
    fi
  done
fi
: "${PYTHON_BIN:?Instale Python 3.10+ (preferencialmente 3.12).}"
"$PYTHON_BIN" -c 'import sys; sys.exit(sys.version_info < (3,10))'
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
chmod +x pgddl
./pgddl --version
printf '\nInstalado localmente: %s/pgddl\n' "$PWD"
