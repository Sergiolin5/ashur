"""The node side of voice: download, transcribe here, leave nothing behind."""
from types import SimpleNamespace as NS

from modules.voice import action, selftest


class FakeModel:
    def __init__(self, texts):
        self.texts = texts

    def transcribe(self, path, **kw):
        assert open(path, "rb").read() == b"AUDIO" and kw["language"] == "es"
        return iter([NS(text=t) for t in self.texts]), NS()


def ctx(tmp_path, stop_after=None):
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return stop_after is not None and calls["n"] > stop_after

    return NS(state_dir=tmp_path, config={}, agent=NS(device_id="mac-m1"), should_stop=should_stop,
              download=lambda path, dest: dest.write_bytes(b"AUDIO"))


def test_transcribes_joins_the_segments_and_removes_its_copy(tmp_path):
    r = action.run({"audio_id": 7}, ctx(tmp_path), model=FakeModel([" Hola. ", "Esto es una prueba."]))
    assert r == {"text": "Hola. Esto es una prueba."}
    assert not (tmp_path / "voice").exists()


def test_stops_when_the_machine_is_needed_back_and_still_cleans_up(tmp_path):
    r = action.run({"audio_id": 7}, ctx(tmp_path, stop_after=1), model=FakeModel(["uno", "dos", "tres"]))
    assert r == {"stopped": True} and not (tmp_path / "voice").exists()


def test_selftest_says_how_to_fix_a_missing_dependency():
    color, why = selftest.check()
    assert color in ("green", "red") and (color == "green" or "faster-whisper" in why)
