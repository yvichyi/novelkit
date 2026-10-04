"""Offline regression tests for the model-agnostic Continuity runtime."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from continuity.novelkit import bootstrap_novelkit
from continuity.runtime import ConflictError, Continuity
from continuity.store import CorruptLedgerError


class ContinuityTests(unittest.TestCase):
    def make_runtime(self) -> tuple[tempfile.TemporaryDirectory, Continuity]:
        tmp = tempfile.TemporaryDirectory()
        runtime = Continuity.create(
            tmp.name,
            "Ship a compiler",
            "Build and verify a small compiler without losing long-horizon constraints.",
            "software",
        )
        self.addCleanup(tmp.cleanup)
        return tmp, runtime

    def test_goal_constraints_milestones_compile_into_context(self) -> None:
        _tmp, runtime = self.make_runtime()
        runtime.add_constraint("Never delete user-owned source.", "blocking")
        milestone = runtime.add_milestone(
            "Parser",
            ["All parser tests pass", "Syntax errors are recoverable"],
            activate=True,
        )
        runtime.add_commitment("Preserve the old AST JSON shape.")
        runtime.add_question("Does recovery preserve source spans?")
        pack = runtime.context_pack()
        self.assertEqual(pack["schema_version"], "continuity.context/v1")
        self.assertEqual(pack["active_milestone"]["id"], milestone["id"])
        self.assertEqual(len(pack["active_constraints"]), 1)
        self.assertEqual(len(pack["open_commitments"]), 1)
        self.assertEqual(len(pack["open_questions"]), 1)
        self.assertEqual(len(pack["context_hash"]), 64)

    def test_step_lifecycle_preserves_evidence_and_learning(self) -> None:
        _tmp, runtime = self.make_runtime()
        milestone = runtime.add_milestone("Parser", activate=True)
        step = runtime.begin_step(
            "Implement error recovery",
            milestone_id=milestone["id"],
            expected_evidence=["parser regression suite passes"],
        )
        self.assertEqual(runtime.status()["current_step"]["id"], step["id"])
        runtime.finish_step(
            "Recovery implemented",
            evidence=["24 parser tests passed"],
            learnings=["Recovery must retain the original token span."],
            milestone_done=True,
        )
        state = runtime.state()
        self.assertIsNone(state["current_step"])
        self.assertEqual(state["milestones"][0]["status"], "done")
        self.assertEqual(
            state["facts"][-1]["statement"],
            "Recovery must retain the original token span.",
        )
        events = runtime.workspace.recent_events(2)
        self.assertEqual(events[-1]["type"], "step_finished")
        self.assertEqual(events[-1]["payload"]["evidence"], ["24 parser tests passed"])

    def test_ledger_is_hash_chained_and_tampering_is_detected(self) -> None:
        tmp, runtime = self.make_runtime()
        runtime.add_constraint("Keep outputs deterministic.")
        self.assertTrue(runtime.verify()["ok"])
        ledger = Path(tmp.name) / ".continuity" / "ledger.jsonl"
        lines = ledger.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[-1])
        event["payload"]["constraint"]["text"] = "tampered"
        lines[-1] = json.dumps(event, ensure_ascii=False, sort_keys=True)
        ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(CorruptLedgerError):
            runtime.verify()

    def test_state_projection_repairs_from_authoritative_ledger(self) -> None:
        tmp, runtime = self.make_runtime()
        runtime.add_commitment("Keep compatibility.")
        state_path = Path(tmp.name) / ".continuity" / "state.json"
        state_path.write_text('{"revision": -999}\n', encoding="utf-8")
        state = runtime.state()
        self.assertEqual(state["revision"], 1)
        repaired = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(repaired["revision"], 1)

    def test_optimistic_revision_conflict_is_rejected(self) -> None:
        _tmp, runtime = self.make_runtime()
        state = runtime.state()
        next_state = json.loads(json.dumps(state))
        runtime.add_question("What remains?")
        with self.assertRaises(ConflictError):
            runtime.workspace.commit(
                next_state,
                "stale_write",
                {},
                "2026-10-04T00:00:00Z",
                expected_revision=state["revision"],
            )

    def test_cli_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(__file__).resolve().parent / "continuity_cli.py"
            proc = subprocess.run(
                [
                    sys.executable, str(cli), "init", tmp,
                    "--title", "Research",
                    "--objective", "Produce an evidence-backed answer.",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(proc.stdout)
            self.assertEqual(data["project"]["title"], "Research")
            proc = subprocess.run(
                [sys.executable, str(cli), "verify", tmp],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertTrue(json.loads(proc.stdout)["ok"])

    def test_novelkit_adapter_maps_domain_state_without_touching_story_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            config = book / "novel_config"
            ledger = book / "ledger"
            published = book / "已发布正文"
            config.mkdir()
            ledger.mkdir()
            published.mkdir()
            (config / "world_setting.md").write_text("world", encoding="utf-8")
            (config / "redline_table.md").write_text("redline", encoding="utf-8")
            (config / "checklist.md").write_text("canon", encoding="utf-8")
            (config / "anchors.json").write_text(
                json.dumps({
                    "STAGE_ANCHORS": [
                        ["开端", 1, 2, 1, 10],
                        ["发展", 3, 4, 11, 20],
                    ]
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            (ledger / "伏笔账本.md").write_text("pending", encoding="utf-8")
            (published / "第1章.md").write_text("第1章", encoding="utf-8")
            runtime = bootstrap_novelkit(book)
            pack = runtime.context_pack()
            self.assertEqual(pack["project"]["domain"], "novel")
            self.assertEqual(pack["active_milestone"]["title"], "开端")
            self.assertEqual(len(pack["active_constraints"]), 2)
            self.assertEqual(len(pack["open_commitments"]), 1)


if __name__ == "__main__":
    unittest.main()
