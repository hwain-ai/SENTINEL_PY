#!/usr/bin/bash
# First-run preparation for the Python checker: pinned Python and uv, locked
# dependencies, then a doctor pass. Safe to rerun; each step verifies before it
# downloads anything.
set -euo pipefail
root="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
"$root/scripts/bootstrap-python.sh"
if ! "$root/scripts/uv.sh" sync --offline; then
  printf 'sentinel-tool: locked dependencies are not cached, syncing from the lock file online\n' >&2
  "$root/scripts/uv.sh" sync
fi
"$root/scripts/uv.sh" run sentinel-py doctor --project "$root" --format json >/dev/null
printf 'sentinel-tool: python checker ready\n' >&2
