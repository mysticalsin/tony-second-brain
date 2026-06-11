#!/usr/bin/env python3
"""meeting_ingest.py — convert a meeting transcript/recap file into SBAP-frontmatter'd markdown.

Inputs (auto-detected by extension):
  .vtt           Teams / Zoom / Recall.ai WebVTT — strip cues, keep speaker:text
  .docx          Teams Premium transcript export — extract paragraphs
  .md / .txt     Cluely export, Otter, manual paste — pass-through with frontmatter
  .json          Recall.ai-style transcript JSON (array of {speaker, text})

Output:
  Meetings/transcripts/<YYYY-MM-DD>-<slug>.md   (changed 2026-05-17 from 00_Inbox/from-dust/meeting-intel/)

The output carries valid SBAP frontmatter so existing triage can re-validate if desired.
Destination is the Meetings/ navigation section directly — block-curator nightly cross-links
into Meetings/by-client/ if a known client name appears in the transcript.
The 'source' field traces back to which drop-zone (cluely / teams / manual).
After successful ingest, the source file moves to 00_Inbox/from-meetings/processed/<source>/.

Usage:
  python3 meeting_ingest.py <path-to-file> [--source cluely|teams|manual]
  python3 meeting_ingest.py --watch  (continuous watch of from-meetings/)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")))
DEST = VAULT / "Meetings" / "transcripts"   # 2026-05-17: moved from 00_Inbox/from-dust/meeting-intel/
PROCESSED = VAULT / "00_Inbox" / "from-meetings" / "processed"
LOG_DIR = Path.home() / "AI-Brain-build" / "logs"


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] meeting_ingest: {msg}\n"
    with (LOG_DIR / f"meeting-ingest-{datetime.now().strftime('%Y-%m-%d')}.log").open("a") as f:
        f.write(line)


def slugify(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", s).strip()
    s = re.sub(r"[\s_]+", "-", s)
    return s.lower()[:max_len] or "meeting"


# Language detection — tries langdetect, falls back to a stopword heuristic
LANG_NAMES = {
    "en": "English", "fr": "French", "es": "Spanish", "pt": "Portuguese",
    "de": "German", "it": "Italian", "nl": "Dutch", "ja": "Japanese",
    "zh-cn": "Chinese (Simplified)", "zh-tw": "Chinese (Traditional)",
    "ko": "Korean", "ar": "Arabic", "ru": "Russian", "pl": "Polish",
    "tr": "Turkish", "sv": "Swedish", "no": "Norwegian", "da": "Danish",
    "fi": "Finnish", "el": "Greek", "he": "Hebrew", "hi": "Hindi",
    "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "uk": "Ukrainian",
    "cs": "Czech", "hu": "Hungarian", "ro": "Romanian",
}

_FALLBACK_STOPWORDS = {
    "fr": {"le", "la", "les", "de", "des", "un", "une", "et", "est", "que",
           "qui", "dans", "pour", "avec", "sur", "ne", "pas", "ce", "se",
           "il", "elle", "nous", "vous", "ils", "elles", "mais", "ou", "ça"},
    "en": {"the", "and", "of", "to", "a", "in", "is", "that", "for", "it",
           "with", "as", "on", "you", "this", "be", "are", "we", "but"},
    "es": {"el", "la", "los", "las", "de", "del", "un", "una", "y", "es",
           "que", "en", "por", "para", "con", "no", "se", "lo", "su"},
    "pt": {"o", "a", "os", "as", "de", "do", "da", "um", "uma", "e", "é",
           "que", "em", "para", "com", "não", "se", "por", "sua"},
    "de": {"der", "die", "das", "und", "ist", "ein", "eine", "zu", "in",
           "mit", "auf", "für", "von", "den", "des", "nicht", "sich"},
    "it": {"il", "la", "i", "le", "di", "un", "una", "e", "è", "che", "in",
           "per", "con", "non", "si", "lo", "ma"},
}


def detect_language(text: str) -> dict:
    """Return {code, name, confidence, secondary?}."""
    sample = text[:6000].strip()
    if len(sample) < 50:
        return {"code": "unknown", "name": "Unknown", "confidence": 0.0}

    # Primary: langdetect (Google CLD3-style, ~50 languages)
    try:
        from langdetect import detect_langs  # type: ignore
        from langdetect.lang_detect_exception import LangDetectException  # type: ignore
        try:
            results = detect_langs(sample)
            if results:
                primary = results[0]
                code = primary.lang.lower()
                out = {
                    "code": code,
                    "name": LANG_NAMES.get(code, code.upper()),
                    "confidence": round(primary.prob, 3),
                }
                if len(results) > 1 and results[1].prob > 0.15:
                    sec = results[1]
                    out["secondary"] = {
                        "code": sec.lang.lower(),
                        "name": LANG_NAMES.get(sec.lang.lower(), sec.lang.upper()),
                        "confidence": round(sec.prob, 3),
                    }
                return out
        except LangDetectException:
            pass
    except ImportError:
        pass

    # Fallback heuristic: count stopword hits per known language
    words = [w.lower() for w in re.findall(r"\b[a-zA-ZÀ-ſ]+\b", sample)]
    if not words:
        return {"code": "unknown", "name": "Unknown", "confidence": 0.0}
    scores = {}
    for lang, stops in _FALLBACK_STOPWORDS.items():
        scores[lang] = sum(1 for w in words if w in stops) / max(len(words), 1)
    best = max(scores, key=scores.get)
    return {
        "code": best, "name": LANG_NAMES.get(best, best.upper()),
        "confidence": round(scores[best], 3),
        "method": "stopword-fallback",
    }


# ─────────────────────── format converters ───────────────────────


def vtt_to_markdown(text: str) -> tuple[str, dict]:
    """WebVTT → markdown with speaker turns. Strips cue numbers + timestamps."""
    lines = text.splitlines()
    out_lines: list[str] = []
    speakers: set[str] = set()
    current_speaker: str | None = None
    in_cue = False

    for raw in lines:
        line = raw.rstrip()
        # Skip the WEBVTT header
        if line.startswith("WEBVTT") or line.startswith("NOTE") or not line:
            in_cue = False
            continue
        # Skip cue identifiers (numbers or UUID-like)
        if re.match(r"^[\d-]+$", line) or re.match(r"^[a-f0-9-]{8,}/\d+-\d+$", line):
            continue
        # Skip timestamp lines (00:00:00.000 --> 00:00:05.000)
        if "-->" in line:
            in_cue = True
            continue
        if not in_cue:
            continue

        # Cue text — may contain <v Speaker Name>text</v> style
        speaker_match = re.match(r"<v\s+([^>]+)>(.*?)(?:</v>)?$", line)
        if speaker_match:
            speaker, content = speaker_match.group(1).strip(), speaker_match.group(2).strip()
            speakers.add(speaker)
            if speaker != current_speaker:
                out_lines.append(f"\n**{speaker}:** {content}")
                current_speaker = speaker
            else:
                out_lines.append(content)
        else:
            # No speaker tag — append as continuation
            out_lines.append(line)

    body = "\n".join(out_lines).strip()
    meta = {"speakers": sorted(speakers), "format": "vtt"}
    return body, meta


def docx_to_markdown(path: Path) -> tuple[str, dict]:
    """DOCX (Teams transcript export) → markdown paragraphs."""
    try:
        from docx import Document  # python-docx
    except ImportError:
        log("python-docx not installed; falling back to raw text extraction")
        # Nuclear fallback: docx is a zip; extract document.xml and strip tags
        import zipfile
        try:
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
            text = re.sub(r"<[^>]+>", " ", xml)
            text = re.sub(r"\s+", " ", text).strip()
            return text, {"format": "docx-fallback"}
        except Exception as e:  # noqa: BLE001
            return f"(could not parse {path.name}: {e})", {"format": "docx-error"}

    doc = Document(str(path))
    paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    body = "\n\n".join(paras)
    speakers = sorted({m.group(1) for p in paras
                       for m in [re.match(r"^([A-Z][a-zA-Z'\s-]+):\s", p)] if m})
    return body, {"speakers": speakers, "format": "docx"}


def passthrough_md(text: str) -> tuple[str, dict]:
    """Plain markdown / text — pass through, strip frontmatter if present."""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            text = text[end + 4:].lstrip()
    speakers = sorted({m.group(1) for m in re.finditer(r"^([A-Z][a-zA-Z'\s-]+):\s", text, re.M)})
    return text.strip(), {"speakers": speakers, "format": "passthrough"}


def json_to_markdown(text: str) -> tuple[str, dict]:
    """Recall.ai-style {speaker, text, timestamp} JSON → markdown."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text, {"format": "json-fallback"}
    if not isinstance(data, list):
        return json.dumps(data, indent=2), {"format": "json-object"}
    out = []
    current = None
    speakers: set[str] = set()
    for turn in data:
        sp = turn.get("speaker") or turn.get("name") or "?"
        txt = (turn.get("text") or turn.get("content") or "").strip()
        if not txt:
            continue
        speakers.add(sp)
        if sp != current:
            out.append(f"\n**{sp}:** {txt}")
            current = sp
        else:
            out.append(txt)
    return "\n".join(out).strip(), {"speakers": sorted(speakers), "format": "json"}


