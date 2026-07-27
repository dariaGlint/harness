"""Repository-owned admission hook integration for Workspace Commit Bridge."""
from __future__ import annotations

import contextlib
import importlib
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator, Mapping, Sequence

from .commit_bridge_types import (
    AdmissionError,
    CommitBridgePolicy,
    HandoffManifest,
    _SHA256_RE,
)


def _resolve_root(policy: CommitBridgePolicy) -> Path:
    admission = policy.admission
    if admission is None:
        return Path.cwd()
    if policy.source_path is None:
        raise AdmissionError(
            "admission_policy_requires_path",
            "admission hooks require policy_json to be supplied by filesystem path",
        )
    root = policy.source_path.parent.resolve()
    candidate = (root / admission.search_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise AdmissionError(
            "unsafe_admission_search_path", "admission search_path escapes the policy repository"
        )
    return candidate


@contextlib.contextmanager
def _import_root(root: Path) -> Iterator[None]:
    value = str(root)
    sys.path.insert(0, value)
    try:
        yield
    finally:
        try:
            sys.path.remove(value)
        except ValueError:
            pass


def _load_symbol(specification: str, root: Path) -> Callable[..., Any]:
    module_name, symbol_name = specification.split(":", 1)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module_name):
        raise AdmissionError("invalid_admission_symbol", f"invalid module path: {module_name!r}")
    if not symbol_name.isidentifier():
        raise AdmissionError("invalid_admission_symbol", f"invalid symbol name: {symbol_name!r}")
    try:
        with _import_root(root):
            module: ModuleType = importlib.import_module(module_name)
    except Exception as exc:
        raise AdmissionError(
            "admission_import_failed", f"cannot import admission module {module_name}: {exc}"
        ) from exc
    symbol = getattr(module, symbol_name, None)
    if not callable(symbol):
        raise AdmissionError(
            "admission_symbol_missing", f"admission callable is missing: {specification}"
        )
    return symbol


class AdmissionRuntime:
    """Invoke existing repository gates without reimplementing their contracts."""

    def __init__(self, policy: CommitBridgePolicy, manifest: HandoffManifest) -> None:
        self.policy = policy
        self.manifest = manifest
        self._root: Path | None = None
        self._claim_manager: Any = None
        self._message_callable: Callable[..., Any] | None = None
        admission = policy.admission
        if admission is None:
            return
        self._root = _resolve_root(policy)
        if admission.issue_claim_factory:
            factory = _load_symbol(admission.issue_claim_factory, self._root)
            try:
                self._claim_manager = factory()
            except Exception as exc:
                raise AdmissionError(
                    "issue_claim_factory_failed", f"Issue Work Claim factory failed: {exc}"
                ) from exc
            if self._claim_manager is None:
                raise AdmissionError(
                    "issue_claim_unavailable",
                    "Issue Work Claim factory returned no manager; required credentials are unavailable",
                )
        if admission.commit_message_callable:
            self._message_callable = _load_symbol(admission.commit_message_callable, self._root)

    def _claim_kwargs(self, *, base_sha: str, branch_name: str) -> dict[str, Any]:
        manifest = self.manifest
        missing = [
            key
            for key, value in (
                ("issue_number", manifest.issue_number),
                ("task_id", manifest.task_id),
                ("controller_run_id", manifest.controller_run_id),
            )
            if value in (None, "")
        ]
        if missing:
            raise AdmissionError(
                "issue_claim_context_missing", f"Issue Work Claim context is missing: {missing}"
            )
        return {
            "issue_number": manifest.issue_number,
            "task_id": manifest.task_id,
            "controller_run_id": manifest.controller_run_id,
            "base_sha": base_sha,
            "branch_name": branch_name,
        }

    def verify_issue_claim(self, *, base_sha: str, branch_name: str) -> Mapping[str, Any] | None:
        if self._claim_manager is None:
            return None
        method_name = self.policy.admission.issue_claim_verify_method  # type: ignore[union-attr]
        method = getattr(self._claim_manager, method_name, None)
        if not callable(method):
            raise AdmissionError(
                "issue_claim_verify_missing", f"Issue Work Claim method is missing: {method_name}"
            )
        try:
            result = method(**self._claim_kwargs(base_sha=base_sha, branch_name=branch_name))
        except Exception as exc:
            raise AdmissionError(
                "issue_claim_verification_failed", f"Issue Work Claim verification failed: {exc}"
            ) from exc
        if not isinstance(result, Mapping):
            raise AdmissionError(
                "issue_claim_verification_invalid", "Issue Work Claim verification returned invalid data"
            )
        return result

    def bind_issue_claim(
        self,
        *,
        base_sha: str,
        branch_name: str,
        head_sha: str,
    ) -> Mapping[str, Any] | None:
        if self._claim_manager is None:
            return None
        method_name = self.policy.admission.issue_claim_bind_method  # type: ignore[union-attr]
        method = getattr(self._claim_manager, method_name, None)
        if not callable(method):
            raise AdmissionError(
                "issue_claim_bind_missing", f"Issue Work Claim method is missing: {method_name}"
            )
        kwargs = self._claim_kwargs(base_sha=base_sha, branch_name=branch_name)
        kwargs["head_sha"] = head_sha
        try:
            result = method(**kwargs)
        except Exception as exc:
            raise AdmissionError(
                "issue_claim_bind_failed", f"Issue Work Claim bind-head failed: {exc}"
            ) from exc
        if not isinstance(result, Mapping):
            raise AdmissionError(
                "issue_claim_bind_invalid", "Issue Work Claim bind-head returned invalid data"
            )
        return result

    def build_commit_message(
        self,
        *,
        base_message: str,
        repository: str,
        branch_name: str,
        base_sha: str,
        tree_sha: str,
        changed_files: Sequence[str],
    ) -> tuple[str, str | None]:
        if self._message_callable is None:
            return base_message, None
        manifest = self.manifest
        missing = [
            key
            for key, value in (
                ("issue_number", manifest.issue_number),
                ("transaction_id", manifest.transaction_id),
                ("validation_ownership", manifest.validation_ownership),
            )
            if value in (None, "")
        ]
        if missing:
            raise AdmissionError(
                "canonical_admission_context_missing",
                f"canonical publication context is missing: {missing}",
            )
        try:
            output = self._message_callable(
                base_message=base_message,
                repository_full_name=repository,
                issue_number=manifest.issue_number,
                transaction_id=manifest.transaction_id,
                branch_name=branch_name,
                base_sha=base_sha,
                tree_sha=tree_sha,
                changed_files=list(changed_files),
                validation_ownership=manifest.validation_ownership,
            )
        except Exception as exc:
            raise AdmissionError(
                "canonical_publication_rejected",
                f"canonical publication admission failed: {exc}",
            ) from exc
        if (
            not isinstance(output, tuple)
            or len(output) != 3
            or not isinstance(output[0], str)
            or not output[0].strip()
            or not isinstance(output[1], Mapping)
            or not isinstance(output[2], str)
            or not _SHA256_RE.fullmatch(output[2])
        ):
            raise AdmissionError(
                "canonical_publication_invalid",
                "canonical publication callable returned an invalid result",
            )
        return output[0], output[2]
