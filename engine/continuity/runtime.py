"""Semantic task state for agents that operate across many runs."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .evidence import evaluate_evidence_gate, make_evidence
from .store import ConflictError, ContinuityError, Workspace

STATE_SCHEMA = "continuity.state/v1"
CONTEXT_SCHEMA = "continuity.context/v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


def _hash(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def new_state(
    title: str,
    objective: str,
    domain: str = "generic",
) -> dict[str, object]:
    now = _now()
    return {
        "schema_version": STATE_SCHEMA,
        "revision": 0,
        "project": {
            "id": _id("project"),
            "title": title.strip(),
            "objective": objective.strip(),
            "domain": domain.strip() or "generic",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        },
        "constraints": [],
        "milestones": [],
        "commitments": [],
        "questions": [],
        "facts": [],
        "resources": [],
        "current_step": None,
    }


def validate_state(state: dict[str, object]) -> list[str]:
    problems: list[str] = []
    if state.get("schema_version") != STATE_SCHEMA:
        problems.append("unsupported state schema")
    if not isinstance(state.get("revision"), int):
        problems.append("revision must be an integer")

    project = state.get("project")
    if not isinstance(project, dict):
        problems.append("project must be an object")
    else:
        if not str(project.get("title", "")).strip():
            problems.append("project title is empty")
        if not str(project.get("objective", "")).strip():
            problems.append("project objective is empty")

    ids: set[str] = set()
    for bucket in (
        "constraints", "milestones", "commitments",
        "questions", "facts", "resources",
    ):
        items = state.get(bucket)
        if not isinstance(items, list):
            problems.append(f"{bucket} must be a list")
            continue
        for item in items:
            if not isinstance(item, dict):
                problems.append(f"{bucket} contains a non-object")
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                problems.append(f"{bucket} item has no id")
            elif item_id in ids:
                problems.append(f"duplicate id: {item_id}")
            else:
                ids.add(item_id)

    milestones = state.get("milestones", [])
    active = [
        m for m in milestones
        if isinstance(m, dict) and m.get("status") == "active"
    ]
    if len(active) > 1:
        problems.append("more than one milestone is active")

    step = state.get("current_step")
    if step is not None and not isinstance(step, dict):
        problems.append("current_step must be null or an object")
    if isinstance(step, dict) and step.get("milestone_id"):
        known = {
            m.get("id") for m in milestones
            if isinstance(m, dict)
        }
        if step["milestone_id"] not in known:
            problems.append("current_step references an unknown milestone")

    return problems


class Continuity:
    """Model-agnostic long-horizon semantic state.

    This class does not run an LLM or tools. It tracks what an agent is trying
    to accomplish, what must remain true, open commitments/questions, evidence,
    and the currently active step. That makes it complementary to conversation
    memory and durable workflow engines rather than a replacement for them.
    """

    def __init__(self, root: str | Path):
        self.workspace = Workspace(root)

    @classmethod
    def create(
        cls,
        root: str | Path,
        title: str,
        objective: str,
        domain: str = "generic",
    ) -> "Continuity":
        runtime = cls(root)
        state = new_state(title, objective, domain)
        problems = validate_state(state)
        if problems:
            raise ContinuityError("; ".join(problems))
        runtime.workspace.create(state, timestamp=_now())
        return runtime

    @property
    def root(self) -> Path:
        return self.workspace.root

    def state(self) -> dict[str, object]:
        state = self.workspace.load()
        problems = validate_state(state)
        if problems:
            raise ContinuityError("invalid state: " + "; ".join(problems))
        return state

    def _mutate(
        self,
        event_type: str,
        payload: dict[str, object],
        change: Callable[[dict[str, object]], None],
    ) -> dict[str, object]:
        current = self.state()
        expected_revision = int(current["revision"])
        next_state = copy.deepcopy(current)
        change(next_state)
        problems = validate_state(next_state)
        if problems:
            raise ContinuityError("invalid transition: " + "; ".join(problems))
        return self.workspace.commit(
            next_state,
            event_type=event_type,
            payload=payload,
            timestamp=_now(),
            expected_revision=expected_revision,
        )

    def add_constraint(
        self,
        text: str,
        severity: str = "blocking",
        source: str = "user",
    ) -> dict[str, object]:
        if severity not in {"blocking", "important", "advisory"}:
            raise ValueError("severity must be blocking, important, or advisory")
        item = {
            "id": _id("constraint"),
            "text": text.strip(),
            "severity": severity,
            "source": source,
            "status": "active",
            "created_at": _now(),
        }
        if not item["text"]:
            raise ValueError("constraint text cannot be empty")

        def change(state: dict[str, object]) -> None:
            state["constraints"].append(item)  # type: ignore[union-attr]

        self._mutate("constraint_added", {"constraint": item}, change)
        return copy.deepcopy(item)

    def add_milestone(
        self,
        title: str,
        criteria: list[str] | None = None,
        activate: bool = False,
    ) -> dict[str, object]:
        item = {
            "id": _id("milestone"),
            "title": title.strip(),
            "criteria": [c.strip() for c in (criteria or []) if c.strip()],
            "status": "active" if activate else "pending",
            "created_at": _now(),
            "completed_at": None,
        }
        if not item["title"]:
            raise ValueError("milestone title cannot be empty")

        def change(state: dict[str, object]) -> None:
            if activate and state["current_step"] is not None:
                raise ContinuityError(
                    "cannot switch active milestone while a step is running"
                )
            milestones = state["milestones"]  # type: ignore[assignment]
            if activate:
                for milestone in milestones:
                    if milestone["status"] == "active":
                        milestone["status"] = "pending"
            milestones.append(item)

        self._mutate("milestone_added", {"milestone": item}, change)
        return copy.deepcopy(item)

    def activate_milestone(self, milestone_id: str) -> None:
        def change(state: dict[str, object]) -> None:
            step = state.get("current_step")
            if (
                isinstance(step, dict)
                and step.get("milestone_id")
                and step.get("milestone_id") != milestone_id
            ):
                raise ContinuityError(
                    "cannot switch milestones while a step is attached "
                    "to another milestone"
                )
            found = False
            for milestone in state["milestones"]:  # type: ignore[union-attr]
                if milestone["id"] == milestone_id:
                    if milestone["status"] == "done":
                        raise ContinuityError("completed milestone cannot be reactivated")
                    milestone["status"] = "active"
                    found = True
                elif milestone["status"] == "active":
                    milestone["status"] = "pending"
            if not found:
                raise ContinuityError(f"unknown milestone: {milestone_id}")

        self._mutate(
            "milestone_activated",
            {"milestone_id": milestone_id},
            change,
        )

    def add_commitment(
        self,
        text: str,
        source: str = "agent",
    ) -> dict[str, object]:
        item = {
            "id": _id("commitment"),
            "text": text.strip(),
            "source": source,
            "status": "open",
            "created_at": _now(),
            "closed_at": None,
        }
        if not item["text"]:
            raise ValueError("commitment text cannot be empty")

        def change(state: dict[str, object]) -> None:
            state["commitments"].append(item)  # type: ignore[union-attr]

        self._mutate("commitment_added", {"commitment": item}, change)
        return copy.deepcopy(item)

    def close_commitment(self, commitment_id: str, status: str = "done") -> None:
        if status not in {"done", "dropped"}:
            raise ValueError("commitment status must be done or dropped")

        def change(state: dict[str, object]) -> None:
            for item in state["commitments"]:  # type: ignore[union-attr]
                if item["id"] == commitment_id:
                    item["status"] = status
                    item["closed_at"] = _now()
                    return
            raise ContinuityError(f"unknown commitment: {commitment_id}")

        self._mutate(
            "commitment_closed",
            {"commitment_id": commitment_id, "status": status},
            change,
        )

    def add_question(self, text: str, source: str = "agent") -> dict[str, object]:
        item = {
            "id": _id("question"),
            "text": text.strip(),
            "source": source,
            "status": "open",
            "answer": None,
            "created_at": _now(),
            "resolved_at": None,
        }
        if not item["text"]:
            raise ValueError("question text cannot be empty")

        def change(state: dict[str, object]) -> None:
            state["questions"].append(item)  # type: ignore[union-attr]

        self._mutate("question_added", {"question": item}, change)
        return copy.deepcopy(item)

    def resolve_question(self, question_id: str, answer: str) -> None:
        def change(state: dict[str, object]) -> None:
            for item in state["questions"]:  # type: ignore[union-attr]
                if item["id"] == question_id:
                    item["status"] = "resolved"
                    item["answer"] = answer.strip()
                    item["resolved_at"] = _now()
                    return
            raise ContinuityError(f"unknown question: {question_id}")

        self._mutate(
            "question_resolved",
            {"question_id": question_id, "answer": answer.strip()},
            change,
        )

    def add_resource(
        self,
        path: str,
        role: str = "reference",
        note: str = "",
    ) -> dict[str, object]:
        item = {
            "id": _id("resource"),
            "path": path,
            "role": role,
            "note": note,
            "created_at": _now(),
        }

        def change(state: dict[str, object]) -> None:
            state["resources"].append(item)  # type: ignore[union-attr]

        self._mutate("resource_added", {"resource": item}, change)
        return copy.deepcopy(item)

    def begin_step(
        self,
        objective: str,
        intent: str = "execute",
        milestone_id: str | None = None,
        preconditions: list[str] | None = None,
        expected_evidence: list[str] | None = None,
    ) -> dict[str, object]:
        current = self.state()
        if current["current_step"] is not None:
            raise ContinuityError("another step is already active")
        if milestone_id:
            matched = next(
                (
                    m for m in current["milestones"]  # type: ignore[index]
                    if m["id"] == milestone_id
                ),
                None,
            )
            if matched is None:
                raise ContinuityError(f"unknown milestone: {milestone_id}")
            if matched["status"] != "active":
                raise ContinuityError(
                    "a step can only attach to the active milestone"
                )

        context = self.context_pack()
        item = {
            "id": _id("step"),
            "objective": objective.strip(),
            "intent": intent.strip() or "execute",
            "milestone_id": milestone_id,
            "preconditions": [
                x.strip() for x in (preconditions or []) if x.strip()
            ],
            "expected_evidence": [
                x.strip() for x in (expected_evidence or []) if x.strip()
            ],
            "started_at": _now(),
            "baseline_revision": current["revision"],
            "baseline_context_hash": context["context_hash"],
        }
        if not item["objective"]:
            raise ValueError("step objective cannot be empty")

        def change(state: dict[str, object]) -> None:
            state["current_step"] = item

        self._mutate("step_started", {"step": item}, change)
        return copy.deepcopy(item)

    def record_evidence(
        self,
        criterion: str,
        summary: str,
        source: str = "external",
        status: str = "pass",
        verified: bool = False,
    ) -> dict[str, object]:
        current = self.state()
        step = current.get("current_step")
        if not isinstance(step, dict):
            raise ContinuityError("no active step")
        item = make_evidence(
            evidence_id=_id("evidence"),
            criterion=criterion,
            summary=summary,
            source=source,
            status=status,
            verified=verified,
            created_at=_now(),
        )

        def change(state: dict[str, object]) -> None:
            active = state["current_step"]
            if not isinstance(active, dict):
                raise ContinuityError("active step disappeared")
            active.setdefault("evidence", []).append(item)

        self._mutate("evidence_recorded", {"evidence": item}, change)
        return copy.deepcopy(item)

    def finish_step(
        self,
        outcome: str,
        learnings: list[str] | None = None,
        milestone_done: bool = False,
        override_reason: str | None = None,
    ) -> None:
        outcome = outcome.strip()
        if not outcome:
            raise ValueError("step outcome cannot be empty")
        current = self.state()
        step = current.get("current_step")
        if not isinstance(step, dict):
            raise ContinuityError("no active step")

        gate = evaluate_evidence_gate(
            step.get("expected_evidence", []),
            step.get("evidence", []),
        )
        override = (override_reason or "").strip()
        if not gate["passed"] and not override:
            missing = ", ".join(gate["missing"]) or "none"
            failed = ", ".join(gate["failed"]) or "none"
            raise ContinuityError(
                "evidence gate not satisfied "
                f"(missing: {missing}; failed: {failed})"
            )

        milestone_gate: dict[str, object] | None = None
        if milestone_done:
            milestone_id = step.get("milestone_id")
            if not milestone_id:
                raise ContinuityError(
                    "cannot complete a milestone from an unbound step"
                )
            milestone = next(
                (
                    m for m in current["milestones"]  # type: ignore[index]
                    if m["id"] == milestone_id
                ),
                None,
            )
            if not isinstance(milestone, dict):
                raise ContinuityError("step milestone no longer exists")
            milestone_gate = evaluate_evidence_gate(
                milestone.get("criteria", []),
                step.get("evidence", []),
            )
            if not milestone_gate["passed"] and not override:
                missing = ", ".join(milestone_gate["missing"]) or "none"
                failed = ", ".join(milestone_gate["failed"]) or "none"
                raise ContinuityError(
                    "milestone evidence gate not satisfied "
                    f"(missing: {missing}; failed: {failed})"
                )

        learnings = [x.strip() for x in (learnings or []) if x.strip()]

        def change(state: dict[str, object]) -> None:
            active = state["current_step"]
            if not isinstance(active, dict):
                raise ContinuityError("active step disappeared")
            step_id = active["id"]
            for statement in learnings:
                state["facts"].append({  # type: ignore[union-attr]
                    "id": _id("fact"),
                    "statement": statement,
                    "source_step": step_id,
                    "created_at": _now(),
                })
            if milestone_done and active.get("milestone_id"):
                for milestone in state["milestones"]:  # type: ignore[union-attr]
                    if milestone["id"] == active["milestone_id"]:
                        milestone["status"] = "done"
                        milestone["completed_at"] = _now()
                        break
            state["current_step"] = None

        self._mutate(
            "step_finished",
            {
                "step_id": step["id"],
                "outcome": outcome,
                "evidence": copy.deepcopy(step.get("evidence", [])),
                "evidence_gate": gate,
                "milestone_evidence_gate": milestone_gate,
                "override_reason": override or None,
                "learnings": learnings,
                "milestone_done": milestone_done,
            },
            change,
        )

    def fail_step(self, reason: str, classification: str = "execution") -> None:
        reason = reason.strip()
        if not reason:
            raise ValueError("failure reason cannot be empty")
        current = self.state()
        step = current.get("current_step")
        if not isinstance(step, dict):
            raise ContinuityError("no active step")

        def change(state: dict[str, object]) -> None:
            state["current_step"] = None

        self._mutate(
            "step_failed",
            {
                "step_id": step["id"],
                "reason": reason,
                "classification": classification.strip() or "execution",
            },
            change,
        )

    def context_pack(
        self,
        recent_events: int = 8,
        fact_limit: int = 20,
    ) -> dict[str, object]:
        state = self.state()
        milestones = state["milestones"]  # type: ignore[assignment]
        active_milestone = next(
            (m for m in milestones if m["status"] == "active"),
            None,
        )
        pack: dict[str, object] = {
            "schema_version": CONTEXT_SCHEMA,
            "revision": state["revision"],
            "project": state["project"],
            "active_constraints": [
                x for x in state["constraints"]  # type: ignore[index]
                if x["status"] == "active"
            ],
            "active_milestone": active_milestone,
            "open_commitments": [
                x for x in state["commitments"]  # type: ignore[index]
                if x["status"] == "open"
            ],
            "open_questions": [
                x for x in state["questions"]  # type: ignore[index]
                if x["status"] == "open"
            ],
            "known_facts": ([] if fact_limit <= 0 else state["facts"][-fact_limit:]),  # type: ignore[index]
            "resources": state["resources"],
            "current_step": state["current_step"],
            "recent_events": self.workspace.recent_events(recent_events),
        }
        pack["context_hash"] = _hash(pack)
        return pack

    def status(self) -> dict[str, object]:
        state = self.state()
        return {
            "schema_version": STATE_SCHEMA,
            "revision": state["revision"],
            "project": state["project"],
            "active_milestone": next(
                (
                    m for m in state["milestones"]  # type: ignore[index]
                    if m["status"] == "active"
                ),
                None,
            ),
            "open_commitments": sum(
                1 for x in state["commitments"]  # type: ignore[index]
                if x["status"] == "open"
            ),
            "open_questions": sum(
                1 for x in state["questions"]  # type: ignore[index]
                if x["status"] == "open"
            ),
            "current_step": state["current_step"],
        }

    def verify(self) -> dict[str, object]:
        state = self.state()
        problems = validate_state(state)
        result = self.workspace.verify()
        result["state_schema"] = state["schema_version"]
        result["problems"] = problems
        result["ok"] = not problems
        return result