def convert(path: Path) -> tuple[str, dict]:
    ext = path.suffix.lower()
    if ext == ".vtt":
        return vtt_to_markdown(path.read_text(encoding="utf-8", errors="replace"))
    if ext == ".docx":
        return docx_to_markdown(path)
    if ext == ".json":
        return json_to_markdown(path.read_text(encoding="utf-8", errors="replace"))
    if ext in (".md", ".txt"):
        return passthrough_md(path.read_text(encoding="utf-8", errors="replace"))
    return path.read_text(encoding="utf-8", errors="replace"), {"format": ext.lstrip(".") or "unknown"}


# ────────────────────────── render SBAP ──────────────────────────


def emit_sbap(path: Path, body: str, meta: dict, source: str) -> Path:
    today = datetime.now()
    raw_title = path.stem.replace("-transcript", "").replace("-", " ")
    title_slug = slugify(raw_title)

    # Language detection
    lang = detect_language(body)
    lang_code = lang["code"]
    lang_name = lang["name"]
    lang_conf = lang["confidence"]
    is_non_english = lang_code not in ("en", "unknown")

    # Add language tag to filename when not English (so the inbox view shows it at a glance)
    lang_suffix = f"-{lang_code}" if is_non_english else ""
    fname = f"{today.strftime('%Y-%m-%d')}-{title_slug}{lang_suffix}.md"
    DEST.mkdir(parents=True, exist_ok=True)
    out_path = DEST / fname

    summary_seed = re.sub(r"\s+", " ", body[:400]).strip()
    speakers_str = ", ".join(meta.get("speakers", []) or ["(unknown)"])
    word_count = len(body.split())

    # Multilingual banner — English meetings get no banner, others get a prominent one
    lang_banner = ""
    if is_non_english:
        lang_banner = f"\n> 🌐 **Language: {lang_name}** ({lang_code}, confidence {lang_conf}). This transcript is NOT in English.\n"
        if lang.get("secondary"):
            sec = lang["secondary"]
            lang_banner += f"> Mixed-language signal detected — secondary: {sec['name']} ({sec['code']}, conf {sec['confidence']}).\n"
    elif lang_code == "unknown":
        lang_banner = "\n> 🌐 **Language: could not detect** (transcript too short or atypical).\n"

    # Build the secondary-language YAML block if applicable
    secondary_yaml = ""
    if lang.get("secondary"):
        sec = lang["secondary"]
        secondary_yaml = f'\n  language_secondary: "{sec["code"]}"  # {sec["name"]} (conf {sec["confidence"]})'

    frontmatter = f"""---
sbap_version: "1.0"
source_agent: meeting-intel
source_run_id: meeting-ingest-{uuid.uuid4().hex[:12]}
generated: "{datetime.now(timezone.utc).isoformat()}"
input_context_refs:
  - "file://{path}"
output_type: meeting_intel
target_path: ""
confidence: 0.8
needs_review: true
reasoning_summary: |
  Ingested {meta.get('format', '?')} transcript from {source} in {lang_name} ({lang_code}, conf {lang_conf}). Speakers detected: {speakers_str}. Word count: {word_count}.
  Source file: {path.name}.
  Preview: {summary_seed[:200]}
meeting_metadata:
  source: "{source}"
  original_filename: "{path.name}"
  speakers: [{', '.join(f'"{s}"' for s in meta.get('speakers', []))}]
  word_count: {word_count}
  ingested_at: "{datetime.now(timezone.utc).isoformat()}"
  language: "{lang_code}"
  language_name: "{lang_name}"
  language_confidence: {lang_conf}{secondary_yaml}
learnings: []
patterns: []
mistakes_to_avoid: []
tags: [meeting, {source}, lang-{lang_code}]
---

# {raw_title}
{lang_banner}
**Source:** {source} · **Ingested:** {today.strftime('%Y-%m-%d %H:%M')} · **Language:** {lang_name} ({lang_code}, conf {lang_conf})
**Speakers:** {speakers_str}
**Original file:** `{path.name}`

---

{body}
"""
    out_path.write_text(frontmatter)
    return out_path


