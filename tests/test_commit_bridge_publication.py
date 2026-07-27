from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from commit_bridge_support import *


class CommitBridgePublicationTests(unittest.TestCase):
    def test_success_order_and_one_tree_one_commit(self) -> None:
        result, client = publish_one(create_pr=True)
        self.assertEqual(result.status, "success")
        writes = [call for call in client.calls if call in {
            "create_blob", "create_tree", "create_commit", "compare_commits",
            "create_branch", "update_branch", "create_pull_request",
        }]
        self.assertEqual(
            writes,
            ["create_blob", "create_tree", "create_commit", "compare_commits", "create_branch", "create_pull_request"],
        )
        self.assertEqual(writes.count("create_tree"), 1)
        self.assertEqual(writes.count("create_commit"), 1)
        self.assertTrue(client.pr_request[-1])

    def test_create_pr_false_does_not_create_pr(self) -> None:
        result, client = publish_one(create_pr=False)
        self.assertEqual(result.status, "success")
        self.assertNotIn("create_pull_request", client.calls)
        self.assertIsNone(result.pr_number)

    def test_branch_verification_retries_transient_not_found(self) -> None:
        class DelayedBranchClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.created_branch_name = None
                self.visibility_misses_remaining = 2

            def create_branch(self, repository, branch, commit_sha):
                super().create_branch(repository, branch, commit_sha)
                self.created_branch_name = branch

            def get_branch_head(self, repository, branch):
                self.calls.append(f"get_branch_head:{branch}")
                if (
                    branch == self.created_branch_name
                    and self.visibility_misses_remaining > 0
                ):
                    self.visibility_misses_remaining -= 1
                    return None
                return self.branch_heads.get(branch)

        client = DelayedBranchClient()
        with mock.patch("production_harness.commit_bridge_publish.time.sleep") as sleep:
            result, client = publish_one(client=client, create_pr=True)

        self.assertEqual(result.status, "success")
        self.assertTrue(result.branch_created)
        self.assertTrue(result.pr_created)
        self.assertEqual(sleep.call_count, 2)

    def test_blob_sha_mismatch_stops_before_tree(self) -> None:
        client = FakeClient()
        client.blob_override = "9" * 40
        result, client = publish_one(client=client)
        self.assertEqual((result.stage, result.reason), ("blob_verification", "blob_sha_mismatch"))
        self.assertNotIn("create_tree", client.calls)

    def test_tree_mismatch_stops_before_commit(self) -> None:
        class BadTreeClient(FakeClient):
            def get_tree_path(self, repository, path, tree_sha):
                return None
        result, client = publish_one(client=BadTreeClient())
        self.assertEqual(result.reason, "tree_verification_mismatch")
        self.assertNotIn("create_commit", client.calls)

    def test_commit_mismatch_stops_before_branch(self) -> None:
        client = FakeClient()
        client.commit_override = CommitInfo(NEW_COMMIT, "8" * 40, (BASE,), "wrong")
        result, client = publish_one(client=client)
        self.assertEqual(result.reason, "commit_verification_mismatch")
        self.assertNotIn("create_branch", client.calls)

    def test_compare_extra_file_stops_before_branch(self) -> None:
        client = FakeClient()
        client.compare_result = CompareResult(
            "ahead", 1, 0,
            (ComparedFile("scripts/example.gd", "added"), ComparedFile("other.txt", "added")),
        )
        data = b"hello\n"
        files = [file_spec("scripts/example.gd", "add", data)]
        result = commit_checkpoint_to_github(
            "owner/repo", BASE, "agent/compare-extra", "test",
            io.BytesIO(checkpoint_bytes({"workspace/scripts/example.gd": data})),
            io.BytesIO(handoff_bytes(files)), client=client,
        )
        self.assertEqual(result.reason, "unexpected_compare_file")
        self.assertNotIn("create_branch", client.calls)

    def test_compare_missing_file_stops_before_branch(self) -> None:
        client = FakeClient()
        client.compare_result = CompareResult("ahead", 1, 0, ())
        data = b"hello\n"
        files = [file_spec("scripts/example.gd", "add", data)]
        result = commit_checkpoint_to_github(
            "owner/repo", BASE, "agent/compare-missing", "test",
            io.BytesIO(checkpoint_bytes({"workspace/scripts/example.gd": data})),
            io.BytesIO(handoff_bytes(files)), client=client,
        )
        self.assertEqual(result.reason, "expected_compare_file_missing")
        self.assertNotIn("create_branch", client.calls)

    def test_latest_master_rebase_without_conflict(self) -> None:
        client = FakeClient()
        client.branch_heads["main"] = LATEST
        client.paths[(BASE, "scripts/example.gd")] = None
        client.paths[(LATEST, "scripts/example.gd")] = None
        result, _ = publish_one(client=client)
        self.assertEqual(result.status, "success")
        self.assertTrue(result.base_rebased)
        self.assertEqual(result.effective_base_sha, LATEST)
        self.assertEqual(client.commits[NEW_COMMIT].parent_shas, (LATEST,))
        self.assertEqual(client.calls.count("compare_commits"), 1)

    def test_latest_master_dependency_conflict_rejects_before_blob(self) -> None:
        data = b"x"
        files = [file_spec("scripts/example.gd", "add", data)]
        client = FakeClient()
        client.branch_heads["main"] = LATEST
        client.paths[(BASE, "scripts/example.gd")] = None
        client.paths[(LATEST, "scripts/example.gd")] = None
        client.paths[(BASE, "shared.py")] = RemotePath("shared.py", "file", "a" * 40, "100644")
        client.paths[(LATEST, "shared.py")] = RemotePath("shared.py", "file", "b" * 40, "100644")
        result = commit_checkpoint_to_github(
            "owner/repo", BASE, "agent/conflict", "test",
            io.BytesIO(checkpoint_bytes({"workspace/scripts/example.gd": data})),
            io.BytesIO(handoff_bytes(files, dependencies=["shared.py"])), client=client,
        )
        self.assertEqual(result.reason, "latest_base_conflict")
        self.assertNotIn("create_blob", client.calls)

    def test_existing_unrelated_branch_is_not_overwritten(self) -> None:
        client = FakeClient()
        unrelated = "6" * 40
        client.branch_heads["agent/existing"] = unrelated
        client.commits[unrelated] = CommitInfo(unrelated, "7" * 40, (BASE,), "other")
        result, client = publish_one(client=client, branch="agent/existing")
        self.assertEqual(result.reason, "unrelated_branch_head")
        self.assertNotIn("update_branch", client.calls)

    def test_repository_hooks_are_invoked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "fakegate.py").write_text(
                '''CALLS = []\n\nclass Manager:\n    def verify(self, **kwargs):\n        CALLS.append(("verify", kwargs)); return {"result": "pass"}\n    def bind_head(self, **kwargs):\n        CALLS.append(("bind", kwargs)); return {"result": "pass"}\n\nMANAGER = Manager()\ndef manager(): return MANAGER\ndef commit_message_for_transaction(**kwargs):\n    CALLS.append(("message", kwargs))\n    return kwargs["base_message"] + "\\n\\nCanonical: yes", {"kind": "test"}, "a" * 64\n''',
                encoding="utf-8",
            )
            policy = {
                "schema_version": 1,
                "repository": "owner/repo",
                "allowed_branch_prefixes": ["agent/"],
                "admission": {
                    "search_path": ".",
                    "issue_claim_factory": "fakegate:manager",
                    "commit_message_callable": "fakegate:commit_message_for_transaction",
                    "required_handoff_fields": [
                        "task_id", "controller_run_id", "transaction_id", "validation_ownership"
                    ],
                },
            }
            policy_path = root / ".commit-bridge-policy.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            extra = {
                "task_id": "issue-7",
                "controller_run_id": "run-7",
                "transaction_id": "tx-7",
                "validation_ownership": {"test": True},
            }
            result, client = publish_one(policy_json=policy_path, extra_top=extra)
            self.assertEqual(result.status, "success")
            self.assertEqual(result.admission_sha256, "a" * 64)
            self.assertIn("Canonical: yes", client.created_message)
            sys.path.insert(0, str(root))
            try:
                import fakegate
                self.assertEqual([item[0] for item in fakegate.CALLS], ["verify", "message", "bind"])
            finally:
                sys.path.remove(str(root))


if __name__ == "__main__":
    unittest.main()
