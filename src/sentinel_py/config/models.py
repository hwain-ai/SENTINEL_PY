"""Immutable values shared by Python config resolution and scope classification."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class UsageConfigError(ValueError):
    """A stable fail-closed config error that is safe for CLI mapping."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ScopeError(UsageConfigError):
    """A native source could not be assigned to one approved scope."""


@dataclass(frozen=True)
class ModuleConfig:
    """Untrusted values from one module entry before validation."""

    module_id: str
    language: str
    root: object
    production: object
    test_command: object | None = None
    coverage_command: object | None = None
    coverage_format: object | None = None
    coverage_report: object | None = None
    test_roots: object | None = None
    test_patterns: object | None = None


@dataclass(frozen=True)
class PythonDefaults:
    """Versioned values approved by the Python language adapter."""

    test_command: tuple[str, ...]
    coverage_command: tuple[str, ...]
    coverage_format: str
    coverage_report: Path
    test_roots: tuple[Path, ...]
    test_patterns: tuple[str, ...]


@dataclass(frozen=True)
class ModuleOverrides:
    """Explicit command-line values for the selected module only."""

    production: object | None = None
    test_command: object | None = None
    coverage_command: object | None = None
    coverage_format: object | None = None
    coverage_report: object | None = None
    test_roots: object | None = None
    test_patterns: object | None = None


@dataclass(frozen=True)
class ResolvedModule:
    """A complete Python module configuration with immutable argv."""

    project_root: Path
    module_id: str
    root: Path
    language: Literal["python"]
    production: tuple[Path, ...]
    test_command: tuple[str, ...]
    coverage_command: tuple[str, ...]
    coverage_format: str
    coverage_report: Path
    test_roots: tuple[Path, ...]
    test_patterns: tuple[str, ...]


@dataclass(frozen=True)
class ScopeEvidence:
    """Exact source inventories verified by their owning subsystem."""

    verified_tests: tuple[Path, ...] = ()
    generated: tuple[Path, ...] = ()
    vendor: tuple[Path, ...] = ()
    build_output: tuple[Path, ...] = ()


SourceCategory = Literal[
    "production",
    "verified-test",
    "generated",
    "vendor",
    "build-output",
]


@dataclass(frozen=True)
class ClassifiedSource:
    """One project-relative Python source and its single classification."""

    path: Path
    category: SourceCategory
    module_id: str | None


@dataclass(frozen=True)
class ModuleProduction:
    """The production files owned by one Python module."""

    module_id: str
    files: tuple[Path, ...]


@dataclass(frozen=True)
class ClassifiedScope:
    """The reconciled project inventory plus selected-module production."""

    module_id: str
    production: tuple[Path, ...]
    production_by_module: tuple[ModuleProduction, ...]
    verified_tests: tuple[Path, ...]
    generated: tuple[Path, ...]
    vendor: tuple[Path, ...]
    build_output: tuple[Path, ...]
    native_sources: tuple[Path, ...]
    classifications: tuple[ClassifiedSource, ...]