def move_to_processed(path: Path, source: str) -> Path:
    target_dir = PROCESSED / source
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    target = target_dir / f"{ts}-{path.name}"
    shutil.move(str(path), str(target))
    return target


# ─────────────────────────── main ────────────────────────────────


def ingest_one(path: Path, source: str | None = None) -> Path | None:
    if not path.exists():
        log(f"file vanished: {path}")
        return None
    # Auto-detect source from path
    if source is None:
        parts = path.parts
        if "cluely" in parts:
            source = "cluely"
        elif "teams" in parts:
            source = "teams"
        elif "manual" in parts:
            source = "manual"
        else:
            source = "unknown"

    try:
        body, meta = convert(path)
    except Exception as e:  # noqa: BLE001
        log(f"convert failed: {path}: {e!r}")
        return None

    if not body or len(body.strip()) < 20:
        log(f"too short, skipping: {path}")
        return None

    out_path = emit_sbap(path, body, meta, source)
    log(f"ingested {source}/{path.name} → {out_path.relative_to(VAULT)} ({meta.get('format')})")

    # Move source out of the drop zone so we don't reprocess
    try:
        archived = move_to_processed(path, source)
        log(f"archived source → {archived.relative_to(VAULT)}")
    except OSError as e:
        log(f"archive failed: {e}")

    return out_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="single file to ingest")
    ap.add_argument("--source", help="cluely | teams | manual (auto-detected from path)")
    ap.add_argument("--scan", action="store_true",
                    help="one-shot: scan all drop zones and ingest pending files")
    args = ap.parse_args()

    if args.scan:
        drop_root = VAULT / "00_Inbox" / "from-meetings"
        count = 0
        for source_dir in ("cluely", "teams", "manual"):
            for f in (drop_root / source_dir).glob("*"):
                if f.is_file() and f.suffix.lower() in (".vtt", ".docx", ".md", ".txt", ".json") \
                   and not f.name.startswith("."):
                    out = ingest_one(f, source=source_dir)
                    if out:
                        count += 1
                        print(f"  ingested: {f.name} → {out.name}")
        print(f"Scan complete: {count} files ingested")
        return 0

    if not args.path:
        print("Usage: meeting_ingest.py <file> | --scan", file=sys.stderr)
        return 1
    out = ingest_one(Path(args.path), args.source)
    if out:
        print(f"Ingested → {out}")
        return 0
    print("Ingest failed (see log)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
