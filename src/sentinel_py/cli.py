"""Harness-neutral SENTINEL_PY command-line surface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .backend_lock import BackendLockError
from .config import LoadedProject, UsageConfigError, load_project
from .coverage import CoverageFormatError, MetricOrderingError
from .crap import AnalysisError
from .evidence import EvidenceError, read_history
from .gate import GateInputError, load_gate
from .mutation import MutationGateError
from .mutmut_adapter import MutmutBridgeError
from .quality import (
    DependencyFailure,
    doctor_result,
    run_local_crap,
    run_strict_check,
    run_strict_crap,
    run_strict_mutation,
)
from .runner import BaselineFailure, CoverageRunnerError, MutationBackendError


TOOL_ERROR = 1
QUALITY_FAILED = 2
USAGE_CONFIG_ERROR = 3
BASELINE_FAILED = 4
DEPENDENCY_ERROR = 5
BACKEND_ERROR = 6
EVIDENCE_ERROR = 7
_QUALITY_COMMANDS = ("crap", "mutation", "check")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel-py",
        description="Strict Python CRAP and mutation quality gates.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in _QUALITY_COMMANDS:
        _add_quality_command(subparsers, command)
    _add_doctor_command(subparsers)
    _add_history_command(subparsers)
    return parser


def _add_quality_command(
    subparsers: argparse._SubParsersAction,
    command: str,
) -> None:
    parser = subparsers.add_parser(command)
    _add_project_selection(parser)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--strict", dest="mode", action="store_const", const="strict")
    mode.add_argument("--local", dest="mode", action="store_const", const="local")
    parser.set_defaults(mode="strict")
    _add_output_selection(parser)
    parser.add_argument("--correlation-id")
    parser.add_argument("--crap-max")
    parser.add_argument("--mutation-min")


def _add_doctor_command(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("doctor")
    _add_project_selection(parser)
    _add_output_selection(parser)


def _add_history_command(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("history")
    parser.add_argument("history_command", nargs="?", choices=("prune",))
    parser.add_argument("--project")
    parser.add_argument("--run-id")
    parser.add_argument("--repeated", action="store_true")
    parser.add_argument("--local-details", action="store_true")
    parser.add_argument("--export")
    parser.add_argument("--before")
    parser.add_argument("--incomplete", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--format", choices=("text", "json", "jsonl"), default="text")


def _add_project_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project")
    parser.add_argument("--config")
    parser.add_argument("--module")


def _add_output_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("text", "json", "jsonl"), default="text")
    parser.add_argument("--output")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return _dispatch(arguments)
    except (UsageConfigError, GateInputError) as error:
        return _emit_error(arguments, USAGE_CONFIG_ERROR, error.code, "usageConfigError")
    except (
        BackendLockError,
        DependencyFailure,
        CoverageFormatError,
        CoverageRunnerError,
    ) as error:
        code = getattr(error, "code", "coverageReportInvalid")
        return _emit_error(arguments, DEPENDENCY_ERROR, code, "dependencyError")
    except BaselineFailure as error:
        return _emit_error(arguments, BASELINE_FAILED, error.code, "baselineFailed")
    except (MutationBackendError, MutmutBridgeError, MutationGateError) as error:
        return _emit_error(arguments, BACKEND_ERROR, error.code, "backendError")
    except EvidenceError as error:
        return _emit_error(arguments, EVIDENCE_ERROR, str(error), "evidenceError")
    except (AnalysisError, MetricOrderingError) as error:
        return _emit_error(arguments, TOOL_ERROR, str(error), "toolError")


def _dispatch(arguments: argparse.Namespace) -> int:
    if arguments.command == "history":
        return _history(arguments)
    if _quality_command_pending(arguments):
        return _pending(arguments)
    gate = load_gate(
        getattr(arguments, "crap_max", None),
        getattr(arguments, "mutation_min", None),
    )
    project = load_project(arguments.project, arguments.config, arguments.module)
    return _dispatch_project_command(arguments, project, gate)


def _quality_command_pending(arguments: argparse.Namespace) -> bool:
    return (
        arguments.command in ("check", "mutation")
        and arguments.mode != "strict"
    )


def _dispatch_project_command(
    arguments: argparse.Namespace,
    project: LoadedProject,
    gate,
) -> int:
    if arguments.command == "doctor":
        return _emit_result(arguments, 0, doctor_result(project))
    if arguments.command == "check":
        exit_code, result = run_strict_check(project, arguments.correlation_id, gate)
        return _emit_result(arguments, exit_code, result)
    if arguments.command == "mutation":
        exit_code, result = run_strict_mutation(
            project,
            arguments.correlation_id,
            gate,
        )
        return _emit_result(arguments, exit_code, result)
    crap_runner = run_local_crap if arguments.mode == "local" else run_strict_crap
    exit_code, result = crap_runner(project, arguments.correlation_id, gate)
    return _emit_result(arguments, exit_code, result)


def _history(arguments: argparse.Namespace) -> int:
    if _history_write_or_detail_requested(arguments):
        return _pending(arguments)
    project_root = _history_project_root(arguments.project)
    result = read_history(project_root, repeated_only=arguments.repeated)
    return _emit_result(arguments, 0, result)


def _history_write_or_detail_requested(arguments: argparse.Namespace) -> bool:
    return any(
        (
            arguments.history_command is not None,
            arguments.run_id is not None,
            arguments.local_details,
            arguments.export is not None,
            arguments.before is not None,
            arguments.incomplete,
            arguments.confirm,
        )
    )


def _history_project_root(value: str | None) -> Path:
    raw = Path.cwd() if value is None else Path(value)
    try:
        project_root = raw.resolve()
    except (OSError, RuntimeError) as error:
        raise UsageConfigError("projectRootInvalid") from error
    if not project_root.is_dir():
        raise UsageConfigError("projectRootInvalid")
    return project_root


def _pending(arguments: argparse.Namespace) -> int:
    diagnostic = {
        "code": "commandPending",
        "command": arguments.command,
        "terminalStatus": "dependencyError",
    }
    return _emit_result(arguments, DEPENDENCY_ERROR, diagnostic, error_text=True)


def _emit_error(
    arguments: argparse.Namespace,
    exit_code: int,
    code: str,
    terminal_status: str,
) -> int:
    diagnostic = {"code": code, "terminalStatus": terminal_status}
    return _emit_result(arguments, exit_code, diagnostic, error_text=True)


def _emit_result(
    arguments: argparse.Namespace,
    exit_code: int,
    document: dict,
    *,
    error_text: bool = False,
) -> int:
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
    output = getattr(arguments, "output", None)
    if output is not None:
        _write_output(Path(output), payload)
    elif arguments.format in ("json", "jsonl"):
        sys.stdout.write(payload)
    elif error_text:
        sys.stderr.write(
            f"{document['terminalStatus']}: {document['code']}\n"
        )
    else:
        sys.stdout.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return exit_code


def _write_output(path: Path, payload: str) -> None:
    try:
        path.write_text(payload, encoding="utf-8")
    except OSError as error:
        raise EvidenceError("outputWriteFailed") from error
