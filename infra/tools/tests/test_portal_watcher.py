#!/usr/bin/env python3
"""test_portal_watcher.py — Self-tests for portal_watcher.py (offline, no network, no LLM).

Tests:
  1. Dry-run with the shipped empty config -> clean exit, message "0 portals opted in".
  2. Fixture config in $TMPDIR with one opt_in:true portal + missing keychain item
     -> skipped with escalation message, exit 0, nothing written to Important/.
  3. Snapshot-diff unit: two text blobs -> change detected.
  4. Playwright-absent guard: --dry-run works with playwright unimportable.

Run from vault root or any directory:
  python build/tools/tests/test_portal_watcher.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

# Resolve portal_watcher.py relative to this test file regardless of cwd.
TOOLS_DIR = Path(__file__).resolve().parent.parent
PORTAL_WATCHER = TOOLS_DIR / "portal_watcher.py"
VAULT = Path(os.environ.get(
    "VAULT",
    "/tmp/test-vault"
))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    print(f"  [{status}] {name}")
    if detail:
        for line in detail.splitlines():
            print(f"         {line}")
    _results.append((name, ok, detail))


# --------------------------------------------------------------------------- #
# Import portal_watcher module directly for unit tests
# --------------------------------------------------------------------------- #

spec = importlib.util.spec_from_file_location("portal_watcher", PORTAL_WATCHER)
assert spec and spec.loader
pw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pw)  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Test 1 — dry-run with shipped empty config
# --------------------------------------------------------------------------- #

def test_empty_config_dry_run() -> None:
    """Dry-run with the real shipped config (portals:[]) must exit 0 with 0-portals message."""
    import tempfile, json as _json
    tmp = tempfile.mkdtemp(prefix="pw-empty-")
    config = Path(tmp) / "portal-watch.json"
    config.write_text(_json.dumps({"portals": [], "poll_hours": 6}))

    result = subprocess.run(
        [sys.executable, str(PORTAL_WATCHER), "--dry-run", "--config", str(config)],
        capture_output=True, text=True,
        env={**os.environ, "VAULT": str(VAULT)},
    )
    ok = (
        result.returncode == 0
        and "0 portals opted in" in result.stdout
    )
    detail = (
        f"rc={result.returncode}\n"
        f"stdout: {result.stdout.strip()[:200]}\n"
        f"stderr: {result.stderr.strip()[:200]}"
    )
    record("empty-config-dry-run", ok, detail)


# --------------------------------------------------------------------------- #
# Test 2 — opt_in:true portal, keychain item missing -> escalation, nothing written
# --------------------------------------------------------------------------- #

def test_opted_in_missing_credential() -> None:
    """A portal with opt_in:true but no keychain item must be skipped.
    Exit 0.  Nothing written to Important/ (escalation goes to stdout in dry-run)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Build a minimal fake vault structure
        fake_vault = tmpdir_path / "vault"
        (fake_vault / "99_Meta" / "config").mkdir(parents=True)
        (fake_vault / "Important").mkdir(parents=True)
        (fake_vault / "_agent_state" / "portal-watcher" / "snapshots").mkdir(parents=True)

        # Config with one opted-in portal referencing a definitely-missing keychain item
        config_data = {
            "portals": [{
                "name": "Test Portal",
                "url": "https://example.invalid/portal",
                "opt_in": True,
                "credential_ref": "PORTAL_WATCHER_TEST_NONEXISTENT_XYZ_12345",
                "watch": ["addenda"],
            }],
            "poll_hours": 6,
        }
        config_path = fake_vault / "99_Meta" / "config" / "portal-watch.json"
        config_path.write_text(json.dumps(config_data))

        result = subprocess.run(
            [sys.executable, str(PORTAL_WATCHER), "--dry-run", "--config", str(config_path)],
            capture_output=True, text=True,
            env={**os.environ, "VAULT": str(fake_vault)},
        )

        # Nothing should be written to Important/
        important_files = list((fake_vault / "Important").iterdir())
        nothing_written = len(important_files) == 0

        # Exit code must be 0
        exit_ok = result.returncode == 0

        # Escalation info should appear in output (stdout or stderr)
        combined_out = result.stdout + result.stderr
        skip_mentioned = (
            "SKIP" in combined_out
            or "credential" in combined_out.lower()
            or "keychain" in combined_out.lower()
            or "not found" in combined_out.lower()
            or "missing" in combined_out.lower()
        )

        ok = exit_ok and nothing_written and skip_mentioned
        detail = (
            f"rc={result.returncode} | nothing_written={nothing_written} | skip_mentioned={skip_mentioned}\n"
            f"stdout: {result.stdout.strip()[:300]}\n"
            f"stderr: {result.stderr.strip()[:300]}\n"
            f"Important/ contents: {[f.name for f in important_files]}"
        )
        record("opted-in-missing-credential", ok, detail)


