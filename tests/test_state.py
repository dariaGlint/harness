from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from production_harness.state import StateError, atomic_write_json, latest_unfinished_task_id, load_json_object


class StateTests(unittest.TestCase):
    def test_atomic_write_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "nested" / "state.json"
            atomic_write_json(path, {"task": "alpha", "count": 2})
            self.assertEqual(load_json_object(path), {"task": "alpha", "count": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["task"], "alpha")

    def test_latest_unfinished_ignores_terminal_and_corrupt_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            atomic_write_json(root / "older" / "state.json", {"machine_state": "WORK_REMAINS"})
            time.sleep(0.01)
            atomic_write_json(root / "complete" / "state.json", {"machine_state": "COMPLETE"})
            (root / "broken").mkdir()
            (root / "broken" / "state.json").write_text("{", encoding="utf-8")
            time.sleep(0.01)
            atomic_write_json(root / "newer" / "state.json", {"machine_state": "RETRY_REQUIRED"})
            task_id = latest_unfinished_task_id(root, terminal_machine_states={"COMPLETE"})
            self.assertEqual(task_id, "newer")

    def test_latest_unfinished_fails_closed_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(StateError):
                latest_unfinished_task_id(Path(raw), terminal_machine_states={"COMPLETE"})


if __name__ == "__main__":
    unittest.main()
