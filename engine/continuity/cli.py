"""CLI for Continuity's model-agnostic long-horizon state."""

from __future__ import annotations

import argparse
import json
import sys

from .novelkit import bootstrap_novelkit
from .runtime import Continuity, ContinuityError


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="continuity",
        description="Persistent semantic state for long-horizon agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init")
    p.add_argument("path")
    p.add_argument("--title", required=True)
    p.add_argument("--objective", required=True)
    p.add_argument("--domain", default="generic")

    p = sub.add_parser("status")
    p.add_argument("path")

    p = sub.add_parser("context")
    p.add_argument("path")
    p.add_argument("--recent", type=int, default=8)

    p = sub.add_parser("verify")
    p.add_argument("path")

    p = sub.add_parser("constraint")
    p.add_argument("path")
    p.add_argument("text")
    p.add_argument("--severity", default="blocking",
                   choices=["blocking", "important", "advisory"])

    p = sub.add_parser("constraint-retire")
    p.add_argument("path")
    p.add_argument("constraint_id")
    p.add_argument("reason")

    p = sub.add_parser("fact")
    p.add_argument("path")
    p.add_argument("statement")
    p.add_argument("--source", default="human")
    p.add_argument("--supersedes")

    p = sub.add_parser("milestone")
    p.add_argument("path")
    p.add_argument("title")
    p.add_argument("--criterion", action="append", default=[])
    p.add_argument("--activate", action="store_true")

    p = sub.add_parser("milestone-activate")
    p.add_argument("path")
    p.add_argument("milestone_id")

    p = sub.add_parser("commitment")
    p.add_argument("path")
    p.add_argument("text")

    p = sub.add_parser("commitment-close")
    p.add_argument("path")
    p.add_argument("commitment_id")
    p.add_argument("--status", default="done", choices=["done", "dropped"])

    p = sub.add_parser("question")
    p.add_argument("path")
    p.add_argument("text")

    p = sub.add_parser("question-resolve")
    p.add_argument("path")
    p.add_argument("question_id")
    p.add_argument("answer")

    p = sub.add_parser("resource")
    p.add_argument("path")
    p.add_argument("resource_path")
    p.add_argument("--role", default="reference")
    p.add_argument("--note", default="")

    p = sub.add_parser("step-begin")
    p.add_argument("path")
    p.add_argument("objective")
    p.add_argument("--intent", default="execute")
    p.add_argument("--milestone")
    p.add_argument("--precondition", action="append", default=[])
    p.add_argument("--expect", action="append", default=[])

    p = sub.add_parser("step-evidence")
    p.add_argument("path")
    p.add_argument("criterion")
    p.add_argument("summary")
    p.add_argument("--source", default="external")
    p.add_argument("--status", default="pass", choices=["pass", "fail", "unknown"])
    p.add_argument("--verified", action="store_true")

    p = sub.add_parser("step-finish")
    p.add_argument("path")
    p.add_argument("outcome")
    p.add_argument("--learning", action="append", default=[])
    p.add_argument("--milestone-done", action="store_true")
    p.add_argument("--override-reason")

    p = sub.add_parser("step-fail")
    p.add_argument("path")
    p.add_argument("reason")
    p.add_argument("--classification", default="execution")

    p = sub.add_parser("novelkit-bootstrap")
    p.add_argument("book_dir")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            runtime = Continuity.create(
                args.path, args.title, args.objective, args.domain
            )
            _print(runtime.status())
        elif args.command == "novelkit-bootstrap":
            runtime = bootstrap_novelkit(args.book_dir)
            _print(runtime.context_pack())
        else:
            runtime = Continuity(args.path)
            if args.command == "status":
                _print(runtime.status())
            elif args.command == "context":
                _print(runtime.context_pack(recent_events=args.recent))
            elif args.command == "verify":
                _print(runtime.verify())
            elif args.command == "constraint":
                _print(runtime.add_constraint(args.text, args.severity))
            elif args.command == "constraint-retire":
                runtime.retire_constraint(args.constraint_id, args.reason)
                _print(runtime.context_pack())
            elif args.command == "fact":
                _print(runtime.add_fact(
                    args.statement,
                    source=args.source,
                    supersedes=args.supersedes,
                ))
            elif args.command == "milestone":
                _print(runtime.add_milestone(
                    args.title, args.criterion, activate=args.activate
                ))
            elif args.command == "milestone-activate":
                runtime.activate_milestone(args.milestone_id)
                _print(runtime.status())
            elif args.command == "commitment":
                _print(runtime.add_commitment(args.text))
            elif args.command == "commitment-close":
                runtime.close_commitment(args.commitment_id, args.status)
                _print(runtime.status())
            elif args.command == "question":
                _print(runtime.add_question(args.text))
            elif args.command == "question-resolve":
                runtime.resolve_question(args.question_id, args.answer)
                _print(runtime.status())
            elif args.command == "resource":
                _print(runtime.add_resource(
                    args.resource_path, role=args.role, note=args.note
                ))
            elif args.command == "step-begin":
                _print(runtime.begin_step(
                    args.objective,
                    intent=args.intent,
                    milestone_id=args.milestone,
                    preconditions=args.precondition,
                    expected_evidence=args.expect,
                ))
            elif args.command == "step-evidence":
                _print(runtime.record_evidence(
                    args.criterion,
                    args.summary,
                    source=args.source,
                    status=args.status,
                    verified=args.verified,
                ))
            elif args.command == "step-finish":
                runtime.finish_step(
                    args.outcome,
                    learnings=args.learning,
                    milestone_done=args.milestone_done,
                    override_reason=args.override_reason,
                )
                _print(runtime.status())
            elif args.command == "step-fail":
                runtime.fail_step(args.reason, args.classification)
                _print(runtime.status())
        return 0
    except (ContinuityError, ValueError) as exc:
        print(f"continuity: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
