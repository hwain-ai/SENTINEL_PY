#!/usr/bin/env -S -i SENTINEL_PY_SEALED_ENTRY=direct-v1 PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 /usr/bin/bash --noprofile --norc
set -euo pipefail
umask 077

fail() {
  /usr/bin/printf 'toolchain error: %s\n' "$1" >&2
  exit 2
}

[[ "${SENTINEL_PY_SEALED_ENTRY:-}" == "direct-v1" ]] ||
  fail "launcher must be executed directly"
/usr/bin/grep -zFqx -- '--noprofile' "/proc/$$/cmdline" ||
  fail "launcher must be executed directly"
/usr/bin/grep -zFqx -- '--norc' "/proc/$$/cmdline" ||
  fail "launcher must be executed directly"
unset SENTINEL_PY_SEALED_ENTRY

readonly script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly repository_root="$(cd -- "$script_directory/.." && pwd -P)"
readonly lock_file="$repository_root/toolchain.lock.json"
values="$(/usr/bin/python3 -I "$script_directory/toolchain_lock.py" \
  "$lock_file" python --require-locked)"
mapfile -t fields <<<"$values"
readonly python_home="$repository_root/.toolchain/${fields[5]}"
readonly python_binary="$python_home/${fields[6]}"
readonly synthetic_home="$repository_root/.toolchain/home"

[[ -d "$python_home" && ! -L "$python_home" && -d "$synthetic_home" && ! -L "$synthetic_home" ]] ||
  fail "verified local Python tree is missing"
/usr/bin/python3 -I "$script_directory/toolchain_lock.py" \
  "$lock_file" python --require-locked --verify-tree "$python_home"
[[ -f "$python_binary" && ! -L "$python_binary" && -x "$python_binary" ]] ||
  fail "verified local Python executable is missing"
[[ "$(/usr/bin/sha256sum -- "$python_binary" | /usr/bin/cut -d ' ' -f 1)" == "${fields[7]}" ]] ||
  fail "Python executable checksum mismatch"

exec /usr/bin/env -i \
  HOME="$synthetic_home" \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PATH="$python_home/bin:/usr/bin:/bin" \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONSAFEPATH=1 \
  "$python_binary" -I -B "$@"
