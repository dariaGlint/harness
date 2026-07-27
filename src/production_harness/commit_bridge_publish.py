"""Verified GitHub publication for Workspace Commit Bridge."""
from __future__ import annotations

from typing import Sequence

from .commit_bridge_archive import prepare_checkpoint_blobs
from .commit_bridge_handoff import (
    _validate_branch, _validate_commit_message, _validate_repository, _validate_sha,
    load_handoff_manifest,
)
from .commit_bridge_types import (
    BridgeLimits, CommitBridgeResult, CommitInfo, ComparedFile, FileInput,
    GitHubCommitClient, GitHubPublicationError, HandoffFile, HandoffManifest,
    HandoffValidationError, RepositoryStateError, TreeEntry,
)

def _remote_path_changed(
    client: GitHubCommitClient,
    repository: str,
    path: str,
    old_ref: str,
    new_ref: str,
) -> bool:
    old = client.get_path(repository, path, old_ref)
    new = client.get_path(repository, path, new_ref)
    if old is None or new is None:
        return old != new
    return (old.object_type, old.sha, old.mode) != (new.object_type, new.sha, new.mode)


def _validate_declared_operations(
    client: GitHubCommitClient,
    repository: str,
    base_sha: str,
    files: Sequence[HandoffFile],
) -> None:
    for item in files:
        existing = client.get_path(repository, item.path, base_sha)
        if existing is not None and existing.object_type not in {"file", "symlink"}:
            raise RepositoryStateError(
                f"repository path is not a file-like object at base: {item.path} ({existing.object_type})"
            )
        if item.operation == "add" and existing is not None:
            raise RepositoryStateError(f"add path already exists at base: {item.path}")
        if item.operation in {"modify", "delete"} and existing is None:
            raise RepositoryStateError(f"{item.operation} path does not exist at base: {item.path}")


def _expected_status(operation: str) -> str:
    return {"add": "added", "modify": "modified", "delete": "removed"}[operation]


def _verify_compare(files: Sequence[ComparedFile], expected: Sequence[HandoffFile]) -> tuple[str, ...]:
    actual = {item.path: item.status for item in files}
    expected_map = {item.path: _expected_status(item.operation) for item in expected}
    if actual != expected_map:
        missing = sorted(set(expected_map) - set(actual))
        unexpected = sorted(set(actual) - set(expected_map))
        wrong_status = {
            path: {"expected": expected_map[path], "actual": actual[path]}
            for path in sorted(set(actual) & set(expected_map))
            if actual[path] != expected_map[path]
        }
        raise GitHubPublicationError(
            "published diff mismatch: "
            f"missing={missing}, unexpected={unexpected}, wrong_status={wrong_status}"
        )
    return tuple(sorted(actual))


def _pr_body(manifest: HandoffManifest, result_paths: Sequence[str]) -> str:
    lines = [
        "## Workspace Commit Bridge",
        "",
        "Published from a validated checkpoint ZIP and `handoff.json`.",
        "",
        "### Changed files",
        "",
    ]
    lines.extend(f"- `{path}`" for path in result_paths)
    if manifest.issue_number is not None:
        lines.extend(("", f"Closes #{manifest.issue_number}"))
    return "\n".join(lines)


