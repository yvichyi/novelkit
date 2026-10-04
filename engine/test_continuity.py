"""Offline regression tests for the model-agnostic Continuity runtime."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from continuity.evidence import evaluate_evidence_gate
from continuity.novelkit import bootstrap_novelkit
from continuity.runtime import ConflictError, Continuity, ContinuityError
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

    def test_step_cannot_self_certify_expected_evidence(self) -> None:
        _tmp, runtime = self.make_runtime()
        milestone = runtime.add_milestone(
            "Parser",
            criteria=["parser regression suite passes"],
            activate=True,
        )
        step = runtime.begin_step(
            "Implement error recovery",
            milestone_id=milestone["id"],
            expected_evidence=["parser regression suite passes"],
        )
        self.assertEqual(runtime.status()["current_step"]["id"], step["id"])

        with self.assertRaises(ContinuityError):
            runtime.finish_step("I think it works")

        runtime.record_evidence(
            "parser regression suite passes",
            "Agent says tests look fine",
            source="agent",
            verified=False,
        )
        with self.assertRaises(ContinuityError):
            runtime.finish_step("Still not independently verified")

        runtime.record_evidence(
            "parser regression suite passes",
            "24 parser tests passed",
            source="ci",
            status="pass",
            verified=True,
        )
        runtime.finish_step(
            "Recovery implemented and externally verified",
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
        event = runtime.workspace.recent_events(1)[0]
        self.assertEqual(event["type"], "step_finished")
        self.assertTrue(event["payload"]["evidence_gate"]["passed"])


    def test_milestone_completion_requires_its_own_criteria(self) -> None:
        _tmp, runtime = self.make_runtime()
        milestone = runtime.add_milestone(
            "Release",
            criteria=["full regression suite passes", "package smoke test passes"],
            activate=True,
        )
        runtime.begin_step(
            "Prepare release",
            milestone_id=milestone["id"],
            expected_evidence=["full regression suite passes"],
        )
        runtime.record_evidence(
            "full regression suite passes",
            "all tests green",
            source="ci",
            verified=True,
        )
        with self.assertRaises(ContinuityError):
            runtime.finish_step("Release prepared", milestone_done=True)
        runtime.record_evidence(
            "package smoke test passes",
            "wheel installed and CLI executed",
            source="package-job",
            verified=True,
        )
        runtime.finish_step("Release prepared", milestone_done=True)
        self.assertEqual(runtime.state()["milestones"][0]["status"], "done")

    def test_step_must_bind_to_active_milestone(self) -> None:
        _tmp, runtime = self.make_runtime()
        pending = runtime.add_milestone("Later")
        with self.assertRaises(ContinuityError):
            runtime.begin_step("Premature work", milestone_id=pending["id"])

    def test_cannot_switch_milestone_under_running_step(self) -> None:
        _tmp, runtime = self.make_runtime()
        first = runtime.add_milestone("First", activate=True)
        second = runtime.add_milestone("Second")
        runtime.begin_step("Work", milestone_id=first["id"])
        with self.assertRaises(ContinuityError):
            runtime.activate_milestone(second["id"])

    def test_agent_source_cannot_mark_itself_verified(self) -> None:
        _tmp, runtime = self.make_runtime()
        runtime.begin_step("Run check", expected_evidence=["check passes"])
        with self.assertRaises(ValueError):
            runtime.record_evidence(
                "check passes",
                "trust me",
                source="agent",
                verified=True,
            )

    def test_latest_verified_evidence_wins(self) -> None:
        records = [
            {
                "id": "e1", "criterion": "tests", "summary": "failed",
                "source": "ci", "status": "fail", "verified": True,
            },
            {
                "id": "e2", "criterion": "tests", "summary": "passed after repair",
                "source": "ci", "status": "pass", "verified": True,
            },
        ]
        gate = evaluate_evidence_gate(["tests"], records)
        self.assertTrue(gate["passed"])
        self.assertEqual(gate["criteria"][0]["evidence_id"], "e2")

    def test_override_is_explicit_in_ledger(self) -> None:
        _tmp, runtime = self.make_runtime()
        runtime.begin_step("Emergency repair", expected_evidence=["full suite passes"])
        runtime.finish_step(
            "Applied emergency repair",
            override_reason="CI service unavailable; human approved temporary bypass.",
        )
        event = runtime.workspace.recent_events(1)[0]
        self.assertEqual(event["type"], "step_finished")
        self.assertFalse(event["payload"]["evidence_gate"]["passed"])
        self.assertIn("human approved", event["payload"]["override_reason"])

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

    def test_context_window_is_bounded_while_obligations_survive(self) -> None:
        _tmp, runtime = self.make_runtime()
        runtime.add_constraint("Never drop the compatibility contract.")
        runtime.add_commitment("Keep API v1 working.")
        for i in range(40):
            runtime.begin_step(f"Iteration {i}")
            runtime.finish_step(f"Completed iteration {i}")
        pack = runtime.context_pack(recent_events=3, fact_limit=0)
        self.assertEqual(len(pack["recent_events"]), 3)
        self.assertEqual(pack["known_facts"], [])
        self.assertEqual(len(pack["active_constraints"]), 1)
        self.assertEqual(len(pack["open_commitments"]), 1)
        self.assertEqual(runtime.workspace.recent_events(0), [])

    def test_cli_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli_path = Path(__file__).resolve().parent / "continuity_cli.py"
            proc = subprocess.run(
                [
                    sys.executable, str(cli_path), "init", tmp,
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
                [sys.executable, str(cli_path), "verify", tmp],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertTrue(json.loads(proc.stdout)["ok"])


    def test_cli_can_close_long_lived_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli_path = Path(__file__).resolve().parent / "continuity_cli.py"

            def run(*args: str) -> dict[str, object]:
                proc = subprocess.run(
                    [sys.executable, str(cli_path), *args],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return json.loads(proc.stdout)

            run(
                "init", tmp,
                "--title", "Lifecycle",
                "--objective", "Exercise the complete long-lived CLI state lifecycle.",
            )
            milestone = run(
                "milestone", tmp, "Second phase",
                "--criterion", "phase check passes",
            )
            commitment = run("commitment", tmp, "Publish migration note.")
            question = run("question", tmp, "Which schema is canonical?")
            resource = run(
                "resource", tmp, "docs/schema.md",
                "--role", "source-of-truth",
                "--note", "Canonical schema",
            )
            self.assertEqual(resource["role"], "source-of-truth")

            status = run("milestone-activate", tmp, milestone["id"])
            self.assertEqual(status["active_milestone"]["id"], milestone["id"])

            status = run(
                "commitment-close", tmp, commitment["id"], "--status", "done"
            )
            self.assertEqual(status["open_commitments"], 0)

            status = run(
                "question-resolve", tmp, question["id"], "schema/v1"
            )
            self.assertEqual(status["open_questions"], 0)

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
            (config / "redlines.json").write_text(json.dumps({"BANNED_WORDS": ["forbidden"]}), encoding="utf-8")
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
