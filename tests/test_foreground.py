from __future__ import annotations

import json
import os
import sys
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
    ForegroundSupervisorError,
    _popen_process_group_options,
    run_child,
    run_until_boundary,
)
from production_harness.report import load_foreground_report_schema
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
            "command_template": CommandTemplate(
                ("runner", "{operation}", "{task_id}", "{invocation_time_budget}")
            ),
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
            results = iter(
                [ChildResult(EXIT_CONTINUE), ChildResult(EXIT_RETRY), ChildResult(EXIT_COMPLETE)]
            )
            commands: list[list[str]] = []

            def invoke(command, cwd, timeout, env):
                commands.append(list(command))
                atomic_write_json(
                    root / "state" / "task-a" / "state.json",
                    {"machine_state": "WORK_REMAINS"},
                )
                return next(results)

            code = run_until_boundary(
                self.request(root), invoke=invoke, clock=FakeClock(), emit_output=False
            )
            self.assertEqual(code, EXIT_COMPLETE)
            self.assertEqual([command[1] for command in commands], ["start", "resume", "resume"])
            report = json.loads(
                (root / "state" / "task-a" / "foreground-supervisor.json").read_text()
            )
            self.assertEqual(report["invocation_count"], 3)
            self.assertEqual(report["continuation_count"], 2)
            self.assertEqual(report["timed_out_count"], 0)
            self.assertEqual(report["status"], "terminal")

    def test_timeout_before_state_is_unrecoverable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def invoke(command, cwd, timeout, env):
                return ChildResult(EXIT_CONTINUE, timed_out=True)

            code = run_until_boundary(
                self.request(root), invoke=invoke, clock=FakeClock(), emit_output=False
            )
            self.assertEqual(code, EXIT_UNRECOVERABLE)
            report = json.loads(
                (root / "state" / "task-a" / "foreground-supervisor.json").read_text()
            )
            self.assertEqual(report["terminal_reason"], "child_timed_out_without_valid_state")

    def test_timeout_with_corrupt_state_is_unrecoverable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "state" / "task-a" / "state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{", encoding="utf-8")

            def invoke(command, cwd, timeout, env):
                return ChildResult(EXIT_CONTINUE, timed_out=True)

            code = run_until_boundary(
                self.request(root), invoke=invoke, clock=FakeClock(), emit_output=False
            )
            self.assertEqual(code, EXIT_UNRECOVERABLE)

    def test_timeout_with_missing_machine_state_is_unrecoverable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            atomic_write_json(root / "state" / "task-a" / "state.json", {"progress": 3})

            def invoke(command, cwd, timeout, env):
                return ChildResult(EXIT_CONTINUE, timed_out=True)

            code = run_until_boundary(
                self.request(root), invoke=invoke, clock=FakeClock(), emit_output=False
            )
            self.assertEqual(code, EXIT_UNRECOVERABLE)

    def test_timeout_with_validator_rejected_state_is_unrecoverable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            atomic_write_json(
                root / "state" / "task-a" / "state.json",
                {"machine_state": "WORK_REMAINS", "digest": "bad"},
            )

            def invoke(command, cwd, timeout, env):
                return ChildResult(EXIT_CONTINUE, timed_out=True)

            code = run_until_boundary(
                self.request(root, state_validator=lambda value: value.get("digest") == "good"),
                invoke=invoke,
                clock=FakeClock(),
                emit_output=False,
            )
            self.assertEqual(code, EXIT_UNRECOVERABLE)

    def test_timeout_with_valid_state_resumes_independent_of_exit_code_policy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            atomic_write_json(
                root / "state" / "task-a" / "state.json",
                {"machine_state": "WORK_REMAINS"},
            )
            results = iter([ChildResult(EXIT_CONTINUE, timed_out=True), ChildResult(EXIT_COMPLETE)])
            commands: list[list[str]] = []

            def invoke(command, cwd, timeout, env):
                commands.append(list(command))
                return next(results)

            code = run_until_boundary(
                self.request(
                    root,
                    continuation_codes=frozenset({99}),
                    terminal_codes=frozenset({EXIT_COMPLETE, EXIT_UNRECOVERABLE}),
                ),
                invoke=invoke,
                clock=FakeClock(),
                emit_output=False,
            )
            self.assertEqual(code, EXIT_COMPLETE)
            self.assertEqual([command[1] for command in commands], ["start", "resume"])
            report = json.loads(
                (root / "state" / "task-a" / "foreground-supervisor.json").read_text()
            )
            self.assertEqual(report["continuation_count"], 0)
            self.assertEqual(report["timed_out_count"], 1)

    def test_unexpected_exit_code_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def invoke(command, cwd, timeout, env):
                return ChildResult(7)

            code = run_until_boundary(
                self.request(root), invoke=invoke, clock=FakeClock(), emit_output=False
            )
            self.assertEqual(code, EXIT_UNRECOVERABLE)

    def test_max_invocations_yields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            atomic_write_json(
                root / "state" / "task-a" / "state.json", {"machine_state": "WORK_REMAINS"}
            )

            def invoke(command, cwd, timeout, env):
                return ChildResult(EXIT_CONTINUE)

            code = run_until_boundary(
                self.request(root, max_invocations=2),
                invoke=invoke,
                clock=FakeClock(),
                emit_output=False,
            )
            self.assertEqual(code, EXIT_CONTINUE)
            report = json.loads(
                (root / "state" / "task-a" / "foreground-supervisor.json").read_text()
            )
            self.assertEqual(report["terminal_reason"], "max_invocations_reached")
            self.assertEqual(report["status"], "yielded")

    def test_resume_latest_pins_one_valid_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            atomic_write_json(root / "state" / "invalid" / "state.json", {"progress": 1})
            atomic_write_json(
                root / "state" / "newer" / "state.json", {"machine_state": "RETRY_REQUIRED"}
            )
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

    def test_report_matches_packaged_contract_and_redacts_template(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def invoke(command, cwd, timeout, env):
                return ChildResult(EXIT_COMPLETE)

            request = self.request(
                root,
                command_template=CommandTemplate(
                    ("runner", "--token", "private-value", "{operation}", "{task_id}")
                ),
            )
            self.assertEqual(
                run_until_boundary(request, invoke=invoke, clock=FakeClock(), emit_output=False),
                EXIT_COMPLETE,
            )
            report = json.loads(
                (root / "state" / "task-a" / "foreground-supervisor.json").read_text()
            )
            schema = load_foreground_report_schema()
            self.assertEqual(set(report), set(schema["properties"]))
            self.assertTrue(set(schema["required"]).issubset(report))
            self.assertNotIn("command_template", report)
            self.assertNotIn("private-value", json.dumps(report))
            self.assertRegex(report["command_template_sha256"], r"^[0-9a-f]{64}$")

    def test_command_template_rejects_unknown_or_formatted_placeholders(self) -> None:
        with self.assertRaises(ForegroundSupervisorError):
            CommandTemplate(("{unknown}",))
        with self.assertRaises(ForegroundSupervisorError):
            CommandTemplate(("{task_id!r}",))
        with self.assertRaises(ForegroundSupervisorError):
            CommandTemplate(("{task_id:>10}",))

    def test_process_group_options_match_current_platform(self) -> None:
        options = _popen_process_group_options()
        if os.name == "nt":
            self.assertIn("creationflags", options)
        else:
            self.assertEqual(options, {"start_new_session": True})

    def test_real_child_timeout_is_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = run_child(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                Path(raw),
                0.1,
            )
            self.assertTrue(result.timed_out)
            self.assertIn("PRODUCTION_HARNESS_CHILD_TIMEOUT", result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX signal semantics")
    def test_sigterm_resistant_child_is_force_killed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            script = (
                "import signal,time; "
                "signal.signal(signal.SIGTERM, lambda *_: None); "
                "time.sleep(30)"
            )
            result = run_child([sys.executable, "-c", script], Path(raw), 0.1)
            self.assertTrue(result.timed_out)


if __name__ == "__main__":
    unittest.main()
