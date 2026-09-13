"""Isolated execution bridge for the pinned mutmut 3.7.0 backend."""

from __future__ import annotations

import configparser
import hashlib
import hmac
import json
import os
import resource
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from ..backend_lock import verify_backend_lock
from ..config import LoadedProject
from ..mutation import MutantRecord
from ..mutmut_adapter import (
    enumerate_candidates,
    load_results,
    read_raw_exit_codes,
)
from ..project_files import DERIVED_DIRECTORY_NAMES, is_tool_owned_path, DEPENDENCY_DIRECTORY
from .pytest_reporter import (
    _OBSERVATION_SCHEMA,
    _failure_signature,
    _report_signature,
    mutmut_report_nonce,
    mutmut_report_path,
)


_OBSERVER_MODULE = "_sentinel_pytest_observer_v1"
_REPORTER_FILENAME = "pytest_reporter.py"
_BACKEND_STARTUP_TIMEOUT_SECONDS = 15 * 60
_BACKEND_IDLE_TIMEOUT_SECONDS = 10 * 60
_BACKEND_ABSOLUTE_TIMEOUT_SECONDS = 24 * 60 * 60
_BACKEND_POLL_SECONDS = 1


class MutationBackendError(RuntimeError):
    """Mutmut could not produce complete trustworthy mutant evidence."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class BaselineFailure(RuntimeError):
    """The unmodified project did not pass two complete pytest baselines."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MutationExecution:
    """The exact planned candidates and normalized backend records."""

    candidate_ids: tuple[str, ...]
    records: tuple[MutantRecord, ...]


@dataclass(frozen=True)
class _ProtectedFile:
    relative_path: str
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int
    digest: str


@dataclass(frozen=True)
class _PytestObservation:
    collected: tuple[str, ...]
    started: tuple[str, ...]
    exit_code: int
    failure_signatures: tuple[str, ...]
    assertion_only: bool

    @property
    def complete_pass(self) -> bool:
        return (
            self.exit_code == 0
            and bool(self.collected)
            and self.started == self.collected
            and not self.failure_signatures
        )


@dataclass(frozen=True)
class _BackendReports:
    root: Path
    run_nonce: str
    assertion_key: bytes = field(repr=False)


def run_mutmut(project: LoadedProject) -> MutationExecution:
    """Run fresh candidate generation, baselines, mutmut, and kill replay."""

    verify_backend_lock()
    before = _protected_inventory(project.project_root)
    try:
        return _run_in_temporary_snapshot(project)
    finally:
        after = _protected_inventory(project.project_root)
        if before != after:
            raise MutationBackendError("protectedSourceChanged")


def _run_in_temporary_snapshot(project: LoadedProject) -> MutationExecution:
    with tempfile.TemporaryDirectory(prefix="sentinel-py-mutmut-") as directory:
        temporary_root = Path(directory)
        snapshot = temporary_root / "project"
        _copy_project(project.project_root, snapshot)
        _prepare_observer(temporary_root)
        sources = _source_bytes(project)
        candidates = enumerate_candidates(sources)
        _require_two_baselines(project, snapshot, temporary_root)
        _install_backend_config(project, snapshot)
        backend_reports = _run_backend(snapshot, temporary_root)
        meta_paths = tuple(sorted((snapshot / "mutants").rglob("*.py.meta")))
        raw_results = read_raw_exit_codes(meta_paths)
        _require_raw_candidate_set(candidates, raw_results)
        failure_states = _classify_test_failures(
            project,
            snapshot,
            temporary_root,
            raw_results,
            backend_reports,
        )
        records = load_results(candidates, meta_paths, failure_states)
        return MutationExecution(candidates, records)


def _copy_project(project_root: Path, snapshot: Path) -> None:
    try:
        shutil.copytree(
            project_root,
            snapshot,
            ignore=shutil.ignore_patterns(*(DERIVED_DIRECTORY_NAMES - {DEPENDENCY_DIRECTORY})),
        )
    except OSError as error:
        raise MutationBackendError("snapshotCopyFailed") from error


