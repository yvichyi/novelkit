"""NovelKit adapter: prove the generic runtime can describe an existing book."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .runtime import Continuity, ContinuityError


def _published_chapter_count(book_dir: Path) -> int:
    published = book_dir / "已发布正文"
    highest = 0
    if not published.is_dir():
        return 0
    for path in published.glob("第*章.md"):
        match = re.search(r"第(\d+)章", path.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def bootstrap_novelkit(book_dir: str | Path) -> Continuity:
    """Create .continuity state alongside a NovelKit book directory."""
    root = Path(book_dir).expanduser().resolve()
    config = root / "novel_config"
    if not config.is_dir():
        raise ContinuityError(f"not a NovelKit book directory: {root}")

    runtime = Continuity.create(
        root,
        title=root.name,
        objective=(
            f"Complete 《{root.name}》 across many writing runs while preserving "
            "canon, hard constraints, planned stages, and unresolved commitments."
        ),
        domain="novel",
    )

    resource_specs = [
        ("novel_config/world_setting.md", "world_state", "Current world-state source"),
        ("novel_config/outline.md", "plan", "Long-horizon story plan"),
        ("novel_config/redline_table.md", "constraint_source", "Hard writing redlines"),
        ("novel_config/checklist.md", "constraint_source", "Canon consistency checklist"),
        ("ledger/章节事件表.md", "history", "Irreversible event ledger"),
        ("ledger/伏笔账本.md", "commitment_source", "Open and recovered foreshadowing"),
    ]
    for rel, role, note in resource_specs:
        if (root / rel).exists():
            runtime.add_resource(rel, role=role, note=note)

    if (config / "redline_table.md").exists():
        runtime.add_constraint(
            "Honor every active rule in novel_config/redline_table.md.",
            severity="blocking",
            source="NovelKit redline table",
        )
    if (config / "checklist.md").exists():
        runtime.add_constraint(
            "Do not contradict established canon in novel_config/checklist.md.",
            severity="blocking",
            source="NovelKit consistency checklist",
        )

    created: list[tuple[dict[str, object], int, int]] = []
    anchors_path = config / "anchors.json"
    if anchors_path.exists():
        try:
            anchors = json.loads(anchors_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            anchors = {}
        for row in anchors.get("STAGE_ANCHORS", []):
            if not isinstance(row, list) or len(row) < 5:
                continue
            name, a_lo, a_hi, ch_lo, ch_hi = row[:5]
            if not all(isinstance(x, int) for x in (a_lo, a_hi, ch_lo, ch_hi)):
                continue
            milestone = runtime.add_milestone(
                str(name),
                criteria=[
                    f"Complete chapters {ch_lo}-{ch_hi}.",
                    f"Advance planned anchors {a_lo}-{a_hi}.",
                ],
            )
            created.append((milestone, ch_lo, ch_hi))

    next_chapter = _published_chapter_count(root) + 1
    for milestone, ch_lo, ch_hi in created:
        if ch_lo <= next_chapter <= ch_hi:
            runtime.activate_milestone(str(milestone["id"]))
            break

    if (root / "ledger" / "伏笔账本.md").exists():
        runtime.add_commitment(
            "Resolve tracked mainline foreshadowing by its planned recovery window.",
            source="NovelKit foreshadow ledger",
        )

    runtime.add_question(
        "What evidence should the next writing step produce before it is accepted into canon?",
        source="continuity bootstrap",
    )
    return runtime
