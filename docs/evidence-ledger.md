# Evidence Ledger

The Evidence Ledger is a dependency-free, append-only record for execution and
external evidence. It replaces unverified completion prose with a canonical event
sequence that can be checked independently.

## Event model

Each JSONL line contains exactly one event:

```json
{
  "actor": "independent-verifier",
  "event_hash": "<64 lowercase hexadecimal characters>",
  "event_type": "task.verified",
  "evidence": [
    {
      "path": "reports/final.json",
      "role": "verification-report",
      "sha256": "<64 lowercase hexadecimal characters>",
      "size_bytes": 1234
    }
  ],
  "payload": {"result": "PASS"},
  "previous_hash": "<previous event hash or null>",
  "schema_version": 1,
  "sequence": 2,
  "subject_id": "task-123",
  "timestamp": "2026-07-27T07:30:00.000000Z"
}
```

The file uses canonical, compact, sorted-key UTF-8 JSON and a final newline after
every event. Hand-edited, pretty-printed, blank, non-object, non-finite, oversized,
or partially written records are rejected.

## Append

```python
from production_harness import append_ledger_event

result = append_ledger_event(
    "validation_output/evidence-ledger.jsonl",
    event_type="task.started",
    subject_id="task-123",
    actor="runner",
    payload={"step": 1},
    expected_sequence=1,
)
print(result.event_hash)
```

A writer acquires `<ledger>.lock`, verifies the current chain, checks optional
stale-writer anchors, appends and fsyncs one event, then verifies the new head. It
never silently removes an existing lock. After an interrupted writer is proven
dead, lock removal is an explicit operator action.

## Evidence references

```python
from production_harness import create_evidence_reference

reference = create_evidence_reference(
    "validation_output/evidence",
    "reports/final.json",
    role="verification-report",
)
```

References are file-based. The API binds a role, normalized relative path, size,
and SHA-256 without placing file contents in the ledger. The evidence root and all
path components must be real directories/files rather than symlinks. Verification
rehashes the regular file and rejects missing, replaced, resized, or modified
content.

## Trusted head anchors

A hash chain cannot detect a complete trailing-record deletion when no external
party remembers the old head. It also cannot prevent an actor that can rewrite the
whole ledger from recomputing every hash. Retain a trusted anchor outside the
ledger's mutable storage:

```python
from production_harness import (
    verify_ledger_against_snapshot,
    write_ledger_snapshot,
)

write_ledger_snapshot(
    "validation_output/evidence-ledger.jsonl",
    "trusted/evidence-ledger-head.json",
)
verify_ledger_against_snapshot(
    "validation_output/evidence-ledger.jsonl",
    "trusted/evidence-ledger-head.json",
)
```

The snapshot contains the expected event count, last hash, and subject. It becomes
a useful trust anchor only when retained in a separately trusted report,
attestation, artifact store, commit, or verifier system. A colocated file with the
same write permissions is operationally useful but not an independent signature.

## CLI

```bash
evidence-ledger append \
  --ledger validation_output/evidence-ledger.jsonl \
  --event-type task.completed \
  --subject-id task-123 \
  --payload-json payload.json \
  --evidence-root validation_output/evidence \
  --evidence verification-report=reports/final.json \
  --expected-sequence 1 \
  --snapshot trusted/evidence-ledger-head.json

evidence-ledger verify \
  --ledger validation_output/evidence-ledger.jsonl \
  --snapshot trusted/evidence-ledger-head.json \
  --evidence-root validation_output/evidence \
  --require-event task.completed
```

Successful commands emit JSON and return `0`. Rejected ledgers, evidence, input,
or locks emit a structured error to stderr and return `10`.

## Security boundary

The ledger is tamper-evident, not an identity signature or authorization system.
It does not decide whether a task passed, whether an actor is trusted, or whether a
project-specific Gate is sufficient. Consumers own event vocabulary, actor trust,
anchor retention, evidence retention, and acceptance policy.
