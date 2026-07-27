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
- checkpoint ZIP publication through one validated Git tree and one commit;
- append-only, hash-chained evidence ledgers with external head anchors;
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

## Workspace Commit Bridge

```python
from production_harness import commit_checkpoint_to_github

result = commit_checkpoint_to_github(
    repository="owner/repository",
    base_sha="0123456789abcdef0123456789abcdef01234567",
    branch_name="agent/example-task",
    commit_message="Add example task",
    checkpoint_zip="checkpoint.zip",
    handoff_json="handoff.json",
    create_pr=True,
)
```

The bridge reads only paths declared by `handoff.json`, validates SHA-256 and Git
blob SHAs, creates one tree and one commit, verifies the exact remote diff, and
then creates or fast-forwards the work branch. Source bytes are sent directly
from the bridge process to GitHub and are not expanded into a conversation or
connector argument. See
[`docs/workspace-commit-bridge.md`](docs/workspace-commit-bridge.md).

## Evidence Ledger

```python
from pathlib import Path

from production_harness import (
    append_ledger_event,
    verify_ledger,
    write_ledger_snapshot,
)

ledger = Path("validation_output/evidence-ledger.jsonl")
append_ledger_event(
    ledger,
    event_type="task.completed",
    subject_id="task-123",
    payload={"result": "PASS"},
    expected_sequence=1,
)
write_ledger_snapshot(ledger, Path("trusted/evidence-ledger-head.json"))
verification = verify_ledger(ledger, expected_event_count=1)
```

Each canonical JSONL event binds the preceding hash, its sequence, subject,
payload, actor, and optional external evidence files. A trusted head snapshot is
required to detect deletion of complete trailing records or a fully recomputed
ledger. See [`docs/evidence-ledger.md`](docs/evidence-ledger.md).

## Contracts

The public API, minimal state envelope, versioned report and evidence-ledger
schemas, timeout semantics, commit handoff schema, and compatibility rules are
documented in [`docs/contracts.md`](docs/contracts.md).

The report stores a digest of the command template rather than raw arguments.
Any `next_command` supplied by a consumer must already be redacted.

## Release evidence

`examples/consumer_fixture.py` is executed from an isolated virtual environment
containing only the built wheel. The validation script rejects source-tree
imports before exercising a real `start -> resume -> complete` flow and the
installed Evidence Ledger API and packaged schema.

Release publication steps are defined in
[`docs/release-checklist.md`](docs/release-checklist.md), and release notes are
maintained in [`CHANGELOG.md`](CHANGELOG.md).

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m compileall -q src tests
PYTHONPATH=src python -m production_harness.cli --help
PYTHONPATH=src python -m production_harness.commit_bridge_cli --help
PYTHONPATH=src python -m production_harness.evidence_ledger_cli --help
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
python scripts/validate_installed_wheel.py
```

## Scope boundary

The public package owns generic state durability, retry policy, report contracts,
continuation control, fail-closed Git object publication, and tamper-evident
execution/evidence records. A consuming project owns its workflow state machine,
checkpoint creation, changed-file selection, quality gates, repository policy,
direct-dependency declarations, trust-anchor retention, and credentials.
