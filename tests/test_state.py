from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from production_harness.state import (
    StateError,
    atomic_write_json,
    latest_unfinished_task_id,
    load_json_object,
    load_valid_state_object,
)


class StateTests(unittest.TestCase):
    def test_atomic_write_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "nested" / "state.json"
            atomic_write_json(path, {"task": "alpha", "count": 2})
            self.assertEqual(load_json_object(path), {"task": "alpha", "count": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["task"], "alpha")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_valid_state_validator_accepts_and_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            atomic_write_json(path, {"machine_state": "WORK_REMAINS", "digest": "ok"})
            self.assertIsNotNone(
                load_valid_state_object(path, validator=lambda state: state.get("digest") == "ok")
            )
            self.assertIsNone(
                load_valid_state_object(path, validator=lambda state: state.get("digest") == "bad")
            )

    def test_validator_exception_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            atomic_write_json(path, {"machine_state": "WORK_REMAINS"})

            def broken_validator(state):
                raise RuntimeError("validator bug")

            with self.assertRaises(StateError):
                load_valid_state_object(path, validator=broken_validator)

    def test_latest_unfinished_ignores_terminal_corrupt_and_invalid_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            atomic_write_json(root / "older" / "state.json", {"machine_state": "WORK_REMAINS"})
            time.sleep(0.01)
            atomic_write_json(root / "complete" / "state.json", {"machine_state": "COMPLETE"})
            (root / "broken").mkdir()
            (root / "broken" / "state.json").write_text("{", encoding="utf-8")
            atomic_write_json(root / "missing-key" / "state.json", {"progress": 2})
            atomic_write_json(root / "empty-key" / "state.json", {"machine_state": ""})
            time.sleep(0.01)
            atomic_write_json(root / "newer" / "state.json", {"machine_state": "RETRY_REQUIRED"})
            task_id = latest_unfinished_task_id(root, terminal_machine_states={"COMPLETE"})
            self.assertEqual(task_id, "newer")

    def test_latest_unfinished_supports_custom_machine_state_key_and_validator(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            atomic_write_json(
                root / "rejected" / "state.json",
                {"phase": "WORK", "digest": "bad"},
            )
            atomic_write_json(
                root / "accepted" / "state.json",
                {"phase": "WORK", "digest": "good"},
            )
            task_id = latest_unfinished_task_id(
                root,
                terminal_machine_states={"DONE"},
                machine_state_key="phase",
                validator=lambda state: state.get("digest") == "good",
            )
            self.assertEqual(task_id, "accepted")

    def test_latest_unfinished_fails_closed_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(StateError):
                latest_unfinished_task_id(Path(raw), terminal_machine_states={"COMPLETE"})


if __name__ == "__main__":
    unittest.main()
