"""Single policy for tool-owned directories outside project source scope."""

from __future__ import annotations

from pathlib import Path


DERIVED_DIRECTORY_NAMES = frozenset(
    (
        ".git",
        ".nox",
        ".pytest_cache",
        ".sentinel",
        ".toolchain",
        ".tox",
        ".venv",
        "__pycache__",
        "mutants",
        "venv",
    )
)


def is_tool_owned_path(relative_path: Path) -> bool:
    """Return whether a project-relative path belongs to a reserved tool directory."""

    return any(part in DERIVED_DIRECTORY_NAMES for part in relative_path.parts)
