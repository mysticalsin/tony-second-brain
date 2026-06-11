#!/usr/bin/env python3
"""validate_sbap_write.py — canonical SBAP frontmatter validator (fail-closed).

Validates the YAML frontmatter of a markdown file against the authoritative
schema 99_Meta/sbap-schemas/agent_write.schema.json using jsonschema's
Draft7Validator WITH a FormatChecker (so `format: date-time` is enforced).

Used by the Ultron Obsidian plugin BEFORE moving a draft into
00_Inbox/from-dust/ultron/ — the plugin writes to a temp file, runs this on it,
and only renames into the inbox on exit 0. FAIL-CLOSED: any missing dependency,
unreadable schema, or validation error exits non-zero so the plugin refuses the
write (unlike triage_dust_writes.py, which silently skips when jsonschema is
absent — we must not rely on that for parity).

Usage:
    python3 build/tools/validate_sbap_write.py <file.md> [--schema <path>]
Exit codes: 0 = valid · 1 = invalid · 2 = environment/usage error (fail closed).
"""
import sys
import os
import argparse


def fail(code, msg):
    sys.stderr.write(f"validate_sbap_write: {msg}\n")
    sys.exit(code)


def extract_frontmatter(text):
    """Return the YAML frontmatter block (string) between the leading --- fences."""
    if not text.startswith("---"):
        return None
    # split on the first two --- lines
    lines = text.splitlines()
    if lines[0].strip() != "---":
        return None
    body = []
    for ln in lines[1:]:
        if ln.strip() == "---":
            return "\n".join(body)
        body.append(ln)
    return None  # no closing fence


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--schema", default=None)
    args = ap.parse_args()

    try:
        import yaml  # PyYAML
    except ImportError:
        fail(2, "PyYAML not available — fail closed")
    try:
        import jsonschema
        from jsonschema import Draft7Validator, FormatChecker
    except ImportError:
        fail(2, "jsonschema not available — fail closed")

    # Resolve schema: --schema, else $SBAP_SCHEMA, else cwd/99_Meta/..., else
    # walk up from the target file to find a vault root containing 99_Meta/.
    schema_path = args.schema or os.environ.get("SBAP_SCHEMA")
    if not schema_path:
        rel = os.path.join("99_Meta", "sbap-schemas", "agent_write.schema.json")
        if os.path.exists(rel):
            schema_path = rel
        else:
            d = os.path.abspath(os.path.dirname(args.file))
            while d != os.path.dirname(d):
                cand = os.path.join(d, rel)
                if os.path.exists(cand):
                    schema_path = cand
                    break
                d = os.path.dirname(d)
    if not schema_path or not os.path.exists(schema_path):
        fail(2, "schema not found — fail closed (set --schema or $SBAP_SCHEMA)")

    try:
        import json
        with open(schema_path, "r") as f:
            schema = json.load(f)
    except Exception as e:
        fail(2, f"schema unreadable: {e}")

    try:
        with open(args.file, "r") as f:
            text = f.read()
    except Exception as e:
        fail(2, f"file unreadable: {e}")

    fm = extract_frontmatter(text)
    if fm is None:
        fail(1, "no YAML frontmatter (--- ... --- block) found")
    try:
        data = yaml.safe_load(fm)
    except Exception as e:
        fail(1, f"frontmatter is not valid YAML: {e}")
    if not isinstance(data, dict):
        fail(1, "frontmatter did not parse to a mapping")

    validator = Draft7Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        for e in errors:
            loc = "/".join(str(p) for p in e.path) or "(root)"
            sys.stderr.write(f"  - {loc}: {e.message}\n")
        fail(1, f"{len(errors)} schema violation(s)")
    print("OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
