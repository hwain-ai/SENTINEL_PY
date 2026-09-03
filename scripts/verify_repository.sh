#!/usr/bin/bash
set -euo pipefail

readonly expected_user_name="황화인"
readonly expected_user_email="166008093+hwain-hwang@users.noreply.github.com"
readonly script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly repository_root="$(cd -- "${script_dir}/.." && pwd -P)"
readonly repository_name="$(basename -- "${repository_root}")"
readonly current_directory="$(pwd -P)"

fail() {
  /usr/bin/printf 'repository verification failed: %s\n' "$1" >&2
  exit 1
}

read_local_config() {
  local key="$1"
  local value

  value="$(/usr/bin/git config --local --get "${key}" 2>/dev/null)" ||
    fail "missing local Git config: ${key}"
  /usr/bin/printf '%s' "${value}"
}

[[ "${current_directory}" == "${repository_root}" ]] ||
  fail "run from repository root: expected ${repository_root}, got ${current_directory}"

git_root="$(/usr/bin/git rev-parse --show-toplevel 2>/dev/null)" ||
  fail "Git repository not initialized at ${repository_root}"
[[ "${git_root}" == "${repository_root}" ]] ||
  fail "Git top-level mismatch: expected ${repository_root}, got ${git_root}"

current_branch="$(/usr/bin/git symbolic-ref --quiet --short HEAD 2>/dev/null)" ||
  fail "current branch is not available"
[[ "${current_branch}" == "main" ]] ||
  fail "expected branch main, got ${current_branch}"

remote_names="$(/usr/bin/git remote)"
[[ -z "${remote_names}" ]] ||
  fail "expected no Git remotes, got: ${remote_names}"

for required_file in docs/index.md docs/log.md toolchain.lock.json; do
  [[ -f "${repository_root}/${required_file}" ]] ||
    fail "missing regular file: ${required_file}"
done

[[ "$(read_local_config core.autocrlf)" == "false" ]] ||
  fail "local core.autocrlf must be false"
[[ "$(read_local_config core.filemode)" == "true" ]] ||
  fail "local core.filemode must be true"
[[ "$(read_local_config user.name)" == "${expected_user_name}" ]] ||
  fail "local user.name must be ${expected_user_name}"
[[ "$(read_local_config user.email)" == "${expected_user_email}" ]] ||
  fail "local user.email must be ${expected_user_email}"

/usr/bin/printf 'repository verification passed: %s\n' "${repository_name}"
