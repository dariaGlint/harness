from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from production_harness.foreground import (
    EXIT_COMPLETE,
    EXIT_CONTINUE,
    EXIT_RETRY,
    EXIT_UNRECOVERABLE,
    ChildResult,
    CommandTemplate,
    ForegroundRequest,
    run_until_boundary,
)
from production_harness.state import atomic_write_json


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.01
        return self.value


class ForegroundTests(unittest.TestCase):
    def request(self, root: Path, **overrides: object) -> ForegroundRequest:
        values: dict[str, object] = {
            "operation": "start",
            "cwd": root,
            "state_root": root / "state",
            "task_id": "task-a",
            "command_template": CommandTemplate(("runner", "{operation}", "{task_id}", "{invocation_time_budget}")),
            "outer_time_budget": 10.0,
            "invocation_time_budget": 1.0,
            "max_invocations": 5,
            "reserve_seconds": 0.1,
        }
        values.update(overrides)
        return ForegroundRequest(**values)  # type: ignore[arg-type]

    def test_continues_10_then_40_then_completes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            results = iter([ChildResult(EXIT_CONTINUE), ChildResult(EXIT_RETRY), ChildResult(EXIT_COMPLETE)])
            commands: list[list[str]] = []

            def invoke(command, cwd, timeout, env):
                commands.append(list(command))
                atomic_write_json(root / "state" / "task-a" / "state.json", {"machine_state": "WORK_REMAINS"})
                return next(results)

            code = run_until_boundary(self.request(root), invoke=invoke, clock=FakeClock(), emit_output=False)
            self.assertEqual(code, EXIT_COMPLETE)
            self.assertEqual([command[1] for command in commands], ["start", "resume", "resume"])
            report = json.loads((root / "state" / "task-a" / "foreground-supervisor.json").read_text())
            self.assertEqual(report["invocation_count"], 3)
            self.assertEqual(report["continuation_count"], 2)
            self.assertEqual(report["status"], "terminal")

    def test_timeout_before_state_is_unrecoverable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def invoke(command, cwd, timeout, env):
                return ChildResult(EXIT_CONTINUE, timed_out=True)

            code = run_until_boundary(self.request(root), invoke=invoke, clock=FakeClock(), emit_output=False)
            self.assertEqual(code, EXIT_UNRECOVERABLE)
            report = json.loads((root / "state" / "task-a" / "foreground-supervisor.json").read_text())
            self.assertEqual(report["terminal_reason"], "child_timed_out_before_state_creation")

    def test_timeout_with_durable_state_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            atomic_write_json(root / "state" / "task-a" / "state.json", {"machine_state": "WORK_REMAINS"})
            results = iter([ChildResult(EXIT_CONTINUE, timed_out=True), ChildResult(EXIT_COMPLETE)])

            def invoke(command, cwd, timeout, env):
                return next(results)

            code = run_until_boundary(self.request(root), invoke=invoke, clock=FakeClock(), emit_output=False)
            self.assertEqual(code, EXIT_COMPLETE)

    def test_unexpected_exit_code_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def invoke(command, cwd, timeout, env):
                return ChildResult(7)

            code = run_until_boundary(self.request(root), invoke=invoke, clock=FakeClock(), emit_output=False)
            self.assertEqual(code, EXIT_UNRECOVERABLE)

    def test_max_invocations_yields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            atomic_write_json(root / "state" / "task-a" / "state.json", {"machine_state": "WORK_REMAINS"})

            def invoke(command, cwd, timeout, env):
                return ChildResult(EXIT_CONTINUE)

            code = run_until_boundary(
                self.request(root, max_invocations=2), invoke=invoke, clock=FakeClock(), emit_output=False
            )
            self.assertEqual(code, EXIT_CONTINUE)
            report = json.loads((root / "state" / "task-a" / "foreground-supervisor.json").read_text())
            self.assertEqual(report["terminal_reason"], "max_invocations_reached")
            self.assertEqual(report["status"], "yielded")

    def test_resume_latest_pins_one_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            atomic_write_json(root / "state" / "older" / "state.json", {"machine_state": "WORK_REMAINS"})
            atomic_write_json(root / "state" / "newer" / "state.json", {"machine_state": "RETRY_REQUIRED"})
            seen: list[str] = []

            def invoke(command, cwd, timeout, env):
                seen.append(command[2])
                return ChildResult(EXIT_COMPLETE)

            code = run_until_boundary(
                self.request(root, operation="resume-latest", task_id=""),
                invoke=invoke,
                clock=FakeClock(),
                emit_output=False,
            )
            self.assertEqual(code, EXIT_COMPLETE)
            self.assertEqual(seen, ["newer"])


if __name__ == "__main__":
    unittest.main()
