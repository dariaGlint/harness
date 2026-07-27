# Workspace Commit Bridge

`Workspace Commit Bridge` accepts a checkpoint ZIP, `handoff.json`, and optional
repository policy as filesystem paths or binary file objects. The caller never
passes selected file contents or Base64 strings. The bridge reads each approved
ZIP member in-process and sends bytes directly to GitHub's Git object API.

## Publication boundary

The transaction is fail-closed:

1. validate request, repository policy, handoff, and ZIP structure;
2. reconcile the ZIP file set exactly with the handoff;
3. verify byte size, SHA-256, Git blob SHA, mode, encoding, and operation;
4. read the current default branch and inspect selected paths plus direct dependencies;
5. invoke configured repository admission hooks;
6. create and verify blobs;
7. refresh the default branch, create one tree, and verify every tree entry;
8. create one single-parent commit and verify its tree, parent, and message;
9. execute one final compare and require the exact paths and statuses;
10. create or fast-forward the dedicated branch;
11. optionally create a Draft pull request.

A failure before branch update leaves no implementation branch or PR. Git blob,
tree, or commit objects created before a later rejection are unreachable objects;
they do not modify repository refs.

## Mandatory rejections

Mandatory controls cannot be disabled by policy. They reject default-branch
updates, unauthorized branch prefixes, stale-base conflicts, unexpected/missing
ZIP files, traversal, absolute paths, symlinks, encrypted or duplicate members,
case collisions, unintended empty files, corrupt UTF-8, hash mismatches, forbidden
artifacts, truncated compare results, incomplete trees/commits, and unexpected or
missing final diff paths.

Common media, archives, logs, `.godot/`, `captures/`, `validation_output/`, and
`movie_frames/` are forbidden by default. PNG/JPG files are accepted only under
repository-approved asset patterns and are still rejected when their path looks
like preview, comparison, diff, capture, or screenshot evidence.

## Handoff contract

The top-level object preserves orchestration fields. A strict nested
`commit_bridge` section supplies publication metadata.

```json
{
  "issue_number": 373,
  "base_master_sha": "<40 lowercase hex>",
  "repository": "dariaGlint/Chaos",
  "changed_files": [
    {
      "path": "scripts/example.gd",
      "sha256": "<64 lowercase hex>",
      "purpose": "Change purpose"
    }
  ],
  "behavior_change": false,
  "validation_results": [],
  "preview_approved": false,
  "next_action": "commit",
  "commit_bridge": {
    "schema_version": 1,
    "repository": "dariaGlint/Chaos",
    "base_sha": "<40 lowercase hex>",
    "workspace_root": "project_files",
    "direct_dependencies": ["scripts/direct_dependency.gd"],
    "files": [
      {
        "path": "scripts/example.gd",
        "operation": "modify",
        "mode": "100644",
        "size_bytes": 1200,
        "sha256": "<64 lowercase hex>",
        "git_blob_sha": "<40 lowercase hex>",
        "purpose": "Change purpose",
        "encoding": "utf-8"
      }
    ]
  }
}
```

Delete entries contain only `path`, `operation: "delete"`, and optional
`purpose`; their paths must be absent from the ZIP. `changed_files` and
`commit_bridge.files` must name exactly the same paths.

Player-experience changes require `behavior_change: true` and
`preview_approved: true`. A repository policy may require additional top-level
fields, such as Issue Work Claim and canonical publication evidence.

Packaged schemas:

- `workspace_commit_handoff_v1.json`
- `workspace_commit_policy_v1.json`

## Repository policy

A policy can bind repository-specific path rules and call existing gates instead
of duplicating them:

```json
{
  "schema_version": 1,
  "repository": "dariaGlint/Chaos",
  "allowed_branch_prefixes": ["agent/"],
  "allowed_asset_patterns": ["assets/**", "models/**", "textures/**"],
  "admission": {
    "search_path": ".",
    "issue_claim_factory": "package.claims:manager_from_environment",
    "commit_message_callable": "package.publication:commit_message_for_transaction",
    "required_handoff_fields": [
      "task_id",
      "controller_run_id",
      "transaction_id",
      "validation_ownership"
    ]
  }
}
```

Admission modules are loaded from the policy repository path. Credentials remain
in the execution environment and are not serialized into results.

## Python API

```python
from production_harness import commit_checkpoint_to_github

result = commit_checkpoint_to_github(
    repository="dariaGlint/Chaos",
    base_sha="<latest-master-sha>",
    branch_name="agent/issue-373-anomalous-wind",
    commit_message="Implement anomalous wind normalization stage",
    checkpoint_zip="/path/to/checkpoint.zip",
    handoff_json="/path/to/handoff.json",
    policy_json="/path/to/.commit-bridge-policy.json",
    create_pr=False,
)
print(result.to_dict())
```

The default adapter reads a short-lived App installation token from
`WORKSPACE_COMMIT_BRIDGE_TOKEN`, falling back to `GITHUB_TOKEN`. It supports an
HTTPS `GITHUB_API_URL`. Tokens are excluded from dataclass representations and
result JSON.

## CLI

```bash
python -m production_harness.commit_bridge_cli publish \
  --repository dariaGlint/Chaos \
  --base-sha <latest-master-sha> \
  --branch agent/issue-373-anomalous-wind \
  --message "Implement anomalous wind normalization stage" \
  --checkpoint /path/to/checkpoint.zip \
  --handoff /path/to/handoff.json \
  --policy /path/to/.commit-bridge-policy.json \
  --no-create-pr
```

The CLI emits one JSON object. Exit code `0` means success; `10` means a
structured rejection. It does not use local Git, GitHub Actions, `gh`,
`create_file`, or `update_file`.
