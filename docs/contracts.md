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

## Workspace Commit Bridge contracts

The bridge accepts checkpoint, handoff, and policy **file references**. Source
bytes are not part of the public call signature or structured result.

Handoff schema version `1` keeps orchestration fields at the top level and uses a
strict `commit_bridge` section for repository, base SHA, ZIP workspace root,
direct dependencies, and exact add/modify/delete entries. `changed_files` and the
machine file list must contain the same normalized paths. Add/modify entries bind
mode, byte size, SHA-256, Git blob SHA, encoding, and explicit empty-file intent.

Repository policy schema version `1` may tighten branch, artifact, asset, handoff,
and admission rules. Mandatory controls cannot be disabled. Admission hooks are
loaded from the policy repository and may call the consumer's existing Issue Work
Claim and canonical publication gates.

A stale requested base may move to the latest default-branch commit only when the
requested base is an ancestor and all selected paths plus declared direct
dependencies are unchanged. The bridge creates one tree and one single-parent
commit, verifies both objects, then performs one final exact compare before
changing the implementation branch.

The packaged `workspace_commit_handoff_v1.json` and
`workspace_commit_policy_v1.json` schemas describe structural contracts. Runtime
validation additionally enforces normalized paths, ZIP safety, resource limits,
repository state, admission hooks, Draft-only PRs, and secret-free structured
results.

## Timeout behavior

A child timeout is its own event and is not interpreted through a consumer's
exit-code set. The harness resumes only when a valid durable state envelope
exists. Otherwise it returns `EXIT_UNRECOVERABLE`.

## Platform behavior

On POSIX, children run in a new session and the process group receives SIGTERM,
then SIGKILL after the grace period. On Windows, children run in a new process
group and receive CTRL_BREAK when available, followed by terminate/kill fallback.
