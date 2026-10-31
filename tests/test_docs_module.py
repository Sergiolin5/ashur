"""The documents module: text out of real files, incremental passes, nothing sensitive read."""
import zipfile

from modules.docs import action
from modules.docs.extract import chunks, extract


def make_docx(path, paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f"<w:document><w:body>{body}</w:body></w:document>")


class Ctx:
    def __init__(self, tmp):
        self.state_dir, self.config, self.task_id, self.sent, self.stop, self.progress = str(tmp), {}, 1, [], False, []

    def post(self, kind, body):
        assert kind == "docs"
        (self.progress if "phase" in body else self.sent).append(body)

    def should_stop(self):
        return self.stop


def test_extracts_text_docx_and_skips_binary(tmp_path):
    (tmp_path / "a.txt").write_text("hola mundo\n\n\n\nsegunda parte", encoding="utf-8")
    make_docx(tmp_path / "b.docx", ["Primer párrafo", "Segundo &amp; último"])
    (tmp_path / "c.txt").write_bytes(b"\x00\x01\x02binary")
    assert extract(str(tmp_path / "a.txt")) == "hola mundo\n\nsegunda parte"
    assert "Segundo & último" in extract(str(tmp_path / "b.docx"))
    assert extract(str(tmp_path / "c.txt")) == ""
    assert extract(str(tmp_path / "broken.docx")) == ""            # missing or damaged: skipped, never raises


def test_chunks_respect_size_and_paragraphs():
    text = "\n\n".join(["x" * 500] * 5 + ["y" * 3000])
    parts = chunks(text, 1200)
    assert all(len(p) <= 1200 for p in parts) and "".join(parts).count("y") == 3000


def test_pass_is_incremental_skips_sensitive_and_reports_deletions(tmp_path):
    docs, state = tmp_path / "docs", tmp_path / "state"
    docs.mkdir(); state.mkdir()
    (docs / "a.txt").write_text("texto uno", encoding="utf-8")
    (docs / "b.md").write_text("texto dos", encoding="utf-8")
    (docs / ".env").write_text("SECRET=1", encoding="utf-8")
    (docs / "foto.jpg").write_bytes(b"\xff\xd8" * 10)
    (docs / ".ssh").mkdir()
    (docs / ".ssh" / "notes.txt").write_text("private", encoding="utf-8")
    ctx = Ctx(state)
    s = action.run({"roots": [str(docs)]}, ctx)
    sent = {u["path"] for b in ctx.sent for u in b["upserts"]}
    assert sent == {str(docs / "a.txt"), str(docs / "b.md")} and s["read"] == 2
    ctx2 = Ctx(state)
    s2 = action.run({"roots": [str(docs)]}, ctx2)
    assert s2["read"] == 0 and s2["unchanged"] == 2 and ctx2.sent == []
    (docs / "b.md").unlink()
    ctx3 = Ctx(state)
    s3 = action.run({"roots": [str(docs)]}, ctx3)
    assert s3["removed"] == 1 and ctx3.sent[-1]["deletes"] == [str(docs / "b.md")]


def test_a_stopped_pass_resumes_without_rereading(tmp_path):
    docs, state = tmp_path / "docs", tmp_path / "state"
    docs.mkdir(); state.mkdir()
    for i in range(3):
        (docs / f"{i}.txt").write_text(f"doc {i}", encoding="utf-8")
    ctx = Ctx(state)
    calls = {"n": 0}
    def stop():
        calls["n"] += 1
        return calls["n"] > 1 and ctx.sent == [] and calls["n"] > 3     # stop while reading, before flushing
    ctx.should_stop = stop
    action.run({"roots": [str(docs)]}, ctx)
    ctx2 = Ctx(state)
    s = action.run({"roots": [str(docs)]}, ctx2)
    done = {u["path"] for b in ctx.sent + ctx2.sent for u in b["upserts"]}
    assert done == {str(docs / f"{i}.txt") for i in range(3)}


def test_cloud_only_files_are_never_opened(tmp_path, monkeypatch):
    docs, state = tmp_path / "docs", tmp_path / "state"
    docs.mkdir(); state.mkdir()
    (docs / "local.txt").write_text("aquí", encoding="utf-8")
    (docs / "nube.txt").write_text("solo en la nube", encoding="utf-8")
    monkeypatch.setattr(action, "cloud_only", lambda p: p.endswith("nube.txt"))
    opened = []
    real = action.extract
    monkeypatch.setattr(action, "extract", lambda p: (opened.append(p), real(p))[1])
    ctx = Ctx(state)
    s = action.run({"roots": [str(docs)]}, ctx)
    assert s["cloud_only"] == 1 and opened == [str(docs / "local.txt")]


def test_cloud_only_detects_the_flags():
    import types
    for attrs, flags, want in ((0x400000, 0, True), (0x1000, 0, True), (0x20, 0, False), (0, 0x40000000, True), (0, 0, False)):
        st = types.SimpleNamespace(st_file_attributes=attrs, st_flags=flags)
        import os as _os
        orig = _os.stat
        _os.stat = lambda p, st=st: st
        try:
            assert action.cloud_only("x") is want
        finally:
            _os.stat = orig


def test_progress_is_reported_while_scanning_and_reading(tmp_path):
    docs, state = tmp_path / "docs", tmp_path / "state"
    docs.mkdir(); state.mkdir()
    for i in range(3):
        (docs / f"{i}.txt").write_text(f"doc {i}", encoding="utf-8")
    ctx = Ctx(state)
    action.run({"roots": [str(docs)]}, ctx)
    assert ctx.progress and ctx.progress[0]["phase"] == "scan" and ctx.progress[0]["found"] >= 1
    assert all(set(b) <= {"phase", "found", "read", "folder"} for b in ctx.progress)    # no document text in progress
