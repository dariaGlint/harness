from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from commit_bridge_support import *


class CommitBridgeTests(unittest.TestCase):
    def test_publish_add_modify_delete_as_one_tree_and_commit(self) -> None:
        add_data = b"new\n"
        modify_data = b"changed\n"
        files = [
            file_spec("new.txt", "add", add_data),
            file_spec("existing.txt", "modify", modify_data, mode="100755"),
            {"path": "old.txt", "operation": "delete"},
        ]
        archive = checkpoint_bytes(
            {
                "workspace/new.txt": add_data,
                "workspace/existing.txt": modify_data,
                "workspace/ignored.bin": b"ignored",
            }
        )
        client = FakeClient()
        client.paths[(BASE, "existing.txt")] = RemotePath(
            "existing.txt", "file", "a" * 40
        )
        client.paths[(BASE, "old.txt")] = RemotePath("old.txt", "file", "b" * 40)
        client.compare_files = [
            ComparedFile("existing.txt", "modified"),
            ComparedFile("new.txt", "added"),
            ComparedFile("old.txt", "removed"),
        ]
        result = commit_checkpoint_to_github(
            "owner/repo",
            BASE,
            "agent/bridge",
            "Publish checkpoint",
            io.BytesIO(archive),
            io.BytesIO(handoff_bytes(files)),
            create_pr=True,
            client=client,
        )
        self.assertIsInstance(result, CommitBridgeResult)
        self.assertEqual(result.commit_sha, NEW_COMMIT)
        self.assertEqual(result.changed_paths, ("existing.txt", "new.txt", "old.txt"))
        self.assertEqual(result.ignored_archive_entries, 1)
        self.assertTrue(result.branch_created)
        self.assertEqual(result.pr_number, 11)
        self.assertEqual(len(client.created_entries), 3)
        self.assertEqual(sum(entry.sha is None for entry in client.created_entries), 1)
        self.assertTrue(client.pr_request[-1])

    def test_stale_base_rebases_when_watched_paths_are_unchanged(self) -> None:
        data = b"new"
        client = FakeClient()
        client.branch_heads["main"] = LATEST
        client.paths[(BASE, "new.txt")] = None
        client.paths[(LATEST, "new.txt")] = None
        client.paths[(BASE, "shared.py")] = RemotePath("shared.py", "file", "a" * 40)
        client.paths[(LATEST, "shared.py")] = RemotePath("shared.py", "file", "a" * 40)
        client.compare_files = [ComparedFile("new.txt", "added")]
        result = commit_checkpoint_to_github(
            "owner/repo",
            BASE,
            "agent/rebased",
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
        self.assertTrue(result.base_rebased)
        self.assertEqual(result.effective_base_sha, LATEST)
        self.assertEqual(client.commits[NEW_COMMIT].parent_shas, (LATEST,))

    def test_default_branch_move_during_blob_upload_rebases_before_tree(self) -> None:
        data = b"new"

        class MovingBaseClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.main_reads = 0

            def get_branch_head(self, repository: str, branch: str) -> str | None:
                if branch == "main":
                    self.main_reads += 1
                    return BASE if self.main_reads == 1 else LATEST
                return super().get_branch_head(repository, branch)

        client = MovingBaseClient()
        client.paths[(BASE, "new.txt")] = None
        client.paths[(LATEST, "new.txt")] = None
        client.compare_files = [ComparedFile("new.txt", "added")]
        result = commit_checkpoint_to_github(
            "owner/repo",
            BASE,
            "agent/moving-base",
            "Add new file",
            io.BytesIO(checkpoint_bytes({"workspace/new.txt": data})),
            io.BytesIO(handoff_bytes([file_spec("new.txt", "add", data)])),
            client=client,
        )
        self.assertTrue(result.base_rebased)
        self.assertEqual(result.effective_base_sha, LATEST)
        self.assertEqual(client.commits[NEW_COMMIT].parent_shas, (LATEST,))

    def test_stale_base_blocks_when_dependency_changed(self) -> None:
        data = b"new"
        client = FakeClient()
        client.branch_heads["main"] = LATEST
        client.paths[(BASE, "new.txt")] = None
        client.paths[(LATEST, "new.txt")] = None
        client.paths[(BASE, "shared.py")] = RemotePath("shared.py", "file", "a" * 40)
        client.paths[(LATEST, "shared.py")] = RemotePath("shared.py", "file", "b" * 40)
        with self.assertRaisesRegex(RepositoryStateError, "direct dependencies"):
            commit_checkpoint_to_github(
                "owner/repo",
                BASE,
                "agent/conflict",
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


    def test_forbidden_defaults_cannot_be_disabled(self) -> None:
        data = b"cache"
        payload = json.loads(handoff_bytes([file_spec("safe.txt", "add", data)]))
        payload["commit_bridge"]["forbidden_prefixes"] = []
        payload["commit_bridge"]["files"] = [file_spec(".godot/cache.bin", "add", data)]
        manifest = load_handoff_manifest(io.BytesIO(json.dumps(payload).encode("utf-8")))
        with self.assertRaisesRegex(CheckpointValidationError, "forbidden publish paths"):
            prepare_checkpoint_blobs(
                io.BytesIO(checkpoint_bytes({"workspace/.godot/cache.bin": data})),
                manifest,
            )

    def test_packaged_handoff_schema_is_valid_json(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "production_harness"
            / "schemas"
            / "workspace_commit_handoff_v1.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$defs"]["commitBridge"]["properties"]["schema_version"]["const"],
            1,
        )

    def test_rebased_publish_fast_forwards_branch_left_at_requested_base(self) -> None:
        data = b"new"
        client = FakeClient()
        client.branch_heads["main"] = LATEST
        client.branch_heads["agent/rebased-existing"] = BASE
        client.paths[(BASE, "new.txt")] = None
        client.paths[(LATEST, "new.txt")] = None
        client.compare_files = [ComparedFile("new.txt", "added")]
        result = commit_checkpoint_to_github(
            "owner/repo",
            BASE,
            "agent/rebased-existing",
            "Add new file",
            io.BytesIO(checkpoint_bytes({"workspace/new.txt": data})),
            io.BytesIO(handoff_bytes([file_spec("new.txt", "add", data)])),
            client=client,
        )
        self.assertTrue(result.base_rebased)
        self.assertTrue(result.branch_updated)
        self.assertEqual(client.branch_heads["agent/rebased-existing"], NEW_COMMIT)

    def test_existing_matching_commit_is_reused_idempotently(self) -> None:
        data = b"new"
        client = FakeClient()
        client.branch_heads["agent/idempotent"] = NEW_COMMIT
        client.commits[NEW_COMMIT] = CommitInfo(
            NEW_COMMIT,
            NEW_TREE,
            (BASE,),
            "Add new file",
        )
        client.compare_files = [ComparedFile("new.txt", "added")]
        result = commit_checkpoint_to_github(
            "owner/repo",
            BASE,
            "agent/idempotent",
            "Add new file",
            io.BytesIO(checkpoint_bytes({"workspace/new.txt": data})),
            io.BytesIO(handoff_bytes([file_spec("new.txt", "add", data)])),
            client=client,
        )
        self.assertTrue(result.commit_reused)
        self.assertEqual(client.create_commit_calls, 0)
        self.assertFalse(result.branch_updated)



if __name__ == "__main__":
    unittest.main()
