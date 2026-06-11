"""Tests for promise_ledger.py.

Run:
    python3 -m pytest build/tools/tests/test_promise_ledger.py -v
    # or from repo root:
    python3 build/tools/tests/test_promise_ledger.py

All tests run offline (--no-llm path only).  No live LLM calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure the tools directory is on the path
TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import promise_ledger as pl

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "promise-ledger-root"

# ---------------------------------------------------------------------------
# Helper assertions
# ---------------------------------------------------------------------------

def _run_no_llm(tmp_root: Path, **kwargs) -> dict:
    """Run the ledger pipeline with no-llm and dry_run=False against tmp_root."""
    return pl.run(root=tmp_root, no_llm=True, dry_run=False, **kwargs)


def _load_ledger(tmp_root: Path) -> tuple[list, list]:
    return pl.load_ledger(tmp_root)


# ---------------------------------------------------------------------------
# Test 1: 2+ promises extracted via --no-llm from fixture root
# ---------------------------------------------------------------------------

def test_extraction_no_llm(tmp_path: Path) -> None:
    """
    Two fixture meetings have explicit Action-items bullets.
    Expect at least 2 promises extracted (one The owner / owner-owes,
    one Globex team / owed-to-owner).
    """
    import shutil
    # Copy fixture into tmp_path so we get a clean state
    shutil.copytree(str(FIXTURE_ROOT), str(tmp_path / "vault"))
    root = tmp_path / "vault"

    stats = _run_no_llm(root)

    ledger, held = _load_ledger(root)
    all_extracted = ledger + held
    assert len(all_extracted) >= 2, (
        f"Expected >= 2 promises, got {len(all_extracted)}: "
        f"{[p['text'] for p in all_extracted]}"
    )

    types = {p["promise_type"] for p in all_extracted}
    assert "owner-owes" in types, "Expected at least one owner-owes promise"
    assert "owed-to-owner" in types, "Expected at least one owed-to-owner promise"

    # All ledger entries have confidence >= threshold
    for p in ledger:
        assert p["confidence"] >= pl.CONFIDENCE_THRESHOLD, (
            f"Ledger entry confidence too low: {p['confidence']} for: {p['text']}"
        )

    print(f"  [test_extraction_no_llm] PASS — {len(all_extracted)} promises extracted")


# ---------------------------------------------------------------------------
# Test 2: Reconcile marks the The owner P&L promise as kept
# ---------------------------------------------------------------------------

def test_reconcile_kept(tmp_path: Path) -> None:
    """
    The kickoff meeting has The owner promising to send the revised P&L.
    The follow-up meeting (06-03) contains 'sent' in the summary.
    After a second run (which reconciles), Tony's P&L promise should be 'kept'.
    """
    import shutil
    shutil.copytree(str(FIXTURE_ROOT), str(tmp_path / "vault"))
    root = tmp_path / "vault"

    # First run: extract
    _run_no_llm(root)

    # Second run: reconcile only
    pl.run(root=root, no_llm=True, dry_run=False, reconcile_only=True)

    ledger, _ = _load_ledger(root)

    # Find the The owner P&L promise
    tony_pl = [
        p for p in ledger
        if "p&l" in p["text"].lower() or "revised" in p["text"].lower()
    ]
    assert tony_pl, (
        f"The owner P&L promise not found in ledger. Ledger: {[p['text'] for p in ledger]}"
    )

    kept = [p for p in tony_pl if p["status"] == "kept"]
    assert kept, (
        f"Expected The owner P&L promise to be marked 'kept'. "
        f"Statuses: {[p['status'] for p in tony_pl]}"
    )
    print(f"  [test_reconcile_kept] PASS — The owner P&L promise marked kept")


# ---------------------------------------------------------------------------
# Test 3: Idempotency — second extraction run adds no duplicates
# ---------------------------------------------------------------------------

def test_idempotency(tmp_path: Path) -> None:
    """
    Running promise_ledger twice on the same vault should not create
    duplicate promise_ids.
    """
    import shutil
    shutil.copytree(str(FIXTURE_ROOT), str(tmp_path / "vault"))
    root = tmp_path / "vault"

    stats1 = _run_no_llm(root)
    ledger1, held1 = _load_ledger(root)
    count_after_first = len(ledger1) + len(held1)

    stats2 = _run_no_llm(root)
    ledger2, held2 = _load_ledger(root)
    count_after_second = len(ledger2) + len(held2)

    assert count_after_first == count_after_second, (
        f"Duplicate promises created on second run: "
        f"{count_after_first} -> {count_after_second}"
    )
    assert stats2["new_ledger"] == 0, (
        f"Second run should add 0 new ledger entries, got {stats2['new_ledger']}"
    )
    assert stats2["new_held"] == 0, (
        f"Second run should add 0 held entries, got {stats2['new_held']}"
    )

    # Verify no duplicate IDs
    all_ids = [p["promise_id"] for p in ledger2 + held2]
    assert len(all_ids) == len(set(all_ids)), "Duplicate promise_ids found in ledger"

    print(f"  [test_idempotency] PASS — {count_after_first} promises, no duplicates on re-run")


# ---------------------------------------------------------------------------
# Test 4: summary.json is correct
# ---------------------------------------------------------------------------

def test_summary_json(tmp_path: Path) -> None:
    """
    summary.json should have the expected keys and correct total_ledger count.
    """
    import shutil
    shutil.copytree(str(FIXTURE_ROOT), str(tmp_path / "vault"))
    root = tmp_path / "vault"

    _run_no_llm(root)

    summary_file = root / "_brain_api" / "promises" / "summary.json"
    assert summary_file.exists(), "summary.json not written"
    summary = json.loads(summary_file.read_text())

    required_keys = {"generated", "due_48h", "overdue_to_owner",
                     "oldest_unresolved", "keep_rate_30d", "total_pending", "total_ledger"}
    missing = required_keys - summary.keys()
    assert not missing, f"summary.json missing keys: {missing}"

    ledger, _ = _load_ledger(root)
    assert summary["total_ledger"] == len(ledger), (
        f"summary total_ledger={summary['total_ledger']} != actual={len(ledger)}"
    )
    print(f"  [test_summary_json] PASS — summary.json has all required keys, "
          f"total_ledger={summary['total_ledger']}")


# ---------------------------------------------------------------------------
# Test 5: dry-run writes nothing
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    """
    --dry-run should not write ledger.json, held.json, summary.json, or memory.json.
    """
    import shutil
    shutil.copytree(str(FIXTURE_ROOT), str(tmp_path / "vault"))
    root = tmp_path / "vault"

    pl.run(root=root, no_llm=True, dry_run=True)

    ledger_file = root / "_brain_api" / "promises" / "ledger.json"
    held_file = root / "_brain_api" / "promises" / "held.json"
    summary_file = root / "_brain_api" / "promises" / "summary.json"
    memory_file = root / "_agent_state" / "promise-ledger" / "memory.json"

    assert not ledger_file.exists(), "dry-run wrote ledger.json"
    assert not held_file.exists(), "dry-run wrote held.json"
    assert not summary_file.exists(), "dry-run wrote summary.json"
    assert not memory_file.exists(), "dry-run wrote memory.json"

    print("  [test_dry_run_writes_nothing] PASS — no files written in dry-run mode")


# ---------------------------------------------------------------------------
# Test 6: promise_id is stable (deterministic)
# ---------------------------------------------------------------------------

def test_promise_id_deterministic() -> None:
    id1 = pl.promise_id("meeting.md", "Send the revised P&L by Friday")
    id2 = pl.promise_id("meeting.md", "Send the revised P&L by Friday")
    assert id1 == id2, "promise_id not deterministic"
    id3 = pl.promise_id("other.md", "Send the revised P&L by Friday")
    assert id1 != id3, "promise_id should differ when meeting_ref differs"
    print(f"  [test_promise_id_deterministic] PASS — id={id1}")


# ---------------------------------------------------------------------------
# Test 7: infer_date_from_text
# ---------------------------------------------------------------------------

def test_infer_date_from_text() -> None:
    # Explicit ISO date
    result = pl.infer_date_from_text("send it by 2026-06-10", "2026-06-08")
    assert result == "2026-06-10", f"Expected 2026-06-10, got {result}"

    # Friday from a Monday base
    result = pl.infer_date_from_text("send by Friday please", "2026-06-08")  # Monday
    assert result is not None, "Expected a date for 'Friday'"
    assert result.endswith("-12"), f"Expected Friday 2026-06-12, got {result}"  # Jun 12

    # No date
    result = pl.infer_date_from_text("will send soon", "2026-06-08")
    assert result is None, f"Expected None for 'soon', got {result}"
    print("  [test_infer_date_from_text] PASS")


# ---------------------------------------------------------------------------
# Runner (standalone, no pytest required)
# ---------------------------------------------------------------------------

def _run_all(tmp_path: Path) -> None:
    results: list[tuple[str, str]] = []
    tests = [
        ("extraction_no_llm", lambda: test_extraction_no_llm(tmp_path / "t1")),
        ("reconcile_kept", lambda: test_reconcile_kept(tmp_path / "t2")),
        ("idempotency", lambda: test_idempotency(tmp_path / "t3")),
        ("summary_json", lambda: test_summary_json(tmp_path / "t4")),
        ("dry_run_writes_nothing", lambda: test_dry_run_writes_nothing(tmp_path / "t5")),
        ("promise_id_deterministic", test_promise_id_deterministic),
        ("infer_date_from_text", test_infer_date_from_text),
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            results.append((name, "PASS"))
            passed += 1
        except Exception as e:
            results.append((name, f"FAIL: {e}"))
            failed += 1

    print(f"\n{'='*60}")
    print(f"Test results: {passed} passed, {failed} failed")
    for name, result in results:
        status = "PASS" if result == "PASS" else "FAIL"
        print(f"  [{status}] {name}" + (f" — {result[6:]}" if status == "FAIL" else ""))

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _run_all(Path(td))
