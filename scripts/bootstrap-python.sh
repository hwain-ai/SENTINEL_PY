#!/usr/bin/env -S -i SENTINEL_PY_SEALED_ENTRY=direct-v1 PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 /usr/bin/bash --noprofile --norc
set -euo pipefail
umask 077

fail() {
  /usr/bin/printf 'toolchain error: %s\n' "$1" >&2
  exit 2
}

require_direct_entry() {
  [[ "${SENTINEL_PY_SEALED_ENTRY:-}" == "direct-v1" ]] ||
    fail "launcher must be executed directly"
  /usr/bin/grep -zFqx -- '--noprofile' "/proc/$$/cmdline" ||
    fail "launcher must be executed directly"
  /usr/bin/grep -zFqx -- '--norc' "/proc/$$/cmdline" ||
    fail "launcher must be executed directly"
  unset SENTINEL_PY_SEALED_ENTRY
}

require_direct_entry

readonly script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly repository_root="$(cd -- "$script_directory/.." && pwd -P)"
readonly lock_file="$repository_root/toolchain.lock.json"
readonly toolchain_root="$repository_root/.toolchain"
readonly download_root="$toolchain_root/downloads"

offline=false
if [[ "$#" -gt 1 ]]; then
  fail "usage: bootstrap-python.sh [--offline]"
fi
if [[ "$#" -eq 1 ]]; then
  [[ "$1" == "--offline" ]] || fail "usage: bootstrap-python.sh [--offline]"
  offline=true
fi

read_lock() {
  /usr/bin/python3 -I "$script_directory/toolchain_lock.py" \
    "$lock_file" "$1" --require-locked
}

python_values="$(read_lock python)"
uv_values="$(read_lock uv)"
mapfile -t python_fields <<<"$python_values"
mapfile -t uv_fields <<<"$uv_values"

[[ "$(/usr/bin/uname -s)" == "Linux" ]] || fail "only Linux is supported"
[[ "$(/usr/bin/uname -m)" == "x86_64" ]] || fail "only Linux x86_64 is supported"

archive_path() {
  local -n selected_fields="$1"
  /usr/bin/printf '%s/%s.tar.gz\n' "$download_root" "${selected_fields[3]}"
}

destination_path() {
  local -n selected_fields="$1"
  /usr/bin/printf '%s/%s\n' "$toolchain_root" "${selected_fields[5]}"
}

verify_archive() {
  local archive="$1"
  local expected_size="$2"
  local expected_sha256="$3"
  [[ -f "$archive" && ! -L "$archive" ]] || return 1
  [[ "$(/usr/bin/stat -c '%s' -- "$archive")" == "$expected_size" ]] || return 1
  [[ "$(/usr/bin/sha256sum -- "$archive" | /usr/bin/cut -d ' ' -f 1)" == "$expected_sha256" ]]
}

require_offline_archives() {
  local -n selected_fields="$1"
  local destination
  local archive
  destination="$(destination_path "$1")"
  archive="$(archive_path "$1")"
  if [[ ! -d "$destination" || -L "$destination" ]]; then
    verify_archive "$archive" "${selected_fields[2]}" "${selected_fields[3]}" ||
      fail "offline archive is missing or invalid for ${selected_fields[0]}"
  fi
}

if [[ "$offline" == true ]]; then
  require_offline_archives python_fields
  require_offline_archives uv_fields
fi

if [[ -e "$toolchain_root" || -L "$toolchain_root" ]]; then
  [[ -d "$toolchain_root" && ! -L "$toolchain_root" ]] ||
    fail ".toolchain must be a repository-local directory"
else
  /usr/bin/mkdir -- "$toolchain_root"
fi
/usr/bin/mkdir -p -- "$download_root"

download_archive() {
  local -n selected_fields="$1"
  local archive
  archive="$(archive_path "$1")"
  if verify_archive "$archive" "${selected_fields[2]}" "${selected_fields[3]}"; then
    return
  fi
  [[ "$offline" == false ]] ||
    fail "offline archive is missing or invalid for ${selected_fields[0]}"
  local partial="$archive.part"
  [[ ! -e "$partial" && ! -L "$partial" ]] ||
    fail "partial download already exists: $partial"
  /usr/bin/curl --fail --location --proto '=https' --proto-redir '=https' \
    --tlsv1.2 --output "$partial" "${selected_fields[1]}"
  verify_archive "$partial" "${selected_fields[2]}" "${selected_fields[3]}" ||
    fail "downloaded archive verification failed for ${selected_fields[0]}"
  /usr/bin/mv -- "$partial" "$archive"
}

