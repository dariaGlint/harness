# Operational Acceptance Contract

The Operational Acceptance Contract converts independently produced gate results
into one deterministic machine verdict. It is deliberately smaller than a
project orchestrator: it does not execute gates, infer which gates a project
needs, or decide whether a repository may publish a change.

## Why the contract exists

A workflow can run every command successfully and still be operationally
incomplete. Common failure modes include:

- presenting task success when required workflow checks were skipped;
- accepting a PASS result that contains no required evidence;
- mixing results from another task or another contract revision;
- silently ignoring a missing, duplicate, or unexpected gate;
- treating a fail-closed blocked outcome as harness malfunction;
- embedding one project's Stage, preview, CI, or publication rules in reusable
  orchestration code.

The contract separates two questions:

1. **Conformance:** were the contract and supplied results validly interpreted?
2. **Task verdict:** did the declared gates pass, fail, or remain blocked?

A valid fail-closed `BLOCKED` result is conformant. Malformed or contradictory
input is rejected and does not produce a report.

## Contract model

```python
from production_harness import AcceptanceGate, build_acceptance_contract

contract = build_acceptance_contract(
    subject_id="task-123",
    gates=[
        AcceptanceGate(
            "compile",
            required=True,
            required_evidence_roles=("compile-report",),
        ),
        AcceptanceGate("docs", required=False),
        AcceptanceGate("tests", required=True),
    ],
    failed_optional="fail",
)
```

Gate definitions are sorted by `gate_id` before the contract digest is computed.
Each gate declares:

- whether it is required;
- which statuses it allows;
- which evidence roles must appear in its result;
- an optional human-readable description.

The v1 verdict policy is explicit and deliberately narrow:

| Condition | Verdict effect |
| --- | --- |
| required gate reports `fail` | `FAIL` |
| optional gate reports `fail` | `FAIL` or ignored, as selected by `failed_optional` |
| required gate is missing | `BLOCKED` |
| required gate reports `blocked` | `BLOCKED` |
| required gate reports `skipped` | `BLOCKED` |
| optional gate is missing, blocked, or skipped | no verdict effect |

A required gate must allow `pass`. Project-specific semantics such as preview
approval, required CI names, or Stage order belong in the consumer's gate list,
not in this package.

## Gate results

```python
from production_harness import build_gate_result

result = build_gate_result(
    contract,
    gate_id="tests",
    status="pass",
    reason="focused tests passed",
)
```

Every result binds:

- schema and kind;
- contract SHA-256;
- subject ID;
- gate ID;
- status and reason;
- sorted external evidence references;
- its own SHA-256 digest.

A result is rejected when its contract, subject, gate, status, digest, or evidence
shape does not match the contract. Duplicate and unexpected results are rejected.

## Evidence verification

Gate evidence uses the existing Evidence Ledger `EvidenceReference` model. The
contract requires evidence by **role**, while each result binds role, normalized
relative path, exact byte size, and SHA-256.

```python
from production_harness import create_evidence_reference

reference = create_evidence_reference(
    "validation_output/evidence",
    "compile/report.json",
    role="compile-report",
)
```

Passing `evidence_root` to evaluation reopens and rehashes every referenced file.
Missing, changed, symlinked, unsafe, or non-regular evidence fails closed. Required
evidence roles are enforced even when file re-verification is not requested.

## Evaluation and report

```python
from production_harness import evaluate_acceptance

report = evaluate_acceptance(
    contract,
    [compile_result, tests_result],
    evidence_root="validation_output/evidence",
)
assert report.conformance_status == "pass"
assert report.task_verdict == "PASS"
```

The report normalizes results into contract gate order and records:

- separate conformance status and task verdict;
- missing required gates;
- failed gates that affect the verdict;
- blocked required gates;
- skipped required gates;
- the contract digest and report digest.

Supplying the original contract to `verify_acceptance_report` recomputes the
verdict and requires the entire report to match contract semantics.

## CLI

Build a contract from a JSON array of complete gate definitions:

```bash
operational-acceptance build-contract \
  --subject-id task-123 \
  --gates-json gates.json \
  --failed-optional fail \
  --output acceptance-contract.json
```

Evaluate zero or more result files:

```bash
operational-acceptance evaluate \
  --contract acceptance-contract.json \
  --result compile-result.json \
  --result tests-result.json \
  --evidence-root validation_output/evidence \
  --output acceptance-report.json
```

Successful commands emit JSON and return `0`. Invalid contracts, results,
evidence, or reports emit a structured rejection to stderr and return `10`.

## Security and ownership boundary

The contract is digest-bound and fail-closed, but it is not an identity
signature, authorization system, or execution sandbox. It does not establish
that an actor is trustworthy or that a gate command was appropriate. Consumers
own:

- gate vocabulary and required gate selection;
- gate execution and provider isolation;
- evidence retention and trust-anchor policy;
- project classifications, preview rules, CI policy, and publication policy;
- authorization and identity.

Packaged schemas:

- `operational-acceptance-contract-v1.schema.json`
- `operational-acceptance-gate-result-v1.schema.json`
- `operational-acceptance-report-v1.schema.json`