def commit_checkpoint_to_github(
    repository: str,
    base_sha: str,
    branch_name: str,
    commit_message: str,
    checkpoint_zip: FileInput,
    handoff_json: FileInput,
    create_pr: bool = False,
    *,
    client: GitHubCommitClient | None = None,
    limits: BridgeLimits = BridgeLimits(),
) -> CommitBridgeResult:
    """Validate and publish approved checkpoint files as one tree and one commit.

    The default client uses a GitHub App installation token from the environment.
    All local content stays inside this process; only Git object API requests carry
    file bytes to GitHub.
    """
    repository = _validate_repository(repository)
    requested_base_sha = _validate_sha(base_sha, "base_sha")
    branch_name = _validate_branch(branch_name)
    commit_message = _validate_commit_message(commit_message)
    manifest = load_handoff_manifest(handoff_json, limits=limits)
    if manifest.repository != repository:
        raise HandoffValidationError(
            f"repository mismatch: request={repository}, handoff={manifest.repository}"
        )
    if manifest.base_sha != requested_base_sha:
        raise HandoffValidationError(
            f"base_sha mismatch: request={requested_base_sha}, handoff={manifest.base_sha}"
        )
    prepared_blobs, ignored_entries = prepare_checkpoint_blobs(
        checkpoint_zip,
        manifest,
        limits=limits,
    )
    if client is None:
        from .github_rest import GitHubRestClient

        client = GitHubRestClient.from_environment()
    if not isinstance(client, GitHubCommitClient):
        raise TypeError("client does not implement GitHubCommitClient")

    repository_info = client.get_repository(repository)
    base_branch = _validate_branch(repository_info.default_branch)
    if branch_name == base_branch or branch_name in {"main", "master"}:
        raise RepositoryStateError("direct publication to a default branch is forbidden")
    latest_base_sha = client.get_branch_head(repository, base_branch)
    if latest_base_sha is None:
        raise RepositoryStateError(f"default branch does not exist: {base_branch}")
    latest_base_sha = _validate_sha(latest_base_sha, "latest base SHA")
    watched = sorted({item.path for item in manifest.files} | set(manifest.direct_dependencies))
    safe_base_heads = {requested_base_sha}
    effective_base_sha = requested_base_sha
    if latest_base_sha != requested_base_sha:
        conflicts = [
            path
            for path in watched
            if _remote_path_changed(client, repository, path, requested_base_sha, latest_base_sha)
        ]
        if conflicts:
            raise RepositoryStateError(
                "latest base changed selected files or direct dependencies: " + repr(conflicts)
            )
        effective_base_sha = latest_base_sha
    safe_base_heads.add(effective_base_sha)

    base_commit = client.get_commit(repository, effective_base_sha)
    if base_commit.sha != effective_base_sha:
        raise RepositoryStateError(
            f"base commit lookup mismatch: expected {effective_base_sha}, got {base_commit.sha}"
        )
    _validate_declared_operations(client, repository, effective_base_sha, manifest.files)

    existing_branch_head = client.get_branch_head(repository, branch_name)
    if existing_branch_head is not None:
        existing_branch_head = _validate_sha(existing_branch_head, "existing branch head")

    uploaded: dict[str, str] = {}
    for prepared in prepared_blobs:
        returned_sha = _validate_sha(
            client.create_blob(repository, prepared.data),
            f"GitHub blob SHA for {prepared.specification.path}",
        )
        if returned_sha != prepared.git_blob_sha:
            raise GitHubPublicationError(
                f"GitHub blob SHA mismatch for {prepared.specification.path}: "
                f"expected {prepared.git_blob_sha}, got {returned_sha}"
            )
        uploaded[prepared.specification.path] = returned_sha

    latest_before_tree = client.get_branch_head(repository, base_branch)
    if latest_before_tree is None:
        raise RepositoryStateError(f"default branch disappeared: {base_branch}")
    latest_before_tree = _validate_sha(latest_before_tree, "latest base SHA before tree")
    if latest_before_tree != effective_base_sha:
        conflicts = [
            path
            for path in watched
            if _remote_path_changed(
                client,
                repository,
                path,
                effective_base_sha,
                latest_before_tree,
            )
        ]
        if conflicts:
            raise RepositoryStateError(
                "default branch moved during blob publication and changed selected files "
                "or direct dependencies: " + repr(conflicts)
            )
        effective_base_sha = latest_before_tree
        safe_base_heads.add(effective_base_sha)
        base_commit = client.get_commit(repository, effective_base_sha)
        if base_commit.sha != effective_base_sha:
            raise RepositoryStateError(
                f"base commit lookup mismatch: expected {effective_base_sha}, got {base_commit.sha}"
            )
        _validate_declared_operations(client, repository, effective_base_sha, manifest.files)

    entries: list[TreeEntry] = []
    for item in manifest.files:
        if item.operation == "delete":
            entries.append(TreeEntry(path=item.path, mode="100644", sha=None))
        else:
            entries.append(
                TreeEntry(path=item.path, mode=str(item.mode), sha=uploaded[item.path])
            )
    tree_sha = _validate_sha(
        client.create_tree(repository, base_commit.tree_sha, entries),
        "created tree SHA",
    )
    if client.get_branch_head(repository, base_branch) != effective_base_sha:
        raise RepositoryStateError(
            "default branch moved after tree creation; rerun to rebuild from the latest base"
        )

    commit_reused = False
    if existing_branch_head is not None and existing_branch_head not in safe_base_heads:
        existing_commit = client.get_commit(repository, existing_branch_head)
        if (
            existing_commit.tree_sha == tree_sha
            and existing_commit.parent_shas == (effective_base_sha,)
            and existing_commit.message == commit_message
        ):
            commit_sha = existing_commit.sha
            commit_reused = True
        else:
            raise RepositoryStateError(
                f"branch {branch_name} has unrelated commit {existing_branch_head}; refusing to overwrite"
            )
    else:
        commit_sha = _validate_sha(
            client.create_commit(
                repository,
                commit_message,
                tree_sha,
                effective_base_sha,
            ),
            "created commit SHA",
        )
        commit_info = client.get_commit(repository, commit_sha)
        if (
            commit_info.tree_sha != tree_sha
            or commit_info.parent_shas != (effective_base_sha,)
            or commit_info.message != commit_message
        ):
            raise GitHubPublicationError("created commit metadata does not match the requested commit")

    compared_files = client.compare(repository, effective_base_sha, commit_sha)
    changed_paths = _verify_compare(compared_files, manifest.files)

    if client.get_branch_head(repository, base_branch) != effective_base_sha:
        raise RepositoryStateError(
            "default branch moved after commit verification; branch was not updated"
        )

    branch_created = False
    branch_updated = False
    if existing_branch_head is None:
        client.create_branch(repository, branch_name, commit_sha)
        branch_created = True
    elif existing_branch_head in safe_base_heads:
        client.update_branch(repository, branch_name, commit_sha)
        branch_updated = True
    elif existing_branch_head == commit_sha:
        pass
    else:
        # Only the validated idempotent branch state reaches this path.
        branch_updated = False

    published_head = client.get_branch_head(repository, branch_name)
    if published_head != commit_sha:
        raise GitHubPublicationError(
            f"branch verification failed: expected {commit_sha}, got {published_head}"
        )

    pr_number: int | None = None
    pr_url: str | None = None
    if create_pr:
        existing_pr = client.find_open_pull_request(repository, branch_name, base_branch)
        if existing_pr is None:
            pr = client.create_pull_request(
                repository,
                branch_name,
                base_branch,
                commit_message.splitlines()[0],
                _pr_body(manifest, changed_paths),
                draft=True,
            )
        else:
            pr = existing_pr
        pr_number = pr.number
        pr_url = pr.url

    return CommitBridgeResult(
        repository=repository,
        requested_base_sha=requested_base_sha,
        effective_base_sha=effective_base_sha,
        base_rebased=effective_base_sha != requested_base_sha,
        base_branch=base_branch,
        branch_name=branch_name,
        tree_sha=tree_sha,
        commit_sha=commit_sha,
        changed_paths=changed_paths,
        uploaded_blob_shas=uploaded,
        ignored_archive_entries=ignored_entries,
        branch_created=branch_created,
        branch_updated=branch_updated,
        commit_reused=commit_reused,
        pr_number=pr_number,
        pr_url=pr_url,
    )
