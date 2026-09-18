"""digest MCP: one summarizer call per file, cached by content, only summaries returned."""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(tmp_path, monkeypatch):
    monkeypatch.setenv("KATAE_WORKDIR", str(tmp_path / "proj"))
    monkeypatch.setattr(sys, "argv", ["digest.py", str(tmp_path / "cache")])
    monkeypatch.syspath_prepend(str(ROOT / "mcp_servers"))
    sys.modules.pop("digest", None)
    return importlib.import_module("digest")


def test_each_file_summarized_once_then_cached(tmp_path, monkeypatch):
    docs = tmp_path / "proj" / "Docs" / "GDD"
    docs.mkdir(parents=True)
    (docs / "a.md").write_text("# A\n" + "전투 " * 3000, encoding="utf-8")
    (docs / "b.md").write_text("# B\n경제", encoding="utf-8")
    (tmp_path / "outside.md").write_text("x", encoding="utf-8")
    d = load(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(d, "_summarize", lambda text, focus, limit: calls.append(text[:12]) or f"- 요약({len(text)})")
    out = d.digest(["Docs/**/*.md", "../outside.md"])
    assert "Docs/GDD/a.md" in out and "Docs/GDD/b.md" in out and "outside" not in out
    assert len(calls) == 2 and len(out) < 1000  # summaries only, not the 9K-char original
    again = d.digest(["Docs/GDD/a.md"])
    assert "캐시" in again and len(calls) == 2


def test_one_failing_file_does_not_lose_the_rest(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "ok.md").write_text("ok", encoding="utf-8")
    (tmp_path / "proj" / "bad.md").write_text("bad", encoding="utf-8")
    d = load(tmp_path, monkeypatch)

    def fake(text, focus, limit):
        if "bad" in text:
            raise RuntimeError("limit")
        return "- fine"
    monkeypatch.setattr(d, "_summarize", fake)
    out = d.digest(["*.md"])
    assert "- fine" in out and "요약 실패" in out
