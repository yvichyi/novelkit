"""Evidence records and deterministic gates for step completion."""

from __future__ import annotations

from typing import Iterable


VALID_STATUSES = {"pass", "fail", "unknown"}
SELF_ASSERTED_SOURCES = {"agent", "model", "llm", "self"}


def make_evidence(
    *,
    evidence_id: str,
    criterion: str,
    summary: str,
    source: str,
    status: str,
    verified: bool,
    created_at: str,
) -> dict[str, object]:
    criterion = criterion.strip()
    summary = summary.strip()
    source = source.strip() or "unspecified"
    if not criterion:
        raise ValueError("evidence criterion cannot be empty")
    if not summary:
        raise ValueError("evidence summary cannot be empty")
    if status not in VALID_STATUSES:
        raise ValueError("evidence status must be pass, fail, or unknown")
    if verified and source.casefold() in SELF_ASSERTED_SOURCES:
        raise ValueError(
            "self-asserted agent/model evidence cannot be marked verified"
        )
    return {
        "id": evidence_id,
        "criterion": criterion,
        "summary": summary,
        "source": source,
        "status": status,
        "verified": bool(verified),
        "created_at": created_at,
    }


def evaluate_evidence_gate(
    expected: Iterable[str],
    records: Iterable[dict[str, object]],
) -> dict[str, object]:
    """Evaluate the latest verified record for each expected criterion."""
    criteria: list[str] = []
    seen: set[str] = set()
    for raw in expected:
        criterion = str(raw).strip()
        if criterion and criterion not in seen:
            seen.add(criterion)
            criteria.append(criterion)

    latest: dict[str, dict[str, object]] = {}
    for record in records:
        criterion = str(record.get("criterion", "")).strip()
        if criterion in seen and record.get("verified") is True:
            latest[criterion] = record

    details: list[dict[str, object]] = []
    missing: list[str] = []
    failed: list[str] = []
    for criterion in criteria:
        record = latest.get(criterion)
        if record is None:
            missing.append(criterion)
            details.append({
                "criterion": criterion,
                "status": "missing",
                "evidence_id": None,
            })
            continue
        status = str(record.get("status", "unknown"))
        details.append({
            "criterion": criterion,
            "status": status,
            "evidence_id": record.get("id"),
            "source": record.get("source"),
        })
        if status != "pass":
            failed.append(criterion)

    return {
        "passed": not missing and not failed,
        "missing": missing,
        "failed": failed,
        "criteria": details,
    }
