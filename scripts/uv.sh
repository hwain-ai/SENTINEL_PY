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
python_values="$(/usr/bin/python3 -I "$script_directory/toolchain_lock.py" \
  "$lock_file" python --require-locked)"
uv_values="$(/usr/bin/python3 -I "$script_directory/toolchain_lock.py" \
  "$lock_file" uv --require-locked)"
mapfile -t python_fields <<<"$python_values"
mapfile -t uv_fields <<<"$uv_values"
readonly python_home="$repository_root/.toolchain/${python_fields[5]}"
readonly python_binary="$python_home/${python_fields[6]}"
readonly uv_home="$repository_root/.toolchain/${uv_fields[5]}"
readonly uv_binary="$uv_home/${uv_fields[6]}"
readonly synthetic_home="$repository_root/.toolchain/home"
readonly cache_directory="$repository_root/.toolchain/cache"
readonly environment_directory="$repository_root/.toolchain/venv"

for directory in "$python_home" "$uv_home" "$synthetic_home" "$cache_directory"; do
  [[ -d "$directory" && ! -L "$directory" ]] ||
    fail "verified local Python and uv trees are missing"
done
/usr/bin/python3 -I "$script_directory/toolchain_lock.py" \
  "$lock_file" python --require-locked --verify-tree "$python_home"
/usr/bin/python3 -I "$script_directory/toolchain_lock.py" \
  "$lock_file" uv --require-locked --verify-tree "$uv_home"
[[ -f "$python_binary" && ! -L "$python_binary" && -x "$python_binary" ]] ||
  fail "verified local Python executable is missing"
[[ -f "$uv_binary" && ! -L "$uv_binary" && -x "$uv_binary" ]] ||
  fail "verified local uv executable is missing"
[[ "$(/usr/bin/sha256sum -- "$python_binary" | /usr/bin/cut -d ' ' -f 1)" == "${python_fields[7]}" ]] ||
  fail "Python executable checksum mismatch"
[[ "$(/usr/bin/sha256sum -- "$uv_binary" | /usr/bin/cut -d ' ' -f 1)" == "${uv_fields[7]}" ]] ||
  fail "uv executable checksum mismatch"

[[ "$#" -ge 1 ]] || fail "usage: uv.sh {run|sync|lock|lock-check|deps|--version} ..."
readonly mode="$1"
shift
common_environment=(
  HOME="$synthetic_home"
  LANG=C.UTF-8
  LC_ALL=C.UTF-8
  PATH="$python_home/bin:$uv_home:/usr/bin:/bin"
  PYTHONDONTWRITEBYTECODE=1
  PYTHONSAFEPATH=1
  UV_CACHE_DIR="$cache_directory"
  UV_KEYRING_PROVIDER=disabled
  UV_NO_CONFIG=1
  UV_PYTHON_DOWNLOADS=never
  UV_PROJECT_ENVIRONMENT="$environment_directory"
)

case "$mode" in
  run)
    exec /usr/bin/env -i "${common_environment[@]}" "$uv_binary" --no-config \
      run --project "$repository_root" --locked --offline --no-sync \
      --python "$python_binary" "$@"
    ;;
  sync)
    offline_argument=()
    if [[ "${1:-}" == "--offline" ]]; then
      offline_argument=(--offline)
      shift
    fi
    [[ "$#" -eq 0 ]] || fail "usage: uv.sh sync [--offline]"
    exec /usr/bin/env -i "${common_environment[@]}" "$uv_binary" --no-config \
      sync --project "$repository_root" --locked --python "$python_binary" \
      "${offline_argument[@]}"
    ;;
  lock)
    [[ "$#" -eq 0 ]] || fail "usage: uv.sh lock"
    exec /usr/bin/env -i "${common_environment[@]}" "$uv_binary" --no-config \
      lock --project "$repository_root" --python "$python_binary"
    ;;
  lock-check)
    [[ "$#" -eq 0 ]] || fail "usage: uv.sh lock-check"
    exec /usr/bin/env -i "${common_environment[@]}" "$uv_binary" --no-config \
      lock --project "$repository_root" --check --offline \
      --python "$python_binary"
    ;;
  deps)
    # deps TARGET REQUIREMENTS [--offline]: install a checked project's own test
    # requirements (wheels only) into TARGET for the pinned Python. TARGET is put on
    # PYTHONPATH by the checker; it is never analyzed or mutated.
    [[ "$#" -eq 2 || ( "$#" -eq 3 && "$3" == "--offline" ) ]] || fail "usage: uv.sh deps TARGET REQUIREMENTS [--offline]"
    offline_argument=()
    [[ "$#" -eq 3 ]] && offline_argument=(--offline)
    exec /usr/bin/env -i "${common_environment[@]}" "$uv_binary" --no-config \
      pip install --python "$python_binary" --target "$1" --requirement "$2" \
      --only-binary :all: --link-mode=copy --reinstall "${offline_argument[@]}"
    ;;
  --version)
    [[ "$#" -eq 0 ]] || fail "usage: uv.sh --version"
    exec /usr/bin/env -i "${common_environment[@]}" "$uv_binary" --version
    ;;
  *)
    fail "usage: uv.sh {run|sync|lock|lock-check|deps|--version} ..."
    ;;
esac
