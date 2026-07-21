from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "scripts" / "capture_session.py"
_spec = importlib.util.spec_from_file_location("capture_session", _SCRIPT)
capture_session = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(capture_session)  # type: ignore[union-attr]


def _entry(role: str, content) -> dict:
    return {"type": role, "message": {"role": role, "content": content}}


def test_extract_text_from_string_and_blocks() -> None:
    assert capture_session.extract_text("plain text") == "plain text"
    blocks = [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "name": "Bash"},
        {"type": "text", "text": "world"},
    ]
    assert capture_session.extract_text(blocks) == "hello\nworld"
    assert capture_session.extract_text(None) == ""


def test_build_digest_uses_first_user_and_last_assistant() -> None:
    entries = [
        _entry("user", "add auth to the API"),
        _entry("assistant", "Sure, starting now."),
        _entry("user", "also add tests"),
        _entry("assistant", "Done: JWT auth added with 12 tests, all green."),
    ]
    digest = capture_session.build_digest(entries, "/home/u/workspace/my-api")
    assert digest is not None
    assert "my-api" in digest
    assert "2 user turns" in digest
    assert 'Started with: "add auth to the API"' in digest
    assert "JWT auth added with 12 tests" in digest


def test_build_digest_skips_tool_results_and_interrupts() -> None:
    entries = [
        _entry("user", "<system-reminder>noise</system-reminder>"),
        _entry("user", "[Request interrupted by user]"),
        _entry("user", "real question"),
        _entry("assistant", "real answer"),
    ]
    digest = capture_session.build_digest(entries, "/w/proj")
    assert digest is not None
    assert "1 user turns" in digest
    assert 'Started with: "real question"' in digest


def test_build_digest_empty_transcript_returns_none() -> None:
    assert capture_session.build_digest([], "/w/proj") is None
    assert capture_session.build_digest([{"type": "meta"}], "/w/proj") is None


def test_build_digest_truncates_long_messages() -> None:
    entries = [
        _entry("user", "x" * 5000),
        _entry("assistant", "y" * 5000),
    ]
    digest = capture_session.build_digest(entries, "/w/proj")
    assert digest is not None
    assert len(digest) < 2000


def test_read_transcript_tolerates_garbage(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    f.write_text('{"type": "user", "message": {"role": "user", "content": "hi"}}\nnot json\n\n')
    entries = capture_session.read_transcript(str(f))
    assert len(entries) == 1
    assert capture_session.read_transcript(str(tmp_path / "missing.jsonl")) == []
