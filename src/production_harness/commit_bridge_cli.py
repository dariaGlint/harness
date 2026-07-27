"""CLI for publishing a checkpoint ZIP through Workspace Commit Bridge."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .commit_bridge import CommitBridgeError, commit_checkpoint_to_github


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="owner/name")
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--branch-name", required=True)
    parser.add_argument("--commit-message", required=True)
    parser.add_argument("--checkpoint-zip", type=Path, required=True)
    parser.add_argument("--handoff-json", type=Path, required=True)
    parser.add_argument("--create-pr", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = commit_checkpoint_to_github(
            repository=args.repository,
            base_sha=args.base_sha,
            branch_name=args.branch_name,
            commit_message=args.commit_message,
            checkpoint_zip=args.checkpoint_zip,
            handoff_json=args.handoff_json,
            create_pr=args.create_pr,
        )
    except (CommitBridgeError, OSError, ValueError) as exc:
        print(f"WORKSPACE_COMMIT_BRIDGE_FAILED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
