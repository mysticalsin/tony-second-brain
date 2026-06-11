"""Tests for ingest_ai_sessions.py. Run: python3 -m pytest build/tools/tests/test_ingest_ai_sessions.py -v"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_ai_sessions as ing


def make_rollout(path: Path, session_id="abc-123", cwd="/home/user/work/proj",
                 user_msg="Build me a dashboard please", n_user=2):
    lines = [
        {"timestamp": "2026-06-03T12:00:00.000Z", "type": "session_meta",
         "payload": {"id": session_id, "timestamp": "2026-06-03T12:00:00.000Z",
                     "cwd": cwd, "originator": "Codex Desktop"}},
        {"timestamp": "2026-06-03T12:00:01.000Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": user_msg}},
    ]
    for i in range(n_user - 1):
        lines.append({"timestamp": "2026-06-03T12:01:00.000Z", "type": "response_item",
                      "payload": {"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text": f"follow-up {i}"}]}})
    lines.append({"timestamp": "2026-06-03T12:02:00.000Z", "type": "event_msg",
                  "payload": {"type": "token_count",
                              "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 20,
                                                             "total_tokens": 9289}}}})
    lines.append({"timestamp": "2026-06-03T12:03:00.000Z", "type": "event_msg",
                  "payload": {"type": "task_complete", "turn_id": "t1",
                              "last_agent_message": "Done, slide added."}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def test_parse_codex_rollout(tmp_path):
    f = tmp_path / "rollout-2026-06-03T12-00-00-abc-123.jsonl"
    make_rollout(f)
    s = ing.parse_codex_rollout(f)
    assert s["session_id"] == "abc-123"
    assert s["date"] == "2026-06-03"
    assert s["cwd"] == "/home/user/work/proj"
    assert s["turns"] == 2
    assert s["summary"].startswith("Build me a dashboard")
    assert "Done, slide added." in s["last_message"]


def test_parse_codex_rollout_skips_excluded_cwd(tmp_path):
    f = tmp_path / "rollout-x.jsonl"
    make_rollout(f, cwd="/home/user/OneDrive/HR Documents/reviews")
    assert ing.parse_codex_rollout(f) is None


def test_write_note_idempotent(tmp_path):
    sessions_dir = tmp_path / "AI Sessions"
    session = {"session_id": "abc-123", "date": "2026-06-03", "cwd": "/x",
               "summary": "Build me a dashboard", "turns": 2, "last_message": "Done."}
    p1 = ing.write_session_note(sessions_dir, "codex", session)
    p2 = ing.write_session_note(sessions_dir, "codex", session)
    assert p1 is not None and p2 is None  # second call: already exists
    notes = list((sessions_dir / "codex").glob("*.md"))
    assert len(notes) == 1
    text = notes[0].read_text()
    assert "type: ai-session" in text
    assert "tool: codex" in text
    assert 'summary: "Build me a dashboard"' in text


def test_ingest_dust_writes(tmp_path):
    agent_dir = tmp_path / "_agent_state" / "rfp-drafter"
    agent_dir.mkdir(parents=True)
    rows = [
        {"ts": "2026-06-03T10:00:00Z", "action": "promoted", "target": "01_Projects/x/05 - draft.md",
         "confidence": 0.9, "source_run_id": "run-1", "output_type": "proposal_draft"},
        {"ts": "2026-06-03T11:00:00Z", "action": "held", "target": "01_Projects/y/05 - draft.md",
         "confidence": 0.6, "source_run_id": "run-2", "output_type": "proposal_draft"},
    ]
    (agent_dir / "writes.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    sessions_dir = tmp_path / "AI Sessions"
    n = ing.ingest_dust(tmp_path / "_agent_state", sessions_dir, since_ts=None)
    assert n == 2
    notes = sorted((sessions_dir / "dust").glob("*.md"))
    assert len(notes) == 2
    assert "rfp-drafter" in notes[0].read_text()


def test_ingest_dust_respects_checkpoint(tmp_path):
    agent_dir = tmp_path / "_agent_state" / "rfp-drafter"
    agent_dir.mkdir(parents=True)
    rows = [
        {"ts": "2026-06-01T10:00:00Z", "action": "promoted", "target": "a.md",
         "confidence": 0.9, "source_run_id": "old-run", "output_type": "x"},
        {"ts": "2026-06-03T11:00:00Z", "action": "held", "target": "b.md",
         "confidence": 0.6, "source_run_id": "new-run", "output_type": "x"},
    ]
    (agent_dir / "writes.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    n = ing.ingest_dust(tmp_path / "_agent_state", tmp_path / "AI Sessions", since_ts="2026-06-02T00:00:00Z")
    assert n == 1


def test_ingest_gemini_empty_dir_is_zero(tmp_path):
    conv = tmp_path / "conversations"
    conv.mkdir()
    assert ing.ingest_gemini(conv, tmp_path / "AI Sessions", since_ts=None) == 0


def test_slugify():
    assert ing.slugify("Build me a dashboard, please!") == "build-me-a-dashboard-please"
    assert len(ing.slugify("x" * 300)) <= 60


def test_ingest_dust_skips_reserved_agents(tmp_path):
    reserved = ["codex", "gemini", "claude-code"]
    row = {"ts": "2026-06-03T10:00:00Z", "action": "promoted", "target": "a.md",
           "confidence": 0.9, "source_run_id": "r1", "output_type": "x"}
    for name in reserved + ["deal-intel"]:
        agent_dir = tmp_path / "_agent_state" / name
        agent_dir.mkdir(parents=True)
        (agent_dir / "writes.jsonl").write_text(json.dumps(row) + "\n")
    sessions_dir = tmp_path / "AI Sessions"
    n = ing.ingest_dust(tmp_path / "_agent_state", sessions_dir, since_ts=None)
    assert n == 1
    notes = list((sessions_dir / "dust").glob("*.md"))
    assert len(notes) == 1
    assert "deal-intel" in notes[0].read_text()


def test_ingest_dust_unreadable_file_skips_only_that_agent(tmp_path, capsys):
    state_dir = tmp_path / "_agent_state"
    # Agent A — readable
    agent_a = state_dir / "agent-alpha"
    agent_a.mkdir(parents=True)
    row = {"ts": "2026-06-03T10:00:00Z", "action": "promoted", "target": "a.md",
           "confidence": 0.9, "source_run_id": "r1", "output_type": "x"}
    (agent_a / "writes.jsonl").write_text(json.dumps(row) + "\n")
    # Agent B — unreadable
    agent_b = state_dir / "agent-beta"
    agent_b.mkdir(parents=True)
    bad_file = agent_b / "writes.jsonl"
    bad_file.write_text(json.dumps(row) + "\n")
    bad_file.chmod(0o000)
    try:
        sessions_dir = tmp_path / "AI Sessions"
        n = ing.ingest_dust(state_dir, sessions_dir, since_ts=None)
        assert n == 1
        notes = list((sessions_dir / "dust").glob("*.md"))
        assert len(notes) == 1
        assert "agent-alpha" in notes[0].read_text()
        captured = capsys.readouterr().out
        assert "cannot read" in captured
    finally:
        bad_file.chmod(0o644)


def test_note_filename_no_trailing_dash(tmp_path):
    sessions_dir = tmp_path / "AI Sessions"
    # "a" * 39 + " more words" slugifies to "a" * 39 + "-more-words"; truncating at 40 chars
    # gives "aaa...aaa-" — a trailing dash that must be stripped.
    summary = "a" * 39 + " more words"
    # session_id "abc-1234extra" → strip non-alphanum → "abc1234extra" → tail8 = "34extra"
    # (only 7 chars after stripping, but that's fine — the tail is whatever remains up to 8)
    session = {"session_id": "abc-1234extra", "date": "2026-06-04",
               "summary": summary, "turns": 1, "last_message": ""}
    p = ing.write_session_note(sessions_dir, "codex", session)
    assert p is not None
    name = p.name
    # No double-dash (which would indicate a trailing dash before the short_id segment)
    assert "--" not in name
    # Filename ends with the uuid tail, not the old prefix
    assert "1234ext" in name or "234extr" in name or "34extra" in name


def test_short_id_uses_uuid_tail(tmp_path):
    """Two sessions with the same summary but differing only in the UUID tail must
    each produce a distinct note — the old [:8] prefix was shared and caused silent drops."""
    sessions_dir = tmp_path / "AI Sessions"
    session_a = {
        "session_id": "019e8d5d-aaaa-bbbb-cccc-111111111111",
        "date": "2026-06-04",
        "summary": "Build the dashboard",
        "turns": 1,
        "last_message": "",
    }
    session_b = {
        "session_id": "019e8d5d-aaaa-bbbb-cccc-222222222222",
        "date": "2026-06-04",
        "summary": "Build the dashboard",
        "turns": 1,
        "last_message": "",
    }
    p1 = ing.write_session_note(sessions_dir, "codex", session_a)
    p2 = ing.write_session_note(sessions_dir, "codex", session_b)
    assert p1 is not None, "first session should be written"
    assert p2 is not None, "second session should also be written (different UUID tail)"
    notes = list((sessions_dir / "codex").glob("*.md"))
    assert len(notes) == 2, f"expected 2 notes, got {len(notes)}: {[n.name for n in notes]}"
    # Verify the tail discriminators are embedded in the filenames
    assert "11111111" in p1.name
    assert "22222222" in p2.name


def test_main_failed_tool_does_not_save_checkpoint(tmp_path, monkeypatch, capsys):
    # Set up module-level constants to point at tmp_path
    monkeypatch.setattr(ing, "AGENT_STATE", tmp_path / "_agent_state")
    monkeypatch.setattr(ing, "SESSIONS_DIR", tmp_path / "AI Sessions")
    monkeypatch.setattr(ing, "CODEX_HOME", tmp_path / "codex_home")
    monkeypatch.setattr(ing, "GEMINI_CONVERSATIONS", tmp_path / "gemini_conversations")

    # Seed gemini and dust dirs so those ingest functions return 0 without error
    (tmp_path / "gemini_conversations").mkdir(parents=True)
    (tmp_path / "_agent_state").mkdir(parents=True)

    # Make ingest_codex raise, others return 0
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ing, "ingest_codex", boom)
    monkeypatch.setattr(ing, "ingest_gemini", lambda *a, **kw: 0)
    monkeypatch.setattr(ing, "ingest_dust", lambda *a, **kw: 0)

    ing.main()

    captured = capsys.readouterr().out
    assert "codex: INGEST FAILED" in captured

    # Codex checkpoint must NOT exist (failed)
    assert not (tmp_path / "_agent_state" / "codex" / "last_ingest.json").exists()

    # Gemini and dust checkpoints MUST exist (succeeded)
    assert (tmp_path / "_agent_state" / "gemini" / "last_ingest.json").exists()
    assert (tmp_path / "_agent_state" / "dust" / "last_ingest.json").exists()
