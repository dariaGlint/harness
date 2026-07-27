# Workspace Commit Bridge

`Workspace Commit Bridge` publishes a checkpoint ZIP without placing source file
contents in a chat message or connector tool argument. It validates the local
archive and `handoff.json`, sends approved bytes directly from the bridge process
to GitHub's Git object API, creates one tree and one commit, verifies the diff,
and only then updates the branch.

## Guarantees

- the request repository and base SHA must exactly match `handoff.json`;
- only paths declared under `commit_bridge.files` are read from the ZIP;
- SHA-256, Git blob SHA, size, operation, and mode are verified before writes;
- ZIP traversal, absolute paths, symlinks, encryption, duplicate paths,
  case-collisions, excessive expansion, and configured artifact paths fail closed;
- a stale default branch is accepted only when selected paths and declared direct
  dependencies are unchanged between the requested and latest base;
- `add`, `modify`, and `delete` operations are checked against the effective base;
- all approved paths are represented in one Git tree and one single-parent commit;
- the compare result must contain exactly the declared paths and statuses;
- the default branch is never updated, existing unrelated branches are never
  overwritten, and PR creation is Draft-only;
- reruns reuse an already-published branch commit when tree, parent, and message
  all match.

## Handoff contract

`handoff.json` may retain human-oriented orchestration fields. The bridge reads
only the nested machine contract:

```json
{
  "issue_number": 123,
  "commit_bridge": {
    "schema_version": 1,
    "repository": "dariaGlint/Chaos",
    "base_sha": "0123456789abcdef0123456789abcdef01234567",
    "workspace_root": "Chaos",
    "direct_dependencies": ["tools/shared.py"],
    "files": [
      {
        "path": "tools/example.py",
        "operation": "modify",
        "mode": "100755",
        "size_bytes": 1200,
        "sha256": "<64 lowercase hex characters>",
        "git_blob_sha": "<40 lowercase hex characters>"
      },
      {
        "path": "docs/obsolete.md",
        "operation": "delete"
      }
    ]
  }
}
```

`workspace_root` is the optional ZIP directory containing repository-relative
paths. Delete entries must be absent from that payload directory. Extra ZIP
entries are ignored and never committed, but all member names are still checked
for traversal, duplicate, and case-collision hazards.

The packaged JSON Schema is
`production_harness/schemas/workspace_commit_handoff_v1.json`.

## Library API

```python
from production_harness import commit_checkpoint_to_github

result = commit_checkpoint_to_github(
    repository="dariaGlint/Chaos",
    base_sha="0123456789abcdef0123456789abcdef01234567",
    branch_name="agent/example-task",
    commit_message="Add example task",
    checkpoint_zip="checkpoint.zip",
    handoff_json="handoff.json",
    create_pr=True,
)
print(result.commit_sha)
```

The default adapter uses `WORKSPACE_COMMIT_BRIDGE_TOKEN`, falling back to
`GITHUB_TOKEN`. Use a short-lived GitHub App installation token. Required
permissions are **Contents: write** and, when `create_pr=True`,
**Pull requests: write**. The adapter also supports GitHub Enterprise through
`GITHUB_API_URL`.

## CLI

```bash
workspace-commit-bridge \
  --repository dariaGlint/Chaos \
  --base-sha 0123456789abcdef0123456789abcdef01234567 \
  --branch-name agent/example-task \
  --commit-message "Add example task" \
  --checkpoint-zip checkpoint.zip \
  --handoff-json handoff.json \
  --create-pr
```

The command returns JSON only after branch verification. A failure returns exit
code `2` and does not force-update a branch.

## Scope boundary

This package does not obtain GitHub App credentials, create checkpoint archives,
select changed files, or decide project-specific direct dependencies. The caller
owns those responsibilities. The bridge does not use `git clone`, `git fetch`,
`git push`, or the `gh` CLI.
