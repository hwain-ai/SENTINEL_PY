#!/bin/sh
# Thin wrapper: the cross-platform launcher lives in scripts/toolchain.py (Linux and macOS).
exec "${SENTINEL_PYTHON:-python3}" -I -B "$(dirname "$0")/toolchain.py" "$@"
