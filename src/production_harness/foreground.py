"""Bounded foreground continuation for resumable command-line workflows."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .state import StateError, atomic_write_json, latest_unfinished_task_id

EXIT_COMPLETE = 0
EXIT_NOT_READY = 2
EXIT_CONTINUE = 10
EXIT_AWAITING_USER_APPROVAL = 20
EXIT_BLOCKED = 30
EXIT_RETRY = 40
EXIT_UNRECOVERABLE = 50

DEFAULT_CONTINUATION_CODES = frozenset({EXIT_CONTINUE, EXIT_RETRY})
DEFAULT_TERMINAL_CODES = frozenset(
    {EXIT_COMPLETE, EXIT_NOT_READY, EXIT_AWAITING_USER_APPROVAL, EXIT_BLOCKED, EXIT_UNRECOVERABLE}
)
DEFAULT_TERMINAL_MACHINE_STATES = frozenset(
    {
        "BLOCKED_BY_DECLARED_STOP",
        "PREVIEW_NOT_READY",
        "AWAITING_USER_APPROVAL",
        "APPROVED_FOR_PUBLISH",
        "COMPLETE",
        "UNRECOVERABLE",
    }
)


class ForegroundSupervisorError(RuntimeError):
    """Fail-closed supervisor configuration or execution error."""


@dataclass(frozen=True)
class ChildResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class CommandTemplate:
    """Argument-vector template rendered without shell evaluation."""

    arguments: tuple[str, ...]

    def render(self, *, operation: str, task_id: str, time_budget: float) -> list[str]:
        values = {
            "operation": operation,
            "task_id": task_id,
            "invocation_time_budget": f"{time_budget:.3f}",
        }
        try:
            return [argument.format_map(values) for argument in self.arguments]
        except KeyError as exc:
            raise ForegroundSupervisorError(f"Unknown command placeholder: {exc.args[0]}") from exc


@dataclass(frozen=True)
class ForegroundRequest:
    operation: str
    cwd: Path
    state_root: Path
    task_id: str
    command_template: CommandTemplate
    outer_time_budget: float = 40.0
    invocation_time_budget: float = 12.0
    max_invocations: int = 16
    reserve_seconds: float = 1.5
    report_path: Path | None = None
    state_filename: str = "state.json"
    continuation_codes: frozenset[int] = field(default_factory=lambda: DEFAULT_CONTINUATION_CODES)
    terminal_codes: frozenset[int] = field(default_factory=lambda: DEFAULT_TERMINAL_CODES)
    terminal_machine_states: frozenset[str] = field(
        default_factory=lambda: DEFAULT_TERMINAL_MACHINE_STATES
    )
    next_command: tuple[str, ...] | None = None


Invoker = Callable[[Sequence[str], Path, float, Mapping[str, str] | None], ChildResult]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _terminate_process_group(process: subprocess.Popen[str], grace_seconds: float = 0.5) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=max(0.05, grace_seconds))
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=max(0.05, grace_seconds))
    except subprocess.TimeoutExpired:
        pass


def run_child(
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: float,
    env: Mapping[str, str] | None = None,
) -> ChildResult:
    """Run a child in its own process group and convert timeout to continuation."""
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
        env=dict(env) if env is not None else None,
    )
    try:
        stdout, stderr = process.communicate(timeout=max(0.1, timeout_seconds))
        return ChildResult(process.returncode, stdout, stderr, False)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        stdout, stderr = process.communicate()
        detail = f"PRODUCTION_HARNESS_CHILD_TIMEOUT after {timeout_seconds:.3f}s"
        stderr = (stderr.rstrip() + "\n" + detail + "\n").lstrip("\n")
        return ChildResult(EXIT_CONTINUE, stdout, stderr, True)


def _validate_request(request: ForegroundRequest) -> None:
    if request.operation not in {"start", "resume", "resume-latest"}:
        raise ForegroundSupervisorError(f"Unsupported operation: {request.operation}")
    if request.outer_time_budget <= 0:
        raise ForegroundSupervisorError("outer_time_budget must be positive")
    if request.invocation_time_budget <= 0:
        raise ForegroundSupervisorError("invocation_time_budget must be positive")
    if request.max_invocations < 1:
        raise ForegroundSupervisorError("max_invocations must be positive")
    if request.reserve_seconds < 0:
        raise ForegroundSupervisorError("reserve_seconds must not be negative")
    if not request.command_template.arguments:
        raise ForegroundSupervisorError("command_template must not be empty")
    if request.continuation_codes & request.terminal_codes:
        raise ForegroundSupervisorError("continuation_codes and terminal_codes must not overlap")


def _default_terminal_reason(returncode: int) -> str:
    return {
        EXIT_COMPLETE: "complete",
        EXIT_NOT_READY: "external_prerequisite_not_ready",
        EXIT_AWAITING_USER_APPROVAL: "awaiting_user_approval",
        EXIT_BLOCKED: "declared_stop",
        EXIT_UNRECOVERABLE: "unrecoverable",
    }.get(returncode, f"terminal_exit_code_{returncode}")


def run_until_boundary(
    request: ForegroundRequest,
    *,
    invoke: Invoker = run_child,
    clock: Callable[[], float] = time.monotonic,
    env: Mapping[str, str] | None = None,
    emit_output: bool = True,
) -> int:
    """Reinvoke one resumable task until a declared boundary or bounded yield."""
    _validate_request(request)
    request = replace(request, cwd=request.cwd.resolve(), state_root=request.state_root.resolve())
    task_id = request.task_id
    operation = request.operation
    if operation == "resume-latest":
        try:
            task_id = latest_unfinished_task_id(
                request.state_root,
                terminal_machine_states=request.terminal_machine_states,
                state_filename=request.state_filename,
            )
        except StateError as exc:
            raise ForegroundSupervisorError(str(exc)) from exc
        request = replace(request, task_id=task_id, operation="resume")
        operation = "resume"

    report_path = request.report_path or request.state_root / task_id / "foreground-supervisor.json"
    started_at = utc_now()
    started_clock = clock()
    deadline = started_clock + request.outer_time_budget
    invocations: list[dict[str, Any]] = []
    last_exit_code: int | None = None
    terminal_reason = "not_started"

    def task_state_exists() -> bool:
        return (request.state_root / task_id / request.state_filename).is_file()

    def write_report(status: str) -> None:
        atomic_write_json(
            report_path,
            {
                "schema_version": 1,
                "task_id": task_id,
                "status": status,
                "terminal_reason": terminal_reason,
                "last_exit_code": last_exit_code,
                "invocation_count": len(invocations),
                "continuation_count": sum(
                    1 for item in invocations if item["returncode"] in request.continuation_codes
                ),
                "outer_time_budget": request.outer_time_budget,
                "invocation_time_budget": request.invocation_time_budget,
                "max_invocations": request.max_invocations,
                "started_at": started_at,
                "updated_at": utc_now(),
                "elapsed_seconds": max(0.0, clock() - started_clock),
                "cwd": str(request.cwd),
                "state_root": str(request.state_root),
                "command_template": list(request.command_template.arguments),
                "invocations": invocations,
                "next_command": list(request.next_command) if status == "yielded" and request.next_command else None,
            },
        )

    while len(invocations) < request.max_invocations:
        remaining = deadline - clock()
        if remaining <= request.reserve_seconds + 0.1:
            terminal_reason = "outer_time_budget_exhausted"
            last_exit_code = EXIT_CONTINUE
            write_report("yielded")
            return EXIT_CONTINUE

        invocation_budget = min(
            request.invocation_time_budget,
            max(0.1, remaining - request.reserve_seconds),
        )
        command = request.command_template.render(
            operation=operation,
            task_id=task_id,
            time_budget=invocation_budget,
        )
        child_timeout = max(
            0.1,
            min(remaining - 0.05, invocation_budget + max(1.0, request.reserve_seconds)),
        )
        result = invoke(command, request.cwd, child_timeout, env)
        if emit_output:
            if result.stdout:
                print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
            if result.stderr:
                print(
                    result.stderr,
                    file=sys.stderr,
                    end="" if result.stderr.endswith("\n") else "\n",
                )
        last_exit_code = result.returncode
        invocations.append(
            {
                "index": len(invocations) + 1,
                "operation": operation,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "invocation_time_budget": invocation_budget,
                "recorded_at": utc_now(),
            }
        )

        if result.timed_out and not task_state_exists():
            terminal_reason = "child_timed_out_before_state_creation"
            last_exit_code = EXIT_UNRECOVERABLE
            write_report("terminal")
            return EXIT_UNRECOVERABLE

        if result.returncode in request.continuation_codes:
            operation = "resume"
            terminal_reason = "continuation_requested"
            write_report("running")
            continue
        if result.returncode in request.terminal_codes:
            terminal_reason = _default_terminal_reason(result.returncode)
            write_report("terminal")
            return result.returncode

        terminal_reason = f"unexpected_exit_code_{result.returncode}"
        last_exit_code = EXIT_UNRECOVERABLE
        write_report("terminal")
        return EXIT_UNRECOVERABLE

    terminal_reason = "max_invocations_reached"
    last_exit_code = EXIT_CONTINUE
    write_report("yielded")
    return EXIT_CONTINUE
