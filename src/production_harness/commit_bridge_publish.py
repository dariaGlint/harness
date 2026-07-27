"""Fail-closed GitHub publication transaction for Workspace Commit Bridge."""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .commit_bridge_admission import AdmissionRuntime
from .commit_bridge_archive import prepare_checkpoint_blobs
from .commit_bridge_handoff import (
    load_handoff_manifest,
    validate_branch_name,
    validate_commit_message,
)
from .commit_bridge_policy import load_commit_bridge_policy
from .commit_bridge_types import (
    BridgeLimits,
    CommitBridgeError,
    CommitBridgePolicy,
    CommitBridgeResult,
    CompareResult,
    ComparedFile,
    GitHubCommitClient,
    GitHubPublicationError,
    HandoffFile,
    HandoffValidationError,
    RepositoryStateError,
    TreeEntry,
    _REPOSITORY_RE,
    _SHA_RE,
)


def _validate_repository(value: str) -> str:
    if (
        not isinstance(value, str)
        or not _REPOSITORY_RE.fullmatch(value)
        or value.endswith(".git")
    ):
        raise HandoffValidationError("invalid_repository", f"invalid repository: {value!r}")
    return value


def _validate_sha(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value.lower()):
        raise HandoffValidationError("invalid_git_sha", f"invalid {label}: {value!r}")
    return value.lower()


def _expected_status(operation: str) -> str:
    return {"add": "added", "modify": "modified", "delete": "removed"}[operation]


def _verify_final_compare(compare: CompareResult, expected: Sequence[HandoffFile]) -> tuple[str, ...]:
    if compare.status not in {"ahead", "identical"} or compare.behind_by != 0:
        raise GitHubPublicationError(
            "compare_relationship_invalid",
            f"created commit is not a descendant of effective base: status={compare.status}",
        )
    if not compare.files_complete:
        raise GitHubPublicationError(
            "compare_incomplete", "GitHub compare result is incomplete; exact scope is not provable"
        )
    actual = {item.path: item.status for item in compare.files}
    expected_map = {item.path: _expected_status(item.operation) for item in expected}
    if actual != expected_map:
        missing = sorted(set(expected_map) - set(actual))
        unexpected = sorted(set(actual) - set(expected_map))
        wrong_status = {
            path: {"expected": expected_map[path], "actual": actual[path]}
            for path in sorted(set(actual) & set(expected_map))
            if actual[path] != expected_map[path]
        }
        reason = "unexpected_compare_file" if unexpected else "expected_compare_file_missing"
        raise GitHubPublicationError(
            reason,
            "published diff mismatch: "
            f"missing={missing}, unexpected={unexpected}, wrong_status={wrong_status}",
        )
    return tuple(sorted(actual))


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


def _verify_base_relationship(
    client: GitHubCommitClient,
    repository: str,
    old_base: str,
    new_base: str,
    watched: Sequence[str],
) -> None:
    """Prove ancestry without consuming the single final compare operation."""
    if old_base != new_base:
        pending = [new_base]
        visited: set[str] = set()
        found = False
        while pending:
            candidate = pending.pop()
            if candidate in visited:
                continue
            visited.add(candidate)
            if len(visited) > 20_000:
                raise RepositoryStateError(
                    "ancestry_limit_exceeded",
                    "commit ancestry exceeded 20000 commits; base relationship is not provable",
                )
            if candidate == old_base:
                found = True
                break
            commit = client.get_commit(repository, candidate)
            pending.extend(parent for parent in commit.parent_shas if parent not in visited)
        if not found:
            raise RepositoryStateError(
                "base_not_ancestor",
                "specified base is not an ancestor of latest default branch",
            )
    conflicts = [
        path for path in watched if _remote_path_changed(client, repository, path, old_base, new_base)
    ]
    if conflicts:
        raise RepositoryStateError(
            "latest_base_conflict",
            "latest base changed selected files or direct dependencies: " + repr(conflicts),
        )


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
                "non_file_target",
                f"repository path is not file-like at base: {item.path} ({existing.object_type})",
            )
        if item.operation == "add" and existing is not None:
            raise RepositoryStateError("add_path_exists", f"add path already exists: {item.path}")
        if item.operation in {"modify", "delete"} and existing is None:
            raise RepositoryStateError(
                "target_path_missing", f"{item.operation} path does not exist: {item.path}"
            )