def _prepare_observer(temporary_root: Path) -> Path:
    observer_root = temporary_root / "observer"
    source = _observer_source()
    if source.is_symlink() or not source.is_file():
        raise MutationBackendError("observerSourceInvalid")
    try:
        payload = source.read_bytes()
        observer_root.mkdir(mode=0o700)
        destination = observer_root / (_OBSERVER_MODULE + ".py")
        destination.write_bytes(payload)
        destination.chmod(0o600)
    except OSError as error:
        raise MutationBackendError("observerCopyFailed") from error
    return destination


def _observer_source() -> Path:
    current = Path(__file__)
    pristine_value = os.environ.get("SENTINEL_MUTMUT_PRISTINE_ROOT")
    if pristine_value is None:
        return current.with_name(_REPORTER_FILENAME)
    pristine = _nested_pristine_source(current, pristine_value)
    return current.with_name(_REPORTER_FILENAME) if pristine is None else pristine


def _nested_pristine_source(current: Path, pristine_value: str) -> Path | None:
    try:
        pristine_root = Path(pristine_value).resolve(strict=True)
        relative = current.resolve(strict=True).relative_to(pristine_root / "mutants")
    except (OSError, RuntimeError, ValueError):
        return None
    return (pristine_root / relative).with_name(_REPORTER_FILENAME)


def _install_backend_config(project: LoadedProject, snapshot: Path) -> None:
    _reject_existing_mutmut_config(snapshot)
    sources = _source_config_paths(project)
    tests = _test_config_paths(project)
    copies = _backend_copy_paths(snapshot)
    lines = _backend_config_lines(sources, tests, copies)
    setup_path = snapshot / "setup.cfg"
    existing = _optional_text(setup_path)
    try:
        setup_path.write_bytes((existing + "\n".join(lines)).encode())
    except OSError as error:
        raise MutationBackendError("backendConfigWriteFailed") from error


def _indented(value: str) -> str:
    return "    " + value


def _source_config_paths(project: LoadedProject) -> tuple[str, ...]:
    return tuple(
        source.path.relative_to(project.project_root).as_posix()
        for source in project.production_sources
    )


def _test_config_paths(project: LoadedProject) -> tuple[str, ...]:
    return tuple(
        path.relative_to(project.project_root).as_posix()
        for path in project.module.test_roots
    )


def _backend_copy_paths(snapshot: Path) -> tuple[str, ...]:
    try:
        entries = tuple(snapshot.iterdir())
    except OSError as error:
        raise MutationBackendError("backendCopyInventoryUnavailable") from error
    names = tuple(
        _backend_copy_name(path)
        for path in entries
        if not is_tool_owned_path(Path(path.name))
    )
    return tuple(sorted(names))


def _backend_copy_name(path: Path) -> str:
    value = path.name
    if any(character in value for character in ("\0", "\n", "\r")):
        raise MutationBackendError("backendCopyPathInvalid")
    try:
        value.encode()
    except UnicodeEncodeError as error:
        raise MutationBackendError("backendCopyPathInvalid") from error
    return value


def _backend_config_lines(
    sources: Sequence[str],
    tests: Sequence[str],
    copies: Sequence[str] = (),
) -> list[str]:
    lines = ["", "[mutmut]", "source_paths ="]
    lines.extend(_indented_lines(sources))
    lines.append("pytest_add_cli_args =")
    lines.extend(_indented_lines(("-p", _OBSERVER_MODULE)))
    lines.append("pytest_add_cli_args_test_selection =")
    lines.extend(_indented_lines(tests))
    lines.append("also_copy =")
    lines.extend(_indented_lines(copies))
    lines.extend(("mutate_only_covered_lines = false", "use_git_change_detection = false", ""))
    return lines


