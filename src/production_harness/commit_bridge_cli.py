"""CLI for publishing a checkpoint through Workspace Commit Bridge."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .commit_bridge import commit_checkpoint_to_github


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    publish = subparsers.add_parser("publish", help="validate and publish one checkpoint")
    publish.add_argument("--repository", required=True, help="owner/name")
    publish.add_argument("--base-sha", required=True)
    publish.add_argument("--branch", required=True)
    publish.add_argument("--message", required=True)
    publish.add_argument("--checkpoint", type=Path, required=True)
    publish.add_argument("--handoff", type=Path, required=True)
    publish.add_argument("--policy", type=Path)
    pr_group = publish.add_mutually_exclusive_group()
    pr_group.add_argument("--create-pr", dest="create_pr", action="store_true")
    pr_group.add_argument("--no-create-pr", dest="create_pr", action="store_false")
    publish.set_defaults(create_pr=False)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = commit_checkpoint_to_github(
        repository=args.repository,
        base_sha=args.base_sha,
        branch_name=args.branch,
        commit_message=args.message,
        checkpoint_zip=args.checkpoint,
        handoff_json=args.handoff,
        create_pr=args.create_pr,
        policy_json=args.policy,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.status == "success" else 10


if __name__ == "__main__":
    raise SystemExit(main())