verify_installed() {
  local tool="$1"
  local -n selected_fields="$2"
  local destination
  destination="$(destination_path "$2")"
  /usr/bin/python3 -I "$script_directory/toolchain_lock.py" \
    "$lock_file" "$tool" --require-locked --verify-tree "$destination"
  local binary="$destination/${selected_fields[6]}"
  [[ -f "$binary" && ! -L "$binary" && -x "$binary" ]] ||
    fail "verified ${tool} executable is missing"
  [[ "$(/usr/bin/sha256sum -- "$binary" | /usr/bin/cut -d ' ' -f 1)" == "${selected_fields[7]}" ]] ||
    fail "${tool} executable checksum mismatch"
}

staged_python=""
staged_uv=""
cleanup_staging() {
  if [[ -n "$staged_python" && -d "$staged_python" && ! -L "$staged_python" ]]; then
    /usr/bin/rm -rf -- "$staged_python"
  fi
  if [[ -n "$staged_uv" && -d "$staged_uv" && ! -L "$staged_uv" ]]; then
    /usr/bin/rm -rf -- "$staged_uv"
  fi
}
trap cleanup_staging EXIT INT TERM

prepare_install() {
  local tool="$1"
  local fields_name="$2"
  local output_name="$3"
  local -n selected_fields="$fields_name"
  local -n output="$output_name"
  local destination
  destination="$(destination_path "$fields_name")"
  if [[ -d "$destination" && ! -L "$destination" ]]; then
    verify_installed "$tool" "$fields_name"
    return
  fi
  [[ ! -e "$destination" && ! -L "$destination" ]] ||
    fail "${tool} destination is not a regular directory"
  download_archive "$fields_name"
  output="$(/usr/bin/mktemp -d "$toolchain_root/.staging-${tool}.XXXXXXXX")"
  /usr/bin/tar --extract --gzip \
    --file "$(archive_path "$fields_name")" \
    --directory "$output" \
    --no-same-owner --no-same-permissions
  local extracted="$output/${selected_fields[4]}"
  [[ -d "$extracted" && ! -L "$extracted" ]] ||
    fail "archive root is missing for ${tool}"
  /usr/bin/python3 -I "$script_directory/toolchain_lock.py" \
    "$lock_file" "$tool" --require-locked --verify-tree "$extracted"
  local binary="$extracted/${selected_fields[6]}"
  [[ -f "$binary" && ! -L "$binary" && -x "$binary" ]] ||
    fail "archive executable is missing for ${tool}"
  [[ "$(/usr/bin/sha256sum -- "$binary" | /usr/bin/cut -d ' ' -f 1)" == "${selected_fields[7]}" ]] ||
    fail "archive executable checksum mismatch for ${tool}"
  local actual_version
  actual_version="$(/usr/bin/env -i HOME="$toolchain_root" LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PATH="$(dirname -- "$binary"):/usr/bin:/bin" "$binary" --version 2>&1)"
  [[ "$actual_version" == "${selected_fields[8]}" ]] ||
    fail "archive version mismatch for ${tool}: $actual_version"
}

prepare_install python python_fields staged_python
prepare_install uv uv_fields staged_uv

publish_install() {
  local tool="$1"
  local fields_name="$2"
  local staging_name="$3"
  local -n selected_fields="$fields_name"
  local -n staging="$staging_name"
  [[ -n "$staging" ]] || return 0
  local source="$staging/${selected_fields[4]}"
  local destination
  destination="$(destination_path "$fields_name")"
  [[ ! -e "$destination" && ! -L "$destination" ]] ||
    fail "${tool} destination appeared during bootstrap"
  /usr/bin/mv -- "$source" "$destination"
  /usr/bin/rmdir -- "$staging"
  staging=""
  verify_installed "$tool" "$fields_name"
}

publish_install python python_fields staged_python
publish_install uv uv_fields staged_uv
/usr/bin/mkdir -p -- "$toolchain_root/home" "$toolchain_root/cache" \
  "$toolchain_root/venv" "$toolchain_root/pytest-cache"
/usr/bin/chmod 700 -- "$toolchain_root" "$toolchain_root/home" \
  "$toolchain_root/cache" "$toolchain_root/venv" "$toolchain_root/pytest-cache"
trap - EXIT INT TERM
/usr/bin/printf 'verified Python %s and uv %s\n' "${python_fields[0]}" "${uv_fields[0]}"
