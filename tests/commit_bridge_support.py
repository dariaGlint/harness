from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Sequence

from production_harness.commit_bridge import (
    CheckpointValidationError,
    CommitBridgeResult,
    CommitInfo,
    ComparedFile,
    HandoffValidationError,
    PullRequestInfo,
    RemotePath,
    RepositoryInfo,
    RepositoryStateError,
    TreeEntry,
    commit_checkpoint_to_github,
    git_blob_sha,
    load_handoff_manifest,
    prepare_checkpoint_blobs,
)
from production_harness.github_rest import GitHubRestClient, GitHubRestConfig

BASE = "1" * 40
LATEST = "2" * 40
BASE_TREE = "3" * 40
NEW_TREE = "4" * 40
NEW_COMMIT = "5" * 40


def handoff_bytes(
    files: list[dict],
    *,
    repository: str = "owner/repo",
    base_sha: str = BASE,
    root: str = "workspace",
    dependencies: list[str] | None = None,
) -> bytes:
    payload = {
        "issue_number": 7,
        "commit_bridge": {
            "schema_version": 1,
            "repository": repository,
            "base_sha": base_sha,
            "workspace_root": root,
            "direct_dependencies": dependencies or [],
            "files": files,
        },
    }
    return json.dumps(payload).encode("utf-8")


def file_spec(path: str, operation: str, data: bytes, mode: str = "100644") -> dict:
    return {
        "path": path,
        "operation": operation,
        "mode": mode,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "git_blob_sha": git_blob_sha(data),
    }


def checkpoint_bytes(entries: dict[str, bytes]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in entries.items():
            archive.writestr(path, data)
    return target.getvalue()


class FakeClient:
    def __init__(self) -> None:
        self.default_branch = "main"
        self.branch_heads = {"main": BASE}
        self.commits = {
            BASE: CommitInfo(BASE, BASE_TREE, (), "base"),
            LATEST: CommitInfo(LATEST, BASE_TREE, (BASE,), "latest"),
        }
        self.paths: dict[tuple[str, str], RemotePath | None] = {}
        self.created_entries: Sequence[TreeEntry] = ()
        self.created_message = ""
        self.compare_files: Sequence[ComparedFile] = ()
        self.pr = PullRequestInfo(11, "https://example.test/pr/11")
        self.create_commit_calls = 0
        self.uploaded_data: list[bytes] = []

    def get_repository(self, repository: str) -> RepositoryInfo:
        return RepositoryInfo(self.default_branch)

    def get_branch_head(self, repository: str, branch: str) -> str | None:
        return self.branch_heads.get(branch)

    def get_commit(self, repository: str, commit_sha: str) -> CommitInfo:
        return self.commits[commit_sha]

    def get_path(self, repository: str, path: str, ref: str) -> RemotePath | None:
        return self.paths.get((ref, path))

    def create_blob(self, repository: str, data: bytes) -> str:
        self.uploaded_data.append(data)
        return git_blob_sha(data)

    def create_tree(
        self, repository: str, base_tree_sha: str, entries: Sequence[TreeEntry]
    ) -> str:
        self.assert_base_tree = base_tree_sha
        self.created_entries = entries
        return NEW_TREE

    def create_commit(
        self, repository: str, message: str, tree_sha: str, parent_sha: str
    ) -> str:
        self.create_commit_calls += 1
        self.created_message = message
        self.commits[NEW_COMMIT] = CommitInfo(NEW_COMMIT, tree_sha, (parent_sha,), message)
        return NEW_COMMIT

    def compare(self, repository: str, base: str, head: str) -> Sequence[ComparedFile]:
        return self.compare_files

    def create_branch(self, repository: str, branch: str, commit_sha: str) -> None:
        self.branch_heads[branch] = commit_sha

    def update_branch(self, repository: str, branch: str, commit_sha: str) -> None:
        self.branch_heads[branch] = commit_sha

    def find_open_pull_request(
        self, repository: str, head_branch: str, base_branch: str
    ) -> PullRequestInfo | None:
        return None

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
        self.pr_request = (head_branch, base_branch, title, body, draft)
        return self.pr
