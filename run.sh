#!/usr/bin/env bash
# Start the rule-canary console. Everything it needs is standard library Python.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m canary.web --port "${PORT:-8000}" "$@"
