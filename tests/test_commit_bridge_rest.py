from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from commit_bridge_support import *


class CommitBridgeTests(unittest.TestCase):
    def test_rest_config_rejects_insecure_api_origin(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS origin"):
            GitHubRestConfig(token="test-token", api_url="http://github.example.test/api/v3")

    def test_rest_adapter_resolves_exact_tree_mode(self) -> None:
        class StubRestClient(GitHubRestClient):
            def __init__(self) -> None:
                super().__init__(GitHubRestConfig(token="test-token"))

            def _request(self, method, path, **kwargs):
                responses = {
                    "repos/owner/repo/git/commits/" + BASE: {
                        "sha": BASE,
                        "tree": {"sha": BASE_TREE},
                        "parents": [],
                        "message": "base",
                    },
                    "repos/owner/repo/git/trees/" + BASE_TREE: {
                        "tree": [
                            {"path": "tools", "mode": "040000", "type": "tree", "sha": "8" * 40}
                        ]
                    },
                    "repos/owner/repo/git/trees/" + "8" * 40: {
                        "tree": [
                            {"path": "run.py", "mode": "100755", "type": "blob", "sha": "9" * 40}
                        ]
                    },
                }
                return responses[path]

        remote = StubRestClient().get_path("owner/repo", "tools/run.py", BASE)
        self.assertIsNotNone(remote)
        assert remote is not None
        self.assertEqual(remote.mode, "100755")
        self.assertEqual(remote.object_type, "file")

    def test_mode_only_upstream_change_blocks_rebase(self) -> None:
        data = b"new"
        client = FakeClient()
        client.branch_heads["main"] = LATEST
        client.paths[(BASE, "new.txt")] = None
        client.paths[(LATEST, "new.txt")] = None
        client.paths[(BASE, "shared.py")] = RemotePath("shared.py", "file", "a" * 40, "100644")
        client.paths[(LATEST, "shared.py")] = RemotePath("shared.py", "file", "a" * 40, "100755")
        with self.assertRaisesRegex(RepositoryStateError, "direct dependencies"):
            commit_checkpoint_to_github(
                "owner/repo",
                BASE,
                "agent/mode-conflict",
                "Add new file",
                io.BytesIO(checkpoint_bytes({"workspace/new.txt": data})),
                io.BytesIO(
                    handoff_bytes(
                        [file_spec("new.txt", "add", data)],
                        dependencies=["shared.py"],
                    )
                ),
                client=client,
            )

    def test_existing_unrelated_branch_is_not_overwritten(self) -> None:
        data = b"new"
        client = FakeClient()
        unrelated = "6" * 40
        client.branch_heads["agent/existing"] = unrelated
        client.commits[unrelated] = CommitInfo(unrelated, "7" * 40, (BASE,), "other")
        client.compare_files = [ComparedFile("new.txt", "added")]
        with self.assertRaisesRegex(RepositoryStateError, "refusing to overwrite"):
            commit_checkpoint_to_github(
                "owner/repo",
                BASE,
                "agent/existing",
                "Add new file",
                io.BytesIO(checkpoint_bytes({"workspace/new.txt": data})),
                io.BytesIO(handoff_bytes([file_spec("new.txt", "add", data)])),
                client=client,
            )


if __name__ == "__main__":
    unittest.main()