def _indented_lines(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(_indented(value) for value in values)


def _optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_bytes().decode()
    except (OSError, UnicodeError) as error:
        raise MutationBackendError("projectMetadataInvalid") from error


def _reject_existing_mutmut_config(snapshot: Path) -> None:
    pyproject = snapshot / "pyproject.toml"
    if pyproject.exists() and _pyproject_has_mutmut(pyproject):
        raise MutationBackendError("unauthorizedBackendConfig")
    setup = snapshot / "setup.cfg"
    if setup.exists() and _setup_has_mutmut(setup):
        raise MutationBackendError("unauthorizedBackendConfig")


def _pyproject_has_mutmut(path: Path) -> bool:
    try:
        document = tomllib.loads(path.read_bytes().decode())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise MutationBackendError("projectMetadataInvalid") from error
    tool = document.get("tool")
    return isinstance(tool, dict) and "mutmut" in tool


def _setup_has_mutmut(path: Path) -> bool:
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, UnicodeError, configparser.Error) as error:
        raise MutationBackendError("projectMetadataInvalid") from error
    return parser.has_section("mutmut")


def _source_bytes(project: LoadedProject) -> dict[str, bytes]:
    sources = {}
    for source in project.production_sources:
        relative = source.path.relative_to(project.project_root).as_posix()
        try:
            sources[relative] = source.path.read_bytes()
        except OSError as error:
            raise MutationBackendError("productionSourceUnavailable") from error
    return sources


def _require_two_baselines(
    project: LoadedProject,
    snapshot: Path,
    temporary_root: Path,
) -> None:
    selection = _pytest_selection(project)
    first = _run_pytest(snapshot, selection, "", temporary_root)
    second = _run_pytest(snapshot, selection, "", temporary_root)
    if not first.complete_pass or not second.complete_pass:
        raise BaselineFailure("baselineTestsFailed")
    if first.collected != second.collected:
        raise BaselineFailure("baselineInventoryChanged")


def _pytest_selection(project: LoadedProject) -> tuple[str, ...]:
    if project.module.test_command != ("python", "-m", "pytest"):
        raise BaselineFailure("unsupportedTestCommand")
    return tuple(
        path.relative_to(project.project_root).as_posix()
        for path in project.module.test_roots
    )


def _run_backend(snapshot: Path, temporary_root: Path) -> _BackendReports:
    report_root = temporary_root / "mutmut-reports"
    report_root.mkdir(mode=0o700)
    run_nonce = str(uuid.uuid4())
    assertion_key = secrets.token_bytes(32)
    command = (sys.executable, "-m", "mutmut", "run", "--max-children", "1")
    environment = _mutation_test_environment(temporary_root, snapshot)
    environment.update(
        {
            "SENTINEL_MUTMUT_REPORT_ROOT": str(report_root),
            "SENTINEL_MUTMUT_RUN_NONCE": run_nonce,
            "SENTINEL_PYTEST_HMAC_KEY": assertion_key.hex(),
        }
    )
    result = _run_process_with_progress(
        command,
        snapshot,
        environment,
        report_root,
        startup_timeout_seconds=_BACKEND_STARTUP_TIMEOUT_SECONDS,
        idle_timeout_seconds=_BACKEND_IDLE_TIMEOUT_SECONDS,
        absolute_timeout_seconds=_BACKEND_ABSOLUTE_TIMEOUT_SECONDS,
        poll_seconds=_BACKEND_POLL_SECONDS,
    )
    if result.returncode != 0:
        raise MutationBackendError("backendProcessFailed")
    return _BackendReports(report_root, run_nonce, assertion_key)


def _require_raw_candidate_set(
    candidates: Sequence[str],
    raw_results: Mapping[str, int | None],
) -> None:
    if frozenset(candidates) != frozenset(raw_results):
        raise MutationBackendError("candidateResultSetMismatch")


