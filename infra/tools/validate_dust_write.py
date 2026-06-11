#!/usr/bin/env python3
"""validate_dust_write.py — Phase 2.3
Assertion helper. Validates that a Dust write file conforms to SBAP + carries the
optional learnings/patterns recommendations. Used by /test-agent and /dust-resolve --bulk.

Usage:
    python3 build/tools/validate_dust_write.py <path-to-md-file>
    python3 build/tools/validate_dust_write.py --strict <path>   # require learnings/patterns

Exit code: 0 if passes all critical checks, 1 if any fail.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None

try:
    import jsonschema  # type: ignore
except ImportError:
    jsonschema = None

VAULT = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")))
SCHEMA = VAULT / "99_Meta" / "sbap-schemas" / "agent_write.schema.json"


def parse_frontmatter(text: str):
    if not text.startswith("---\n") or yaml is None:
        return None, "missing or unparseable frontmatter"
    end = text.find("\n---", 4)
    if end == -1:
        return None, "frontmatter not closed with '---'"
    try:
        fm = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as e:
        return None, f"YAML parse error: {e}"
    return fm, None


def coerce_for_schema(value):
    from datetime import date, datetime as _dt
    if isinstance(value, _dt):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: coerce_for_schema(v) for k, v in value.items()}
    if isinstance(value, list):
        return [coerce_for_schema(v) for v in value]
    return value


def validate(path: Path, strict: bool = False) -> dict:
    """Return a result dict with pass/fail per check."""
    out = {
        "file": str(path),
        "checks": [],
        "ok": True,
        "warnings": 0,
        "failures": 0,
    }

    def check(name: str, passed: bool, msg: str = "", severity: str = "fail"):
        out["checks"].append({"name": name, "passed": bool(passed), "msg": msg, "severity": severity})
        if not passed:
            if severity == "fail":
                out["ok"] = False
                out["failures"] += 1
            else:
                out["warnings"] += 1

    if not path.exists():
        check("file_exists", False, f"path not found: {path}")
        return out
    check("file_exists", True)

    text = path.read_text()
    fm, err = parse_frontmatter(text)
    if fm is None:
        check("frontmatter_parsable", False, err)
        return out
    check("frontmatter_parsable", True)
    fm = coerce_for_schema(fm)

    # Required SBAP fields
    required = ["sbap_version", "source_agent", "source_run_id", "generated",
                "input_context_refs", "output_type", "target_path", "confidence"]
    for f in required:
        check(f"required:{f}", f in fm, f"missing field {f}")

    if "sbap_version" in fm:
        check("sbap_version_eq_1.0", fm["sbap_version"] == "1.0",
              f"got {fm['sbap_version']!r}")
    if "confidence" in fm:
        c = fm["confidence"]
        check("confidence_in_range", isinstance(c, (int, float)) and 0 <= c <= 1,
              f"confidence={c!r}")
    if "output_type" in fm:
        # Mirror the enum in 99_Meta/sbap-schemas/agent_write.schema.json
        enum = ["proposal_draft", "intelligence_brief", "email_draft", "escalation_alert",
                "weekly_review", "qualification", "coaching_note", "win_loss_retro",
                "partner_submission", "amaia_demo_draft", "block_curation_recs",
                "inbound_email", "calendar_event",
                "quote", "sow", "po_ingest", "social_post", "meeting_intel",
                "other"]
        check("output_type_in_enum", fm["output_type"] in enum,
              f"{fm['output_type']!r} not in enum")
    if "source_agent" in fm:
        check("source_agent_slug", bool(re.match(r"^[a-z0-9-]+$", str(fm["source_agent"]))),
              f"{fm['source_agent']!r} not lowercase-hyphen")
    if "input_context_refs" in fm:
        refs = fm["input_context_refs"]
        check("input_context_refs_nonempty",
              isinstance(refs, list) and len(refs) > 0,
              "must be a non-empty list — audit chain breaks without it")

    # Strict mode: encourages learnings accumulation
    if strict:
        for f in ["learnings", "patterns", "mistakes_to_avoid"]:
            v = fm.get(f) or []
            check(f"strict:{f}_present", isinstance(v, list) and len(v) > 0,
                  f"strict mode: {f} should have ≥1 entry for memory accumulation",
                  severity="warn")

    # Full schema check if jsonschema available
    if jsonschema is not None:
        try:
            schema = json.loads(SCHEMA.read_text())
            jsonschema.Draft7Validator(schema).validate(fm)
            check("schema_full_validation", True)
        except jsonschema.ValidationError as e:
            check("schema_full_validation", False, f"{e.message} at {list(e.path)}")
        except OSError as e:
            check("schema_full_validation", True, f"schema unreadable: {e}", severity="warn")
    else:
        check("schema_full_validation", True, "jsonschema not installed — skipped", severity="warn")

    return out


def render_report(result: dict) -> str:
    lines = [f"# Validation report — {result['file']}", ""]
    lines.append(f"**Verdict:** {'✅ PASS' if result['ok'] else '❌ FAIL'}")
    lines.append(f"**Failures:** {result['failures']}  ·  **Warnings:** {result['warnings']}")
    lines.append("")
    lines.append("| Check | Result | Detail |")
    lines.append("|---|---|---|")
    for c in result["checks"]:
        icon = "✓" if c["passed"] else ("!" if c["severity"] == "warn" else "✗")
        lines.append(f"| {c['name']} | {icon} | {c.get('msg', '')} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = validate(Path(args.path), strict=args.strict)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render_report(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
