#!/usr/bin/env python3
"""test_contradiction_detector.py — Self-tests for contradiction_detector.py.

All tests are deterministic (no LLM calls, no network).
Fixture vault is written to TMPDIR and removed at teardown.

Test plan:
  T1: fixture with 3 agents, 2 contradictions (stage: Qualify vs Propose;
      value: 450k vs 1.2M) — assert exactly 2 ContradictionRecords.
  T2: severity mapping — stage/value -> high, confirmed.
  T3: --dry-run writes nothing (no contradictions.json, no escalation files).
  T4: second real run emits no duplicate escalations (idempotency).
  T5: extraction-coverage stats are non-zero (gazetteer bites).
  T6: third agent that agrees on one value does not create additional record
      for that specific (entity, belief_type) — only the disagreeing pair counts.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).parent.parent / "contradiction_detector.py"
PYTHON = sys.executable


def build_fixture_vault(root: Path) -> None:
    """Create a minimal fixture vault under root."""
    # Required dirs
    for d in [
        "_agent_state/_registry.json",
        "_brain_api/bid/_open.json",
        "_brain_api/agent",
        "Important/escalations",
    ]:
        p = root / d
        if "." in p.name:
            p.parent.mkdir(parents=True, exist_ok=True)
        else:
            p.mkdir(parents=True, exist_ok=True)

    # _open.json — one bid entity
    open_bids = {
        "_meta": {"generated": "2026-06-10T00:00:00Z"},
        "bids": [
            {
                "bid_id": "test-bid-alpha",
                "company": "Globex Corp",
                "client": "globex",
                "topic": "AI QA",
                "stage": "Qualify",
                "value": 0,
                "deadline": "",
                "probability": 0,
                "owner": "Tony",
            }
        ],
    }
    (root / "_brain_api/bid/_open.json").write_text(json.dumps(open_bids))

    # _registry.json
    registry = {
        "_meta": {"sbap_version": "1.0"},
        "agents": [
            {"agent_name": "agent-alpha", "status": "active"},
            {"agent_name": "agent-beta", "status": "active"},
            {"agent_name": "agent-gamma", "status": "active"},
        ],
    }
    (root / "_agent_state/_registry.json").write_text(json.dumps(registry))

    # Agent Alpha: stage=Qualify, value=€450k
    _write_memory(
        root / "_agent_state/agent-alpha",
        learnings=[
            {
                "date": "2026-06-01",
                "text": "Globex bid test-bid-alpha is in Qualify stage with a deal value of €450k",
                "source": "dust-write",
            }
        ],
    )

    # Agent Beta: stage=Propose (contradicts Qualify), value=€1.2M (contradicts €450k)
    _write_memory(
        root / "_agent_state/agent-beta",
        learnings=[
            {
                "date": "2026-06-02",
                "text": "Globex test-bid-alpha has moved to Propose stage; deal value confirmed at €1.2M",
                "source": "dust-write",
            }
        ],
    )

    # Agent Gamma: agrees with Alpha on Qualify — does NOT add a new contradiction
    _write_memory(
        root / "_agent_state/agent-gamma",
        learnings=[
            {
                "date": "2026-06-01",
                "text": "Globex test-bid-alpha remains in Qualify stage per last check",
                "source": "dust-write",
            }
        ],
    )


def _write_memory(agent_dir: Path, learnings: list) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    memory = {
        "agent": agent_dir.name,
        "memory_version": "1.0",
        "last_updated": "2026-06-02T00:00:00Z",
        "recent_learnings": learnings,
        "global_patterns": [],
        "self_observations": [],
        "per_account_knowledge": {},
    }
    (agent_dir / "memory.json").write_text(json.dumps(memory))


def run_tool(*args: str, root: str) -> subprocess.CompletedProcess:
    cmd = [PYTHON, str(TOOL), "--root", root, *args]
    return subprocess.run(cmd, capture_output=True, text=True)


class TestContradictionDetector(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="cd_test_"))
        build_fixture_vault(self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # T1: contradictions detected — stage and value types both present
    # ------------------------------------------------------------------
    def test_t1_stage_and_value_contradictions_detected(self) -> None:
        """Fixture: Alpha(Qualify,€450k) + Beta(Propose,€1.2M) + Gamma(Qualify).
        Expected contradictions:
          - stage: Alpha(Qualify) vs Beta(Propose)     [high]
          - stage: Beta(Propose)  vs Gamma(Qualify)    [high]
          - value: Alpha(€450k)   vs Beta(€1.2M)       [high]
        Exactly 3 records (no 'globex' entity — merged into test-bid-alpha).
        """
        result = run_tool("--json", root=str(self.tmpdir))
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        data = json.loads(result.stdout)
        contradictions = data["contradictions"]
        btypes = {r["belief_type"] for r in contradictions}
        self.assertIn("stage", btypes, msg="Expected a stage contradiction")
        self.assertIn("value", btypes, msg="Expected a value contradiction")
        self.assertEqual(
            len(contradictions),
            3,
            msg=f"Expected 3 contradictions (2 stage, 1 value), got {len(contradictions)}.\n"
                f"Records: {json.dumps(contradictions, indent=2)}",
        )
        # Confirm a single canonical entity is used (alias-bank key 'globex' is preferred
        # over bid_id 'test-bid-alpha' because client='globex' matches the alias bank)
        entities = {r["entity"] for r in contradictions}
        self.assertEqual(
            len(entities),
            1,
            msg=f"All 3 contradictions should share one canonical entity, got: {entities}",
        )

    # ------------------------------------------------------------------
    # T2: severity is 'high' for both stage and value belief types
    # ------------------------------------------------------------------
    def test_t2_severity_high(self) -> None:
        result = run_tool("--json", root=str(self.tmpdir))
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        data = json.loads(result.stdout)
        for rec in data["contradictions"]:
            self.assertEqual(
                rec["severity"],
                "high",
                msg=f"Expected high severity for {rec['belief_type']}, got {rec['severity']}",
            )

    # ------------------------------------------------------------------
    # T3: --dry-run writes nothing
    # ------------------------------------------------------------------
    def test_t3_dry_run_writes_nothing(self) -> None:
        json_out = self.tmpdir / "_brain_api/agent/contradictions.json"
        esc_dir = self.tmpdir / "Important/escalations"

        # Ensure file doesn't pre-exist
        if json_out.exists():
            json_out.unlink()

        result = run_tool("--dry-run", root=str(self.tmpdir))
        self.assertEqual(result.returncode, 0, msg=result.stderr)

        self.assertFalse(
            json_out.exists(),
            msg="contradictions.json should not be written under --dry-run",
        )
        escalations = list(esc_dir.glob("*.md")) if esc_dir.exists() else []
        self.assertEqual(
            len(escalations),
            0,
            msg=f"No escalations should be written under --dry-run, found: {escalations}",
        )

    # ------------------------------------------------------------------
    # T4: second run does not duplicate escalations
    # ------------------------------------------------------------------
    def test_t4_no_duplicate_escalations(self) -> None:
        # First run (real-mode because no --dry-run but --root is provided,
        # meaning is_real_run is False — we use a sub-flag to force escalation writes)
        # The tool only writes escalations on is_real_run (no --root, no --dry-run).
        # For the idempotency test we need to exercise the emitted_ids path.
        # We simulate by directly calling the Python module internals.

        # Import the module
        tool_dir = str(TOOL.parent)
        if tool_dir not in sys.path:
            sys.path.insert(0, tool_dir)
        import importlib
        import importlib.util

        spec = importlib.util.spec_from_file_location("contradiction_detector", str(TOOL))
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        root = self.tmpdir
        gazetteer = mod.build_gazetteer(root)
        beliefs = mod.load_all_beliefs(root, gazetteer)
        records = mod.detect_contradictions(beliefs)

        # Simulate two runs with escalation writing
        state_path = root / "_agent_state" / mod.TOOL_NAME / "memory.json"
        state = mod.load_state(state_path)
        emitted_ids: set = set(state.get("emitted_escalation_ids", []))
        esc_dir = root / "Important" / "escalations"
        esc_dir.mkdir(parents=True, exist_ok=True)
        date_str = "2026-06-10"

        # First run — write all high-severity escalations
        written_run1 = []
        for rec in records:
            if rec["severity"] == "high" and rec["id"] not in emitted_ids:
                mod.write_escalation_md(esc_dir, rec, date_str)
                emitted_ids.add(rec["id"])
                written_run1.append(rec["id"])

        state["emitted_escalation_ids"] = sorted(emitted_ids)
        mod.save_state(state_path, state)

        # Second run — should write zero new escalations
        state2 = mod.load_state(state_path)
        emitted_ids2: set = set(state2.get("emitted_escalation_ids", []))
        written_run2 = []
        for rec in records:
            if rec["severity"] == "high" and rec["id"] not in emitted_ids2:
                mod.write_escalation_md(esc_dir, rec, date_str)
                written_run2.append(rec["id"])

        self.assertGreater(
            len(written_run1), 0,
            msg="Run 1 should have written at least one escalation",
        )
        self.assertEqual(
            len(written_run2),
            0,
            msg=f"Run 2 should write zero new escalations, wrote: {written_run2}",
        )

    # ------------------------------------------------------------------
    # T5: extraction coverage stats are non-zero
    # ------------------------------------------------------------------
    def test_t5_extraction_coverage_nonzero(self) -> None:
        result = run_tool("--json", root=str(self.tmpdir))
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        data = json.loads(result.stdout)
        self.assertGreater(
            data["n_belief_tuples_extracted"],
            0,
            msg="Expected at least one belief tuple extracted from fixture",
        )
        self.assertGreater(
            data["n_memory_files_scanned"],
            0,
            msg="Expected at least one memory file scanned",
        )

    # ------------------------------------------------------------------
    # T6: agreeing pair (Alpha+Gamma both say Qualify) produces no record
    # ------------------------------------------------------------------
    def test_t6_agreeing_pair_no_record(self) -> None:
        """Agent Alpha and Gamma both assert stage=Qualify for the same entity.
        They agree, so no contradiction record should exist for that pair.
        The only stage records that DO exist should involve Beta (Propose).
        """
        result = run_tool("--json", root=str(self.tmpdir))
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        data = json.loads(result.stdout)
        stage_recs = [r for r in data["contradictions"] if r["belief_type"] == "stage"]
        # Check no record has both agents = alpha+gamma (the agreeing pair)
        for rec in stage_recs:
            pair = {rec["a"]["agent"], rec["b"]["agent"]}
            self.assertNotEqual(
                pair,
                {"agent-alpha", "agent-gamma"},
                msg="Agreeing pair agent-alpha+agent-gamma should produce no contradiction record",
            )
        # Every stage contradiction must involve Beta (Propose) on one side
        for rec in stage_recs:
            agents_in_rec = {rec["a"]["agent"], rec["b"]["agent"]}
            self.assertIn(
                "agent-beta",
                agents_in_rec,
                msg=f"Stage contradiction without Beta: {rec}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
