from __future__ import annotations

import hashlib
import io
import json
import sys
import types
import zipfile
from pathlib import Path
from typing import Sequence

# This local worktree is intentionally a partial overlay. Bypass package __init__
# so focused bridge tests do not require unrelated foreground modules locally.
PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "production_harness"
if "production_harness" not in sys.modules:
    package = types.ModuleType("production_harness")
    package.__path__ = [str(PACKAGE_ROOT)]
    package.__package__ = "production_harness"
    sys.modules["production_harness"] = package

from production_harness.commit_bridge import (  # noqa: E402
    BridgeLimits,
    CommitBridgeResult,
    CommitInfo,
    CompareResult,
    ComparedFile,
    PullRequestInfo,
    RemotePath,
    RepositoryInfo,
    TreeEntry,
    commit_checkpoint_to_github,
    git_blob_sha,
    load_commit_bridge_policy,
    load_handoff_manifest,
    prepare_checkpoint_blobs,
)
from production_harness.github_rest import GitHubRestClient, GitHubRestConfig  # noqa: E402

BASE = "1" * 40
LATEST = "2" * 40
BASE_TREE = "3" * 40
NEW_TREE = "4" * 40
NEW_COMMIT = "5" * 40


def file_spec(
    path: str,
    operation: str,
    data: bytes | None = None,
    *,
    mode: str = "100644",
    purpose: str = "test change",
    encoding: str | None = None,
    allow_empty: bool = False,
) -> dict:
    result = {"path": path, "operation": operation, "purpose": purpose}
    if operation == "delete":
        return result
    assert data is not None
    result.update(
        {
            "mode": mode,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "git_blob_sha": git_blob_sha(data),
            "allow_empty": allow_empty,
        }
    )
    if encoding is not None:
        result["encoding"] = encoding
    return result


def handoff_payload(
    files: list[dict],
    *,
    repository: str = "owner/repo",
    base_sha: str = BASE,
    root: str = "workspace",
    dependencies: list[str] | None = None,
    behavior_change: bool = False,
    preview_approved: bool = False,
    extra_top: dict | None = None,
) -> dict:
    changed_files = []
    for item in files:
        changed = {"path": item["path"], "purpose": item.get("purpose", "test change")}
        if item.get("sha256") is not None:
            changed["sha256"] = item["sha256"]
        changed_files.append(changed)
    payload = {
        "issue_number": 7,
        "base_master_sha": base_sha,
        "repository": repository,
        "changed_files": changed_files,
        "behavior_change": behavior_change,
        "validation_results": [],
        "preview_approved": preview_approved,
        "next_action": "commit",
        "commit_bridge": {
            "schema_version": 1,
            "repository": repository,
            "base_sha": base_sha,
            "workspace_root": root,
            "direct_dependencies": dependencies or [],
            "files": files,
        },
    }
    if extra_top:
        payload.update(extra_top)
    return payload


def handoff_bytes(files: list[dict], **kwargs) -> bytes:
    return json.dumps(handoff_payload(files, **kwargs), ensure_ascii=False).encode("utf-8")


def checkpoint_bytes(entries: Sequence[tuple[str, bytes]] | dict[str, bytes]) -> bytes:
    pairs = list(entries.items()) if isinstance(entries, dict) else list(entries)
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in pairs:
            archive.writestr(path, data)
    return target.getvalue()


def symlink_checkpoint(path: str, target: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        info = zipfile.ZipInfo(path)
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, target)
    return output.getvalue()


def success_compare(files: list[dict]) -> CompareResult:
    statuses = {"add": "added", "modify": "modified", "delete": "removed"}
    return CompareResult(
        status="ahead",
        ahead_by=1,
        behind_by=0,
        files=tuple(ComparedFile(item["path"], statuses[item["operation"]]) for item in files),
    )


