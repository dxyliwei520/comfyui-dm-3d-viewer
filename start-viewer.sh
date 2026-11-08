#!/usr/bin/env bash
# Portable launcher (macOS / Linux). Needs python3 (3.9+). three.js is bundled.
set -e
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
exec python3 server.py "$@"