# --------------------------------------------------------------------------- #
# Test 3 — snapshot diff unit test
# --------------------------------------------------------------------------- #

def test_snapshot_diff() -> None:
    """Two different text blobs must be detected as changed; identical blobs as unchanged."""
    text_a = textwrap.dedent("""\
        Procurement Portal — RFP 2026-XYZ
        Deadline: 2026-07-01 17:00 ET
        Section 1: Scope of Work
        Vendor must provide 24/7 support.
    """)
    text_b = textwrap.dedent("""\
        Procurement Portal — RFP 2026-XYZ
        Deadline: 2026-07-15 17:00 ET   <-- EXTENDED
        Section 1: Scope of Work
        Vendor must provide 24/7 support.
        ADDENDUM 1: New requirement added.
    """)

    changed_ab, diff_ab = pw.detect_change(text_a, text_b)
    changed_aa, diff_aa = pw.detect_change(text_a, text_a)

    ok = changed_ab and not changed_aa and len(diff_ab) > 0
    detail = (
        f"A vs B: changed={changed_ab} (expected True)\n"
        f"A vs A: changed={changed_aa} (expected False)\n"
        f"diff snippet: {diff_ab[:200]}"
    )
    record("snapshot-diff-unit", ok, detail)


# --------------------------------------------------------------------------- #
# Test 4 — Playwright-absent guard (dry-run must work without playwright)
# --------------------------------------------------------------------------- #

def test_playwright_absent_dry_run() -> None:
    """--dry-run must exit 0 even when playwright is not importable (stub gate)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        fake_vault = tmpdir_path / "vault"
        (fake_vault / "99_Meta" / "config").mkdir(parents=True)

        # Empty config — 0 opted-in portals
        config_path = fake_vault / "99_Meta" / "config" / "portal-watch.json"
        config_path.write_text(json.dumps({"portals": [], "poll_hours": 6}))

        # Build a Python environment where playwright is shadowed by a broken stub
        # We do this by passing a custom PYTHONPATH with a fake 'playwright' module
        fake_pkg_dir = tmpdir_path / "fake_pkgs"
        fake_playwright = fake_pkg_dir / "playwright" / "sync_api.py"
        fake_playwright.parent.mkdir(parents=True)
        # Write a stub that raises ImportError (simulates playwright not installed)
        (fake_playwright.parent / "__init__.py").write_text(
            'raise ImportError("playwright not installed (test stub)")\n'
        )
        fake_playwright.write_text(
            'raise ImportError("playwright not installed (test stub)")\n'
        )

        env = {**os.environ, "VAULT": str(fake_vault)}
        # Prepend the fake package dir so 'import playwright' raises ImportError
        old_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(fake_pkg_dir) + (":" + old_pp if old_pp else "")

        result = subprocess.run(
            [sys.executable, str(PORTAL_WATCHER), "--dry-run", "--config", str(config_path)],
            capture_output=True, text=True, env=env,
        )

        ok = result.returncode == 0
        detail = (
            f"rc={result.returncode}\n"
            f"stdout: {result.stdout.strip()[:300]}\n"
            f"stderr: {result.stderr.strip()[:200]}"
        )
        record("playwright-absent-dry-run", ok, detail)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def main() -> int:
    print(f"\nportal_watcher self-tests  ({PORTAL_WATCHER})\n{'='*60}")

    test_empty_config_dry_run()
    test_opted_in_missing_credential()
    test_snapshot_diff()
    test_playwright_absent_dry_run()

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(_results)} tests.")
    if failed:
        print("SOME TESTS FAILED.")
    else:
        print("All tests passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