class FakeClient:
    def __init__(self) -> None:
        self.default_branch = "main"
        self.branch_heads: dict[str, str] = {"main": BASE}
        self.commits: dict[str, CommitInfo] = {
            BASE: CommitInfo(BASE, BASE_TREE, (), "base"),
            LATEST: CommitInfo(LATEST, BASE_TREE, (BASE,), "latest"),
        }
        self.paths: dict[tuple[str, str], RemotePath | None] = {}
        self.tree_paths: dict[tuple[str, str], RemotePath | None] = {}
        self.compare_result = CompareResult("ahead", 1, 0, ())
        self.pr = PullRequestInfo(11, "https://example.test/pr/11", True)
        self.calls: list[str] = []
        self.uploaded_data: list[bytes] = []
        self.created_entries: tuple[TreeEntry, ...] = ()
        self.created_message = ""
        self.blob_override: str | None = None
        self.tree_sha = NEW_TREE
        self.commit_sha = NEW_COMMIT
        self.commit_override: CommitInfo | None = None
        self.open_pr: PullRequestInfo | None = None

    def get_repository(self, repository: str) -> RepositoryInfo:
        self.calls.append("get_repository")
        return RepositoryInfo(self.default_branch)

    def get_branch_head(self, repository: str, branch: str) -> str | None:
        self.calls.append(f"get_branch_head:{branch}")
        return self.branch_heads.get(branch)

    def get_commit(self, repository: str, commit_sha: str) -> CommitInfo:
        self.calls.append(f"get_commit:{commit_sha}")
        if commit_sha == self.commit_sha and self.commit_override is not None:
            return self.commit_override
        return self.commits[commit_sha]

    def get_path(self, repository: str, path: str, ref: str) -> RemotePath | None:
        self.calls.append(f"get_path:{ref}:{path}")
        return self.paths.get((ref, path))

    def get_tree_path(self, repository: str, path: str, tree_sha: str) -> RemotePath | None:
        self.calls.append(f"get_tree_path:{path}")
        return self.tree_paths.get((tree_sha, path))

    def create_blob(self, repository: str, data: bytes) -> str:
        self.calls.append("create_blob")
        self.uploaded_data.append(data)
        return self.blob_override or git_blob_sha(data)

    def create_tree(
        self, repository: str, base_tree_sha: str, entries: Sequence[TreeEntry]
    ) -> str:
        self.calls.append("create_tree")
        self.created_entries = tuple(entries)
        for entry in entries:
            self.tree_paths[(self.tree_sha, entry.path)] = (
                None
                if entry.sha is None
                else RemotePath(entry.path, "file", entry.sha, entry.mode)
            )
        return self.tree_sha

    def create_commit(
        self, repository: str, message: str, tree_sha: str, parent_sha: str
    ) -> str:
        self.calls.append("create_commit")
        self.created_message = message
        self.commits[self.commit_sha] = CommitInfo(
            self.commit_sha, tree_sha, (parent_sha,), message
        )
        return self.commit_sha

    def compare_commits(self, repository: str, base: str, head: str) -> CompareResult:
        self.calls.append("compare_commits")
        return self.compare_result

    def create_branch(self, repository: str, branch: str, commit_sha: str) -> None:
        self.calls.append("create_branch")
        self.branch_heads[branch] = commit_sha

    def update_branch(self, repository: str, branch: str, commit_sha: str) -> None:
        self.calls.append("update_branch")
        self.branch_heads[branch] = commit_sha

    def find_open_pull_request(
        self, repository: str, head_branch: str, base_branch: str
    ) -> PullRequestInfo | None:
        self.calls.append("find_open_pull_request")
        return self.open_pr

    def create_pull_request(
        self,
        repository: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        *,
        draft: bool,
    ) -> PullRequestInfo:
        self.calls.append("create_pull_request")
        self.pr_request = (head_branch, base_branch, title, body, draft)
        return self.pr


def publish_one(
    data: bytes = b"hello\n",
    *,
    path: str = "scripts/example.gd",
    client: FakeClient | None = None,
    branch: str = "agent/bridge-test",
    create_pr: bool = False,
    behavior_change: bool = False,
    preview_approved: bool = False,
    checkpoint_extra: dict[str, bytes] | None = None,
    policy_json=None,
    extra_top: dict | None = None,
) -> tuple[CommitBridgeResult, FakeClient]:
    client = client or FakeClient()
    files = [file_spec(path, "add", data)]
    client.compare_result = success_compare(files)
    entries = {f"workspace/{path}": data}
    entries.update(checkpoint_extra or {})
    result = commit_checkpoint_to_github(
        "owner/repo",
        BASE,
        branch,
        "Publish checkpoint",
        io.BytesIO(checkpoint_bytes(entries)),
        io.BytesIO(
            handoff_bytes(
                files,
                behavior_change=behavior_change,
                preview_approved=preview_approved,
                extra_top=extra_top,
            )
        ),
        create_pr=create_pr,
        client=client,
        policy_json=policy_json,
    )
    return result, client
