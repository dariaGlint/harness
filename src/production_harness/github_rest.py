"""Standard-library GitHub REST adapter for Workspace Commit Bridge."""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .commit_bridge import (
    CommitInfo,
    ComparedFile,
    GitHubPublicationError,
    PullRequestInfo,
    RemotePath,
    RepositoryInfo,
    TreeEntry,
)


@dataclass(frozen=True)
class GitHubRestConfig:
    token: str
    api_url: str = "https://api.github.com"
    timeout_seconds: float = 30.0
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not self.token or self.token != self.token.strip():
            raise ValueError("token must be non-empty and contain no surrounding whitespace")
        parsed = urllib.parse.urlsplit(self.api_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("api_url must be an HTTPS origin without credentials, query, or fragment")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")


class GitHubRestClient:
    """GitHub object API client compatible with App installation tokens."""

    def __init__(self, config: GitHubRestConfig) -> None:
        self._config = config
        self._commit_cache: dict[tuple[str, str], CommitInfo] = {}
        self._tree_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._path_cache: dict[tuple[str, str, str], RemotePath | None] = {}

    @classmethod
    def from_environment(cls) -> "GitHubRestClient":
        token = os.environ.get("WORKSPACE_COMMIT_BRIDGE_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token is not None:
            token = token.strip()
        if not token:
            raise GitHubPublicationError(
                "WORKSPACE_COMMIT_BRIDGE_TOKEN or GITHUB_TOKEN is required; "
                "use a GitHub App installation token"
            )
        api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        return cls(GitHubRestConfig(token=token, api_url=api_url))

    def _url(self, path: str, query: Mapping[str, object] | None = None) -> str:
        url = f"{self._config.api_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            encoded = urllib.parse.urlencode(
                {key: str(value) for key, value in query.items() if value is not None}
            )
            if encoded:
                url = f"{url}?{encoded}"
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        query: Mapping[str, object] | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        encoded = None
        if payload is not None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self._url(path, query),
            data=encoded,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._config.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "production-harness-workspace-commit-bridge",
                **({"Content-Type": "application/json"} if encoded is not None else {}),
            },
        )
        for attempt in range(1, self._config.max_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self._config.timeout_seconds) as response:
                    body = response.read()
                    if not body:
                        return None
                    return json.loads(body.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read(4096).decode("utf-8", errors="replace")
                if exc.code == 404 and allow_not_found:
                    return None
                retryable = exc.code in {429, 500, 502, 503, 504}
                if retryable and attempt < self._config.max_attempts:
                    time.sleep(0.5 * (2 ** (attempt - 1)))
                    continue
                raise GitHubPublicationError(
                    f"GitHub API {method} {path} failed with HTTP {exc.code}: {body}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < self._config.max_attempts:
                    time.sleep(0.5 * (2 ** (attempt - 1)))
                    continue
                raise GitHubPublicationError(
                    f"GitHub API {method} {path} failed after {attempt} attempts: {exc}"
                ) from exc
        raise AssertionError("unreachable")

    @staticmethod
    def _quote_path(path: str) -> str:
        return "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))

    def get_repository(self, repository: str) -> RepositoryInfo:
        payload = self._request("GET", f"repos/{repository}")
        default_branch = payload.get("default_branch") if isinstance(payload, dict) else None
        if not isinstance(default_branch, str) or not default_branch:
            raise GitHubPublicationError("repository response lacks default_branch")
        return RepositoryInfo(default_branch=default_branch)

    def get_branch_head(self, repository: str, branch: str) -> str | None:
        payload = self._request(
            "GET",
            f"repos/{repository}/git/ref/heads/{self._quote_path(branch)}",
            allow_not_found=True,
        )
        if payload is None:
            return None
        try:
            return str(payload["object"]["sha"])
        except (KeyError, TypeError) as exc:
            raise GitHubPublicationError("branch ref response lacks object.sha") from exc

    def get_commit(self, repository: str, commit_sha: str) -> CommitInfo:
        cache_key = (repository, commit_sha)
        cached = self._commit_cache.get(cache_key)
        if cached is not None:
            return cached
        payload = self._request("GET", f"repos/{repository}/git/commits/{commit_sha}")
        try:
            result = CommitInfo(
                sha=str(payload["sha"]),
                tree_sha=str(payload["tree"]["sha"]),
                parent_shas=tuple(str(parent["sha"]) for parent in payload.get("parents", [])),
                message=str(payload["message"]),
            )
        except (KeyError, TypeError) as exc:
            raise GitHubPublicationError("commit response is incomplete") from exc
        self._commit_cache[cache_key] = result
        return result

    def _tree_entries(self, repository: str, tree_sha: str) -> list[dict[str, Any]]:
        cache_key = (repository, tree_sha)
        cached = self._tree_cache.get(cache_key)
        if cached is not None:
            return cached
        payload = self._request("GET", f"repos/{repository}/git/trees/{tree_sha}")
        entries = payload.get("tree") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise GitHubPublicationError("tree response lacks tree entries")
        if payload.get("truncated") is True:
            raise GitHubPublicationError("tree response is truncated; path state is not provable")
        normalized: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise GitHubPublicationError("tree entry is not an object")
            if not all(isinstance(entry.get(key), str) for key in ("path", "mode", "type", "sha")):
                raise GitHubPublicationError("tree entry is incomplete")
            normalized.append(entry)
        self._tree_cache[cache_key] = normalized
        return normalized

    def get_path(self, repository: str, path: str, ref: str) -> RemotePath | None:
        cache_key = (repository, ref, path)
        if cache_key in self._path_cache:
            return self._path_cache[cache_key]
        tree_sha = self.get_commit(repository, ref).tree_sha
        parts = path.split("/")
        for index, part in enumerate(parts):
            matching = next(
                (entry for entry in self._tree_entries(repository, tree_sha) if entry["path"] == part),
                None,
            )
            if matching is None:
                self._path_cache[cache_key] = None
                return None
            final = index == len(parts) - 1
            if final:
                mode = str(matching["mode"])
                object_type = "symlink" if mode == "120000" else (
                    "file" if matching["type"] == "blob" else str(matching["type"])
                )
                result = RemotePath(
                    path=path,
                    object_type=object_type,
                    sha=str(matching["sha"]),
                    mode=mode,
                )
                self._path_cache[cache_key] = result
                return result
            if matching["type"] != "tree":
                self._path_cache[cache_key] = None
                return None
            tree_sha = str(matching["sha"])
        raise AssertionError("unreachable")

    def create_blob(self, repository: str, data: bytes) -> str:
        payload = self._request(
            "POST",
            f"repos/{repository}/git/blobs",
            payload={"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"},
        )
        try:
            return str(payload["sha"])
        except (KeyError, TypeError) as exc:
            raise GitHubPublicationError("create_blob response lacks sha") from exc

    def create_tree(
        self,
        repository: str,
        base_tree_sha: str,
        entries: Sequence[TreeEntry],
    ) -> str:
        tree = [
            {
                "path": entry.path,
                "mode": entry.mode,
                "type": entry.object_type,
                "sha": entry.sha,
            }
            for entry in entries
        ]
        payload = self._request(
            "POST",
            f"repos/{repository}/git/trees",
            payload={"base_tree": base_tree_sha, "tree": tree},
        )
        try:
            return str(payload["sha"])
        except (KeyError, TypeError) as exc:
            raise GitHubPublicationError("create_tree response lacks sha") from exc

    def create_commit(
        self,
        repository: str,
        message: str,
        tree_sha: str,
        parent_sha: str,
    ) -> str:
        payload = self._request(
            "POST",
            f"repos/{repository}/git/commits",
            payload={"message": message, "tree": tree_sha, "parents": [parent_sha]},
        )
        try:
            return str(payload["sha"])
        except (KeyError, TypeError) as exc:
            raise GitHubPublicationError("create_commit response lacks sha") from exc

    def compare(self, repository: str, base: str, head: str) -> Sequence[ComparedFile]:
        payload = self._request("GET", f"repos/{repository}/compare/{base}...{head}")
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list):
            raise GitHubPublicationError("compare response lacks files")
        if len(files) >= 300:
            raise GitHubPublicationError("compare response reached GitHub's 300-file limit")
        result: list[ComparedFile] = []
        for item in files:
            if not isinstance(item, dict):
                raise GitHubPublicationError("compare files entry is not an object")
            path = item.get("filename")
            status = item.get("status")
            if not isinstance(path, str) or not isinstance(status, str):
                raise GitHubPublicationError("compare files entry is incomplete")
            result.append(ComparedFile(path=path, status=status))
        return result

    def create_branch(self, repository: str, branch: str, commit_sha: str) -> None:
        self._request(
            "POST",
            f"repos/{repository}/git/refs",
            payload={"ref": f"refs/heads/{branch}", "sha": commit_sha},
        )

    def update_branch(self, repository: str, branch: str, commit_sha: str) -> None:
        self._request(
            "PATCH",
            f"repos/{repository}/git/refs/heads/{self._quote_path(branch)}",
            payload={"sha": commit_sha, "force": False},
        )

    def find_open_pull_request(
        self,
        repository: str,
        head_branch: str,
        base_branch: str,
    ) -> PullRequestInfo | None:
        owner = repository.split("/", 1)[0]
        payload = self._request(
            "GET",
            f"repos/{repository}/pulls",
            query={"state": "open", "head": f"{owner}:{head_branch}", "base": base_branch},
        )
        if not isinstance(payload, list):
            raise GitHubPublicationError("pull request list response is not an array")
        if not payload:
            return None
        first = payload[0]
        try:
            return PullRequestInfo(number=int(first["number"]), url=first.get("html_url"))
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubPublicationError("pull request list entry is incomplete") from exc

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
        payload = self._request(
            "POST",
            f"repos/{repository}/pulls",
            payload={
                "head": head_branch,
                "base": base_branch,
                "title": title,
                "body": body,
                "draft": draft,
            },
        )
        try:
            return PullRequestInfo(number=int(payload["number"]), url=payload.get("html_url"))
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubPublicationError("create_pull_request response is incomplete") from exc