def _verify_tree(
    client: GitHubCommitClient,
    repository: str,
    tree_sha: str,
    files: Sequence[HandoffFile],
    uploaded: Mapping[str, str],
) -> None:
    for item in files:
        remote = client.get_tree_path(repository, item.path, tree_sha)
        if item.operation == "delete":
            if remote is not None:
                raise GitHubPublicationError(
                    "tree_verification_mismatch", f"deleted path remains in created tree: {item.path}"
                )
            continue
        if remote is None:
            raise GitHubPublicationError(
                "tree_verification_mismatch", f"created tree is missing path: {item.path}"
            )
        if (
            remote.object_type not in {"file", "symlink"}
            or remote.sha != uploaded[item.path]
            or remote.mode != item.mode
        ):
            raise GitHubPublicationError(
                "tree_verification_mismatch",
                f"created tree entry differs for {item.path}: "
                f"sha={remote.sha}, mode={remote.mode}, type={remote.object_type}",
            )


def _pr_body(issue_number: int | None, paths: Sequence[str]) -> str:
    lines = [
        "## Workspace Commit Bridge",
        "",
        "Published from a validated checkpoint ZIP, `handoff.json`, and repository policy.",
        "",
        "### Changed files",
        "",
        *(f"- `{path}`" for path in paths),
    ]
    if issue_number is not None:
        lines.extend(("", f"Closes #{issue_number}"))
    return "\n".join(lines)


def _safe_message(exc: BaseException) -> str:
    text = str(exc).replace("\0", "")
    return text[:4000]


