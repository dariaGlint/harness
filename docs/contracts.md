# v0.1 contracts

This document defines the compatibility boundary that must be stable before a
consumer repository pins this package.

## Public Python API

The supported import surface is the `production_harness.__all__` list. Modules,
functions, and constants not exported there are internal and may change during
v0.x.

Within v0.1 patch releases:

- existing exported call signatures will not remove parameters;
- report schema version `1` remains readable;
- Workspace Commit Bridge handoff schema version `1` remains readable;
- exit-code defaults remain unchanged;
- new optional fields and APIs may be added;
- security and fail-closed corrections may reject state previously accepted by
  mistake.

## Task state envelope

The harness does not own a consumer's full state machine. It requires only a
minimal durable envelope by default:

```json
{
  "machine_state": "WORK_REMAINS"
}
```

Requirements:

- the state file must decode as one JSON object;
- `machine_state` must be a non-empty string;
- a custom `state_machine_key` may replace the default key;
- a consumer may provide `state_validator` for stronger validation;
- corrupt, non-object, missing-key, empty-state, or validator-rejected files are
  not considered resumable durable state.

The packaged `task-state-envelope-v1.schema.json` describes the default minimal
envelope. Additional consumer fields are allowed.

## Foreground report

`foreground-supervisor.json` conforms to packaged schema
`foreground-report-v1.schema.json` and declares `schema_version: 1`.

Reports intentionally contain only a SHA-256 digest and argument count for the
command template. Raw command arguments are not persisted because they may
contain credentials or private paths.

`ForegroundRequest.next_command`, when supplied, is persisted verbatim on a
yielded report. Consumers must pass an already redacted command containing no
secrets.

## Workspace commit handoff

The supported machine contract is the `commit_bridge` object in `handoff.json`.
It declares `schema_version: 1`, repository, base commit, optional ZIP workspace
root, optional direct dependencies, and an exact `files` array.

For `add` and `modify`, path, mode, byte size, SHA-256, and Git blob SHA are
required. For `delete`, content fields are forbidden and the payload path must be
absent from the ZIP. The request repository and base SHA must match the handoff.

The bridge may move from the requested base to the latest default-branch commit
only when each selected path and direct dependency resolves to the same remote
object at both commits. The generated compare result must exactly match declared
paths and operation statuses before the branch is changed.

The packaged `workspace_commit_handoff_v1.json` schema describes the structural
contract. Runtime validation adds path normalization, Unicode, ZIP safety,
resource-limit, digest, remote-state, and branch-safety checks.

## Timeout behavior

A child timeout is its own event and is not interpreted through a consumer's
exit-code set. The harness resumes only when a valid durable state envelope
exists. Otherwise it returns `EXIT_UNRECOVERABLE`.

## Platform behavior

On POSIX, children run in a new session and the process group receives SIGTERM,
then SIGKILL after the grace period. On Windows, children run in a new process
group and receive CTRL_BREAK when available, followed by terminate/kill fallback.
