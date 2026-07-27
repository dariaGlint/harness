from __future__ import annotations

import json
import os
import unittest

from commit_bridge_support import *


class CommitBridgeRestTests(unittest.TestCase):
    def test_rest_config_rejects_insecure_origin(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS origin"):
            GitHubRestConfig(token="secret", api_url="http://github.example.test/api/v3")

    def test_token_is_redacted_from_repr(self) -> None:
        config = GitHubRestConfig(token="super-secret-token")
        self.assertNotIn("super-secret-token", repr(config))

    def test_result_does_not_contain_environment_token(self) -> None:
        secret = "do-not-leak-this-token"
        previous = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = secret
        try:
            result, _ = publish_one(branch="feature/invalid")
        finally:
            if previous is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = previous
        self.assertNotIn(secret, json.dumps(result.to_dict()))

    def test_rest_adapter_resolves_tree_mode(self) -> None:
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
                        "tree": [{"path": "tools", "mode": "040000", "type": "tree", "sha": "8" * 40}],
                        "truncated": False,
                    },
                    "repos/owner/repo/git/trees/" + "8" * 40: {
                        "tree": [{"path": "run.py", "mode": "100755", "type": "blob", "sha": "9" * 40}],
                        "truncated": False,
                    },
                }
                return responses[path]

        remote = StubRestClient().get_path("owner/repo", "tools/run.py", BASE)
        self.assertIsNotNone(remote)
        self.assertEqual(remote.mode, "100755")
        self.assertEqual(remote.object_type, "file")


if __name__ == "__main__":
    unittest.main()