def _publish(
    repository: str,
    base_sha: str,
    branch_name: str,
    commit_message: str,
    checkpoint_zip: Any,
    handoff_json: Any,
    create_pr: bool,
    *,
    policy_json: Any,
    client: GitHubCommitClient | None,
    limits: BridgeLimits,
    state: dict[str, Any],
    set_stage: Any,
) -> CommitBridgeResult:
    set_stage("input_validation")
    repository = _validate_repository(repository)
    requested_base_sha = _validate_sha(base_sha, "base_sha")
    branch_name = validate_branch_name(branch_name)
    commit_message = validate_commit_message(commit_message)

    set_stage("policy_validation")
    policy = load_commit_bridge_policy(policy_json, limits=limits)
    if policy.repository is not None and policy.repository != repository:
        raise HandoffValidationError(
            "policy_repository_mismatch",
            f"policy repository differs: expected {policy.repository}, got {repository}",
        )
    if not any(branch_name.startswith(prefix) for prefix in policy.allowed_branch_prefixes):
        raise RepositoryStateError(
            "unauthorized_branch_prefix",
            f"branch must start with one of {policy.allowed_branch_prefixes}: {branch_name}",
        )

    set_stage("handoff_validation")
    manifest = load_handoff_manifest(handoff_json, policy=policy, limits=limits)
    if manifest.repository != repository:
        raise HandoffValidationError(
            "repository_mismatch",
            f"repository mismatch: request={repository}, handoff={manifest.repository}",
        )
    if manifest.base_sha != requested_base_sha:
        raise HandoffValidationError(
            "base_sha_mismatch",
            f"base_sha mismatch: request={requested_base_sha}, handoff={manifest.base_sha}",
        )
    if (
        manifest.behavior_change
        and policy.player_experience_requires_preview
        and manifest.preview_approved is not True
    ):
        raise HandoffValidationError(
            "preview_not_approved",
            "player-experience change requires preview_approved=true before publication",
        )

    set_stage("checkpoint_validation")
    prepared_blobs = prepare_checkpoint_blobs(
        checkpoint_zip, manifest, policy=policy, limits=limits
    )

    if client is None:
        from .github_rest import GitHubRestClient

        client = GitHubRestClient.from_environment()
    if not isinstance(client, GitHubCommitClient):
        raise TypeError("client does not implement GitHubCommitClient")

    set_stage("repository_state")
    repository_info = client.get_repository(repository)
    base_branch = validate_branch_name(repository_info.default_branch)
    state["base_branch"] = base_branch
    if policy.forbid_default_branch_update and (
        branch_name == base_branch or branch_name in {"main", "master"}
    ):
        raise RepositoryStateError(
            "default_branch_forbidden", "direct publication to a default branch is forbidden"
        )
    latest_base_sha = client.get_branch_head(repository, base_branch)
    if latest_base_sha is None:
        raise RepositoryStateError(
            "default_branch_missing", f"default branch does not exist: {base_branch}"
        )
    latest_base_sha = _validate_sha(latest_base_sha, "latest default branch SHA")
    watched = sorted({item.path for item in manifest.files} | set(manifest.direct_dependencies))
    effective_base_sha = requested_base_sha
    safe_branch_heads = {requested_base_sha}
    if latest_base_sha != requested_base_sha:
        set_stage("upstream_conflict_check")
        _verify_base_relationship(
            client, repository, requested_base_sha, latest_base_sha, watched
        )
        effective_base_sha = latest_base_sha
        state["base_rebased"] = True
    safe_branch_heads.add(effective_base_sha)
    state["effective_base_sha"] = effective_base_sha
    base_commit = client.get_commit(repository, effective_base_sha)
    if base_commit.sha != effective_base_sha:
        raise RepositoryStateError(
            "base_commit_mismatch",
            f"base commit lookup mismatch: expected {effective_base_sha}, got {base_commit.sha}",
        )
    _validate_declared_operations(client, repository, effective_base_sha, manifest.files)
    existing_branch_head = client.get_branch_head(repository, branch_name)
    if existing_branch_head is not None:
        existing_branch_head = _validate_sha(existing_branch_head, "existing branch head")

    set_stage("repository_admission")
    admission = AdmissionRuntime(policy, manifest)
    admission.verify_issue_claim(base_sha=requested_base_sha, branch_name=branch_name)

    uploaded: dict[str, str] = {}
    set_stage("blob_creation")
    for prepared in prepared_blobs:
        returned_sha = _validate_sha(
            client.create_blob(repository, prepared.data),
            f"GitHub blob SHA for {prepared.specification.path}",
        )
        set_stage("blob_verification")
        if returned_sha != prepared.git_blob_sha:
            raise GitHubPublicationError(
                "blob_sha_mismatch",
                f"GitHub blob SHA mismatch for {prepared.specification.path}: "
                f"expected {prepared.git_blob_sha}, got {returned_sha}",
            )
        uploaded[prepared.specification.path] = returned_sha
        state["uploaded_blob_shas"] = dict(uploaded)
        set_stage("blob_creation")

    set_stage("latest_base_refresh")
    latest_before_tree = client.get_branch_head(repository, base_branch)
    if latest_before_tree is None:
        raise RepositoryStateError("default_branch_missing", f"default branch disappeared: {base_branch}")
    latest_before_tree = _validate_sha(latest_before_tree, "latest default branch SHA before tree")
    if latest_before_tree != effective_base_sha:
        _verify_base_relationship(
            client, repository, effective_base_sha, latest_before_tree, watched
        )
        effective_base_sha = latest_before_tree
        safe_branch_heads.add(effective_base_sha)
        state["effective_base_sha"] = effective_base_sha
        state["base_rebased"] = True
        base_commit = client.get_commit(repository, effective_base_sha)
        if base_commit.sha != effective_base_sha:
            raise RepositoryStateError(
                "base_commit_mismatch",
                f"base commit lookup mismatch: expected {effective_base_sha}, got {base_commit.sha}",
            )
        _validate_declared_operations(client, repository, effective_base_sha, manifest.files)

    entries = [
        TreeEntry(path=item.path, mode=item.mode or "100644", sha=None)
        if item.operation == "delete"
        else TreeEntry(path=item.path, mode=str(item.mode), sha=uploaded[item.path])
        for item in manifest.files
    ]
    set_stage("tree_creation")
    tree_sha = _validate_sha(
        client.create_tree(repository, base_commit.tree_sha, entries), "created tree SHA"
    )
    state["tree_sha"] = tree_sha
    state["tree_created"] = True

    set_stage("tree_verification")
    _verify_tree(client, repository, tree_sha, manifest.files, uploaded)
    if client.get_branch_head(repository, base_branch) != effective_base_sha:
        raise RepositoryStateError(
            "default_branch_moved_after_tree",
            "default branch moved after tree creation; commit was not created",
        )

    set_stage("canonical_publication_admission")
    changed_file_paths = tuple(item.path for item in manifest.files)
    final_commit_message, admission_sha256 = admission.build_commit_message(
        base_message=commit_message,
        repository=repository,
        branch_name=branch_name,
        base_sha=effective_base_sha,
        tree_sha=tree_sha,
        changed_files=changed_file_paths,
    )
    state["admission_sha256"] = admission_sha256

    set_stage("commit_creation")
    commit_reused = False
    commit_created = False
    if existing_branch_head is not None and existing_branch_head not in safe_branch_heads:
        existing_commit = client.get_commit(repository, existing_branch_head)
        if (
            existing_commit.tree_sha == tree_sha
            and existing_commit.parent_shas == (effective_base_sha,)
            and existing_commit.message == final_commit_message
        ):
            commit_sha = existing_commit.sha
            commit_reused = True
            state["commit_reused"] = True
        else:
            raise RepositoryStateError(
                "unrelated_branch_head",
                f"branch {branch_name} has unrelated commit {existing_branch_head}; refusing to overwrite",
            )
    else:
        commit_sha = _validate_sha(
            client.create_commit(
                repository, final_commit_message, tree_sha, effective_base_sha
            ),
            "created commit SHA",
        )
        commit_created = True
        state["commit_created"] = True
    state["commit_sha"] = commit_sha

    set_stage("commit_verification")
    commit_info = client.get_commit(repository, commit_sha)
    if (
        commit_info.sha != commit_sha
        or commit_info.tree_sha != tree_sha
        or commit_info.parent_shas != (effective_base_sha,)
        or commit_info.message != final_commit_message
    ):
        raise GitHubPublicationError(
            "commit_verification_mismatch",
            "created commit metadata does not match requested tree, parent, or message",
        )

    set_stage("compare_verification")
    final_compare = client.compare_commits(repository, effective_base_sha, commit_sha)
    changed_paths = _verify_final_compare(final_compare, manifest.files)
    state["changed_files"] = changed_paths
    state["compare_verified"] = True

    if client.get_branch_head(repository, base_branch) != effective_base_sha:
        raise RepositoryStateError(
            "default_branch_moved_after_compare",
            "default branch moved after commit verification; branch was not updated",
        )

    set_stage("issue_claim_binding")
    admission.bind_issue_claim(
        base_sha=requested_base_sha,
        branch_name=branch_name,
        head_sha=commit_sha,
    )

    set_stage("branch_update")
    branch_created = False
    branch_updated = False
    if existing_branch_head is None:
        client.create_branch(repository, branch_name, commit_sha)
        branch_created = True
        state["branch_created"] = True
    elif existing_branch_head in safe_branch_heads:
        client.update_branch(repository, branch_name, commit_sha)
        branch_updated = True
        state["branch_updated"] = True
    elif existing_branch_head == commit_sha or commit_reused:
        pass
    else:  # Defensive; unrelated branches are rejected before commit creation.
        raise RepositoryStateError(
            "unrelated_branch_head", f"branch changed unexpectedly: {branch_name}"
        )

    set_stage("branch_verification")
    published_head = client.get_branch_head(repository, branch_name)
    if published_head != commit_sha:
        raise GitHubPublicationError(
            "branch_verification_mismatch",
            f"branch verification failed: expected {commit_sha}, got {published_head}",
        )

    pr_number: int | None = None
    pr_url: str | None = None
    pr_created = False
    if create_pr:
        set_stage("pr_creation")
        existing_pr = client.find_open_pull_request(repository, branch_name, base_branch)
        if existing_pr is not None:
            if not existing_pr.draft:
                raise GitHubPublicationError(
                    "non_draft_pr_exists", "an existing non-draft pull request is not accepted"
                )
            pr = existing_pr
        else:
            pr = client.create_pull_request(
                repository,
                branch_name,
                base_branch,
                final_commit_message.splitlines()[0],
                _pr_body(manifest.issue_number, changed_paths),
                draft=True,
            )
            pr_created = True
            state["pr_created"] = True
        pr_number = pr.number
        pr_url = pr.url

    return CommitBridgeResult(
        status="success",
        repository=repository,
        requested_base_sha=requested_base_sha,
        effective_base_sha=effective_base_sha,
        base_branch=base_branch,
        branch=branch_name,
        tree_sha=tree_sha,
        commit_sha=commit_sha,
        pr_number=pr_number,
        pr_url=pr_url,
        changed_files=changed_paths,
        compare_verified=True,
        tree_created=True,
        commit_created=commit_created,
        branch_created=branch_created,
        branch_updated=branch_updated,
        pr_created=pr_created,
        base_rebased=effective_base_sha != requested_base_sha,
        commit_reused=commit_reused,
        uploaded_blob_shas=uploaded,
        admission_sha256=admission_sha256,
    )


