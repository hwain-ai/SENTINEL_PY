"""Fresh coverage.py execution inside a disposable project snapshot."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from ..config import LoadedProject
from ..project_files import DEPENDENCY_DIRECTORY
from .mutation_backend import (
    MutationBackendError,
    _copy_project,
    _observer_test_environment,
    _prepare_observer,
    _protected_inventory,
    _require_two_baselines,
    _run_process,
)


class CoverageRunnerError(RuntimeError):
    """Fresh coverage could not be generated with the pinned adapter."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CoverageExecution:
    """Snapshot source bytes plus the newly generated coverage report."""

    sources: Mapping[str, bytes]
    report: bytes


def run_fresh_coverage(project: LoadedProject) -> CoverageExecution:
    """Run two baselines and one new coverage capture without touching originals."""

    before = _safe_inventory(project.project_root)
    try:
        execution = _run_snapshot_coverage(project)
    except MutationBackendError as error:
        raise CoverageRunnerError(error.code) from error
    finally:
        after = _safe_inventory(project.project_root)
        if before != after:
            raise CoverageRunnerError("protectedSourceChanged")
    return execution


def _safe_inventory(project_root: Path):
    try:
        return _protected_inventory(project_root)
    except MutationBackendError as error:
        raise CoverageRunnerError(error.code) from error


def _run_snapshot_coverage(project: LoadedProject) -> CoverageExecution:
    with tempfile.TemporaryDirectory(prefix="sentinel-py-coverage-") as directory:
        temporary_root = Path(directory)
        snapshot = temporary_root / "project"
        _copy_project(project.project_root, snapshot)
        _prepare_observer(temporary_root)
        _require_two_baselines(project, snapshot, temporary_root)
        module_root = _snapshot_module_root(project, snapshot)
        report_path = _snapshot_report_path(project, snapshot)
        _remove_old_report(report_path)
        _execute_coverage(project, module_root, report_path, temporary_root)
        return CoverageExecution(
            MappingProxyType(_snapshot_sources(project, snapshot)),
            _read_report(report_path),
        )


def _snapshot_module_root(project: LoadedProject, snapshot: Path) -> Path:
    relative = project.module.root.relative_to(project.project_root)
    return snapshot / relative


def _snapshot_report_path(project: LoadedProject, snapshot: Path) -> Path:
    relative = project.module.coverage_report.relative_to(project.project_root)
    return snapshot / relative


def _remove_old_report(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise CoverageRunnerError("coverageReportPathInvalid")
    path.unlink()


def _execute_coverage(
    project: LoadedProject,
    module_root: Path,
    report_path: Path,
    temporary_root: Path,
) -> None:
    formatter = _coverage_formatter(project)
    coverage_config = temporary_root / "coverage.ini"
    try:
        # The dependency directory is third-party code: measured lines there would only slow the run.
        coverage_config.write_text(
            f"[run]\nomit =\n    {DEPENDENCY_DIRECTORY}/*\n[report]\nexclude_lines =\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise CoverageRunnerError("coverageConfigWriteFailed") from error
    environment = _observer_test_environment(temporary_root, module_root)
    # 프로젝트별 제외·누락 설정이 검사 결과에서 생산 코드 줄을 숨기지 못하게 한다.
    environment["COVERAGE_RCFILE"] = str(coverage_config)
    tests = _module_test_roots(project)
    commands = (
        (sys.executable, "-m", "coverage", "erase"),
        (
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "--source=.",
            "-m",
            "pytest",
            *tests,
        ),
        (*formatter, "-o", report_path.relative_to(module_root).as_posix()),
    )
    for command in commands:
        result = _run_process(command, module_root, environment)
        if result.returncode != 0:
            raise CoverageRunnerError("coverageExecutionFailed")


def _coverage_formatter(project: LoadedProject) -> tuple[str, ...]:
    configured = project.module.coverage_command
    if configured != ("python", "-m", "coverage", "json"):
        raise CoverageRunnerError("unsupportedCoverageCommand")
    return (sys.executable, *configured[1:])


def _module_test_roots(project: LoadedProject) -> tuple[str, ...]:
    return tuple(
        path.relative_to(project.module.root).as_posix()
        for path in project.module.test_roots
    )


def _snapshot_sources(project: LoadedProject, snapshot: Path) -> dict[str, bytes]:
    sources = {}
    for source in project.production_sources:
        project_relative = source.path.relative_to(project.project_root)
        snapshot_path = snapshot / project_relative
        try:
            sources[source.module_relative_path] = snapshot_path.read_bytes()
        except OSError as error:
            raise CoverageRunnerError("productionSourceUnavailable") from error
    return sources


def _read_report(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise CoverageRunnerError("coverageReportUnavailable")
    try:
        return path.read_bytes()
    except OSError as error:
        raise CoverageRunnerError("coverageReportUnavailable") from error