def _classify_test_failures(
    project: LoadedProject,
    snapshot: Path,
    temporary_root: Path,
    raw_results: Mapping[str, int | None],
    backend_reports: _BackendReports,
) -> dict[str, str]:
    failed = tuple(sorted(key for key, value in raw_results.items() if value == 1))
    if not failed:
        return {}
    selection = _pytest_selection(project)
    mutant_root = snapshot / "mutants"
    control = _run_pytest(
        mutant_root,
        selection,
        "",
        temporary_root,
        backend_reports.assertion_key,
    )
    if not control.complete_pass:
        raise BaselineFailure("baselineTestsFailed")
    return {
        candidate: _replay_failure(
            candidate,
            mutant_root,
            _initial_mutmut_observation(candidate, backend_reports),
            temporary_root,
            backend_reports.assertion_key,
        )
        for candidate in failed
    }


def _initial_mutmut_observation(
    candidate: str,
    reports: _BackendReports,
) -> _PytestObservation:
    runtime_candidate = candidate.replace("__init__.", "")
    return _load_observation(
        mutmut_report_path(reports.root, runtime_candidate),
        mutmut_report_nonce(reports.run_nonce, runtime_candidate),
        1,
        reports.assertion_key,
    )


def _replay_failure(
    candidate: str,
    mutant_root: Path,
    initial: _PytestObservation,
    temporary_root: Path,
    assertion_key: bytes,
) -> str:
    replay = _run_pytest(
        mutant_root,
        initial.collected,
        candidate,
        temporary_root,
        assertion_key,
    )
    valid = (
        initial.exit_code == replay.exit_code == 1
        and initial.collected == replay.collected
        and initial.started == replay.started
        and bool(initial.failure_signatures)
        and initial.failure_signatures == replay.failure_signatures
        and initial.assertion_only == replay.assertion_only
    )
    if not valid:
        raise MutationBackendError("killProofInvalid")
    return "killed" if initial.assertion_only else "runtimeError"


def _run_pytest(
    cwd: Path,
    selection: Sequence[str],
    mutant: str,
    temporary_root: Path,
    assertion_key: bytes | None = None,
) -> _PytestObservation:
    if assertion_key is None:
        assertion_key = secrets.token_bytes(32)
    _require_assertion_key(assertion_key)
    nonce = str(uuid.uuid4())
    report_root = temporary_root / "reports"
    report_root.mkdir(mode=0o700, exist_ok=True)
    report_path = report_root / f"{nonce}.json"
    if cwd.name == "mutants":
        environment = _mutation_test_environment(
            temporary_root,
            cwd.parent,
            cwd,
        )
    else:
        environment = _observer_test_environment(temporary_root, cwd)
    environment.update(
        {
            "MUTANT_UNDER_TEST": mutant,
            "SENTINEL_PYTEST_HMAC_KEY": assertion_key.hex(),
            "SENTINEL_PYTEST_NONCE": nonce,
            "SENTINEL_PYTEST_REPORT": str(report_path),
        }
    )
    command = (
        sys.executable,
        "-m",
        "pytest",
        "--rootdir=.",
        "-q",
        "-x",
        "-p",
        "no:randomly",
        "-p",
        "no:random-order",
        "-p",
        _OBSERVER_MODULE,
        *selection,
    )
    result = _run_process(command, cwd, environment)
    return _load_observation(report_path, nonce, result.returncode, assertion_key)


def _run_process(
    command: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    *,
    timeout_seconds: float = 300,
) -> subprocess.CompletedProcess:
    process = _start_process(command, cwd, environment)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process)
        raise MutationBackendError("childProcessTimedOut") from error
    return _completed_process(command, process, stdout, stderr)