def commit_checkpoint_to_github(
    repository: str,
    base_sha: str,
    branch_name: str,
    commit_message: str,
    checkpoint_zip: Any,
    handoff_json: Any,
    create_pr: bool = False,
    *,
    policy_json: Any = None,
    client: GitHubCommitClient | None = None,
    limits: BridgeLimits = BridgeLimits(),
) -> CommitBridgeResult:
    """Publish file references without exposing file contents in the caller's tool arguments."""
    state: dict[str, Any] = {}
    current_stage = "input_validation"

    def set_stage(value: str) -> None:
        nonlocal current_stage
        current_stage = value

    try:
        return _publish(
            repository,
            base_sha,
            branch_name,
            commit_message,
            checkpoint_zip,
            handoff_json,
            create_pr,
            policy_json=policy_json,
            client=client,
            limits=limits,
            state=state,
            set_stage=set_stage,
        )
    except CommitBridgeError as exc:
        return CommitBridgeResult.rejected(
            repository=repository if isinstance(repository, str) else "",
            requested_base_sha=base_sha if isinstance(base_sha, str) else "",
            branch=branch_name if isinstance(branch_name, str) else "",
            stage=current_stage,
            reason=exc.reason,
            message=_safe_message(exc),
            state=state,
        )
    except (OSError, ValueError, TypeError) as exc:
        return CommitBridgeResult.rejected(
            repository=repository if isinstance(repository, str) else "",
            requested_base_sha=base_sha if isinstance(base_sha, str) else "",
            branch=branch_name if isinstance(branch_name, str) else "",
            stage=current_stage,
            reason="runtime_error",
            message=_safe_message(exc),
            state=state,
        )
