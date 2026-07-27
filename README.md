# production-harness

Reusable, fail-closed primitives for long-running production workflows.

This repository starts with the generic continuation layer extracted from the
Chaos project's Mandatory Workflow Foreground Supervisor. It intentionally does
not contain Chaos stages, game rules, repository names, Godot scene paths, or
project-specific quality gates.

## Included in v0.1

- atomic JSON state writes with file and directory `fsync` where supported;
- validated discovery of the latest unfinished task;
- bounded foreground continuation for configurable exit codes;
- cross-platform process-group termination and durable-state timeout recovery;
- fail-closed handling for invalid state and unknown exit codes;
- deterministic retry chunk shrinking that never increases work;
- a shell-free, strictly validated argument-vector command template;
- versioned packaged JSON Schemas for task state and foreground reports;
- Linux and Windows CI without Godot or private repository access.

## Library example

```python
from pathlib import Path

from production_harness import CommandTemplate, ForegroundRequest, run_until_boundary

request = ForegroundRequest(
    operation="start",
    cwd=Path.cwd(),
    state_root=Path("validation_output/workflow_state"),
    task_id="example-task",
    command_template=CommandTemplate((
        "python",
        "workflow.py",
        "{operation}",
        "--task-id",
        "{task_id}",
        "--time-budget",
        "{invocation_time_budget}",
    )),
)

raise SystemExit(run_until_boundary(request))
```

The command is executed directly without a shell. Supported placeholders are
`{operation}`, `{task_id}`, and `{invocation_time_budget}`. Formatting,
conversions, attribute access, and other placeholders are rejected.

## Contracts

The public API, minimal state envelope, versioned report schema, timeout
semantics, and compatibility rules are documented in
[`docs/contracts.md`](docs/contracts.md).

The report stores a digest of the command template rather than raw arguments.
Any `next_command` supplied by a consumer must already be redacted.

## Release evidence

`examples/consumer_fixture.py` is executed from an isolated virtual environment
containing only the built wheel. The validation script rejects source-tree
imports before exercising a real `start -> resume -> complete` flow.

Release publication steps are defined in
[`docs/release-checklist.md`](docs/release-checklist.md), and release notes are
maintained in [`CHANGELOG.md`](CHANGELOG.md).

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m compileall -q src tests
PYTHONPATH=src python -m production_harness.cli --help
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
python scripts/validate_installed_wheel.py
```

## Scope boundary

The public package owns generic state durability, retry policy, report contracts,
and continuation control. A consuming project owns its workflow state machine,
full task schema, quality gates, publication rules, repository integration, and
domain adapters.
