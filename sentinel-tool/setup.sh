#!/bin/sh
# First-run preparation: locked Python and uv, dependencies from uv.lock, doctor. See scripts/toolchain.py.
exec "${SENTINEL_PYTHON:-python3}" -I -B "$(dirname "$0")/../scripts/toolchain.py" setup
