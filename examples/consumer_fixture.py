from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import production_harness
from production_harness import (
    EXIT_COMPLETE,
    CommandTemplate,
    ForegroundRequest,
    run_until_boundary,
)


def _worker_source() -> str:
    return r'''from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("start", "resume"))
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--time-budget", type=float, required=True)
    args = parser.parse_args()

    state_path = args.state_root / args.task_id / "state.json"
    if args.operation == "start":
        atomic_write(state_path, {"machine_state": "WORK_REMAINS", "attempt": 1})
        return 10

    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("machine_state") != "WORK_REMAINS":
        return 50
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    imported_path = Path(production_harness.__file__).resolve()
    if repository_root in imported_path.parents:
        raise RuntimeError(f"source-tree import detected: {imported_path}")

    with tempfile.TemporaryDirectory(prefix="production-harness-consumer-") as raw:
        root = Path(raw)
        worker = root / "consumer_worker.py"
        worker.write_text(_worker_source(), encoding="utf-8")
        state_root = root / "state"
        task_id = "external-consumer"

        request = ForegroundRequest(
            operation="start",
            cwd=root,
            state_root=state_root,
            task_id=task_id,
            command_template=CommandTemplate(
                (
                    sys.executable,
                    str(worker),
                    "{operation}",
                    "--task-id",
                    "{task_id}",
                    "--state-root",
                    str(state_root),
                    "--time-budget",
                    "{invocation_time_budget}",
                )
            ),
            outer_time_budget=8.0,
            invocation_time_budget=2.0,
            max_invocations=3,
            reserve_seconds=0.2,
        )

        result = run_until_boundary(request, emit_output=False)
        if result != EXIT_COMPLETE:
            raise RuntimeError(f"unexpected foreground result: {result}")

        report_path = state_root / task_id / "foreground-supervisor.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("status") != "terminal":
            raise RuntimeError(f"unexpected report status: {report.get('status')}")
        if report.get("invocation_count") != 2:
            raise RuntimeError(f"unexpected invocation count: {report.get('invocation_count')}")
        operations = [item.get("operation") for item in report.get("invocations", [])]
        if operations != ["start", "resume"]:
            raise RuntimeError(f"unexpected operation sequence: {operations}")

    print(f"external consumer fixture passed: {imported_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