def _run_process_with_progress(
    command: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    progress_root: Path,
    *,
    startup_timeout_seconds: float,
    idle_timeout_seconds: float,
    absolute_timeout_seconds: float,
    poll_seconds: float,
) -> subprocess.CompletedProcess:
    process = _start_process(command, cwd, environment)
    try:
        stdout, stderr = _wait_for_progress(
            process,
            progress_root,
            startup_timeout_seconds,
            idle_timeout_seconds,
            absolute_timeout_seconds,
            poll_seconds,
        )
    except BaseException:
        _terminate_process_group(process)
        raise
    return _completed_process(command, process, stdout, stderr)


def _start_process(
    command: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
) -> subprocess.Popen:
    protected_environment = dict(environment)
    protected_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        return subprocess.Popen(
            command,
            cwd=cwd,
            env=protected_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=_disable_core_dumps,
        )
    except OSError as error:
        raise MutationBackendError("childProcessFailed") from error


def _disable_core_dumps() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _completed_process(
    command: Sequence[str],
    process: subprocess.Popen,
    stdout: bytes,
    stderr: bytes,
) -> subprocess.CompletedProcess:
    if _process_group_exists(process.pid):
        _terminate_process_group(process)
        raise MutationBackendError("childProcessTreeNotDrained")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _wait_for_progress(
    process: subprocess.Popen,
    progress_root: Path,
    startup_timeout_seconds: float,
    idle_timeout_seconds: float,
    absolute_timeout_seconds: float,
    poll_seconds: float,
) -> tuple[bytes, bytes]:
    started_at = time.monotonic()
    last_progress_at = started_at
    fingerprint = _progress_fingerprint(progress_root)
    progress_timeout_seconds = startup_timeout_seconds
    while True:
        now = time.monotonic()
        active_deadline = last_progress_at + progress_timeout_seconds
        absolute_deadline = started_at + absolute_timeout_seconds
        deadline = min(active_deadline, absolute_deadline)
        if now >= deadline:
            _raise_progress_timeout(now, absolute_deadline)
        try:
            return process.communicate(timeout=min(poll_seconds, deadline - now))
        except subprocess.TimeoutExpired:
            current = _progress_fingerprint(progress_root)
            if current != fingerprint:
                fingerprint = current
                last_progress_at = time.monotonic()
                progress_timeout_seconds = idle_timeout_seconds


def _raise_progress_timeout(now: float, absolute_deadline: float) -> None:
    code = "backendProcessTimedOut" if now >= absolute_deadline else "backendProcessStalled"
    raise MutationBackendError(code)


def _progress_fingerprint(root: Path) -> tuple[tuple[str, int, int, int], ...]:
    try:
        entries = tuple(_progress_entry(path) for path in root.iterdir())
        return tuple(sorted(entries))
    except (OSError, UnicodeError) as error:
        raise MutationBackendError("backendProgressUnavailable") from error


def _progress_entry(path: Path) -> tuple[str, int, int, int]:
    metadata = path.lstat()
    return path.name, metadata.st_mode, metadata.st_size, metadata.st_mtime_ns


def _terminate_process_group(process: subprocess.Popen) -> None:
    _signal_process_group(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + 0.25
    while _process_group_exists(process.pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _process_group_exists(process.pid):
        _signal_process_group(process.pid, signal.SIGKILL)
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def _signal_process_group(process_group: int, requested_signal: signal.Signals) -> None:
    try:
        os.killpg(process_group, requested_signal)
    except ProcessLookupError:
        return


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _minimal_environment(temporary_root: Path) -> dict[str, str]:
    home = temporary_root / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }


def _mutation_test_environment(
    temporary_root: Path,
    pristine_root: Path,
    source_root: Path | None = None,
) -> dict[str, str]:
    environment = _observer_test_environment(temporary_root, source_root)
    # mutmut copies only project files into mutants/, so the dependencies stay in the pristine snapshot.
    _append_dependency_path(environment, pristine_root)
    environment["SENTINEL_MUTMUT_PRISTINE_ROOT"] = str(pristine_root)
    return environment


def _observer_test_environment(
    temporary_root: Path,
    source_root: Path | None = None,
) -> dict[str, str]:
    environment = _minimal_environment(temporary_root)
    observer_root = temporary_root / "observer"
    if source_root is None:
        environment["PYTHONPATH"] = str(observer_root)
        return environment
    python_paths = [str(observer_root)]
    python_paths.extend(
        str(path)
        for path in (source_root / "source", source_root / "src")
        if path.is_dir()
    )
    python_paths.append(str(source_root))
    environment["PYTHONSAFEPATH"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    _append_dependency_path(environment, source_root)
    return environment


def _append_dependency_path(environment: dict[str, str], root: Path) -> None:
    """Put <root>/.sentinel-deps last on PYTHONPATH so project code shadows its dependencies."""

    dependencies = root / DEPENDENCY_DIRECTORY
    if not dependencies.is_dir():
        return
    python_paths = environment["PYTHONPATH"].split(os.pathsep)
    if str(dependencies) not in python_paths:
        environment["PYTHONPATH"] = os.pathsep.join([*python_paths, str(dependencies)])


def _load_observation(
    path: Path,
    nonce: str,
    exit_code: int,
    assertion_key: bytes,
) -> _PytestObservation:
    _require_assertion_key(assertion_key)
    document = _read_observation_document(path)
    _validate_observation_envelope(document, nonce, exit_code, assertion_key)
    return _observation_values(document, assertion_key)


def _read_observation_document(path: Path) -> dict:
    try:
        document = json.loads(path.read_bytes().decode())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MutationBackendError("pytestObservationMissing") from error
    required = {
        "collected",
        "exitCode",
        "failures",
        "nonce",
        "reportSignature",
        "schemaVersion",
        "started",
    }
    if not isinstance(document, dict) or set(document) != required:
        raise MutationBackendError("pytestObservationInvalid")
    return document


def _validate_observation_envelope(
    document: dict,
    nonce: str,
    exit_code: int,
    assertion_key: bytes,
) -> None:
    signature = document["reportSignature"]
    if not _canonical_hmac_signature(signature):
        raise MutationBackendError("pytestObservationInvalid")
    unsigned = dict(document)
    del unsigned["reportSignature"]
    expected = _report_signature(assertion_key, unsigned)
    if not hmac.compare_digest(signature, expected):
        raise MutationBackendError("pytestObservationInvalid")
    if document["nonce"] != nonce or document["exitCode"] != exit_code:
        raise MutationBackendError("pytestObservationInvalid")
    if document["schemaVersion"] != _OBSERVATION_SCHEMA:
        raise MutationBackendError("pytestObservationInvalid")


def _observation_values(document: dict, assertion_key: bytes) -> _PytestObservation:
    collected = _text_array(document["collected"])
    started = _text_array(document["started"])
    failures = document["failures"]
    if not isinstance(failures, list):
        raise MutationBackendError("pytestObservationInvalid")
    failure_values = tuple(_failure_value(item, assertion_key) for item in failures)
    _validate_observation_relationships(collected, started, failure_values)
    signatures = tuple(item[0] for item in failure_values)
    assertions = tuple(item[1] for item in failure_values)
    exit_code = document["exitCode"]
    if type(exit_code) is not int:
        raise MutationBackendError("pytestObservationInvalid")
    return _PytestObservation(collected, started, exit_code, signatures, bool(assertions) and all(assertions))


def _validate_observation_relationships(
    collected: tuple[str, ...],
    started: tuple[str, ...],
    failures: tuple[tuple[str, bool, str, str], ...],
) -> None:
    if started != collected[: len(started)]:
        raise MutationBackendError("pytestObservationInvalid")
    identities = tuple((item[2], item[3]) for item in failures)
    if any(node_id not in started for node_id, _phase in identities):
        raise MutationBackendError("pytestObservationInvalid")
    if len(identities) != len(set(identities)):
        raise MutationBackendError("pytestObservationInvalid")


def _text_array(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise MutationBackendError("pytestObservationInvalid")
    values = tuple(value)
    if len(values) != len(set(values)):
        raise MutationBackendError("pytestObservationInvalid")
    return values


def _failure_value(value: object, assertion_key: bytes) -> tuple[str, bool, str, str]:
    _require_assertion_key(assertion_key)
    failure = _failure_record(value)
    assertion = _failure_assertion(failure["assertion"])
    exception_type = _failure_text(failure["exceptionType"])
    node_id = _failure_text(failure["nodeId"])
    phase = _failure_phase(failure["phase"])
    signature = _failure_hmac(failure["signature"])
    location = _failure_location(failure["location"])
    expected = _failure_signature(
        assertion_key,
        assertion,
        exception_type,
        node_id,
        phase,
        location,
    )
    if not hmac.compare_digest(signature, expected):
        raise MutationBackendError("pytestObservationInvalid")
    return signature, assertion, node_id, phase


def _failure_record(value: object) -> dict:
    fields = {
        "assertion",
        "exceptionType",
        "location",
        "nodeId",
        "phase",
        "signature",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise MutationBackendError("pytestObservationInvalid")
    return value


def _failure_assertion(value: object) -> bool:
    if type(value) is not bool:
        raise MutationBackendError("pytestObservationInvalid")
    return value


def _failure_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise MutationBackendError("pytestObservationInvalid")
    return value


def _failure_phase(value: object) -> str:
    if value not in ("setup", "call", "teardown"):
        raise MutationBackendError("pytestObservationInvalid")
    return value


def _failure_hmac(value: object) -> str:
    if not _canonical_hmac_signature(value):
        raise MutationBackendError("pytestObservationInvalid")
    return value


def _require_assertion_key(value: object) -> None:
    if type(value) is not bytes or len(value) != 32:
        raise MutationBackendError("pytestObservationInvalid")


def _canonical_hmac_signature(value: object) -> bool:
    prefix = "hmac-sha256:"
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    digest = value[len(prefix) :]
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _failure_location(value: object) -> dict[str, object]:
    location = _location_record(value)
    path = _location_path(location["path"])
    line = _location_integer(location["line"], 1)
    column = _location_integer(location["column"], -1)
    return {"column": column, "line": line, "path": path}


def _location_record(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"column", "line", "path"}:
        raise MutationBackendError("pytestObservationInvalid")
    return value


def _location_path(value: object) -> str:
    if not isinstance(value, str):
        raise MutationBackendError("pytestObservationInvalid")
    if not _canonical_relative_path(value):
        raise MutationBackendError("pytestObservationInvalid")
    return value


def _location_integer(value: object, minimum: int) -> int:
    if type(value) is not int:
        raise MutationBackendError("pytestObservationInvalid")
    if value < minimum:
        raise MutationBackendError("pytestObservationInvalid")
    return value


def _canonical_relative_path(value: str) -> bool:
    parts = value.split("/")
    return bool(value) and all(part not in ("", ".", "..") for part in parts)


def _protected_inventory(project_root: Path) -> tuple[_ProtectedFile, ...]:
    files = []
    for path in project_root.rglob("*"):
        relative = path.relative_to(project_root)
        if is_tool_owned_path(relative):
            continue
        if path.is_symlink():
            raise MutationBackendError("protectedPathSymlink")
        if path.is_file():
            files.append(_protected_file(path, relative.as_posix()))
    return tuple(sorted(files, key=_protected_file_sort_key))


def _protected_file_sort_key(item: _ProtectedFile) -> bytes:
    return item.relative_path.encode()


def _protected_file(path: Path, relative_path: str) -> _ProtectedFile:
    try:
        metadata = path.stat()
        payload = path.read_bytes()
    except OSError as error:
        raise MutationBackendError("protectedSourceUnavailable") from error
    return _ProtectedFile(
        relative_path=relative_path,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
        digest=hashlib.sha256(payload).hexdigest(),
    )
