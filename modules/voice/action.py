"""Speech to text, on this machine only.

The core hands this node an audio id. The node downloads the file over the tailnet (or the core's door), transcribes it with
faster-whisper, deletes its copy and returns the text. Nothing is sent to any cloud service.
"""
import shutil
from pathlib import Path

DEFAULT_MODEL = "small"          # good Spanish on an M1 at a few times real time; "base" is faster, "medium" better


def load_model(cfg: dict):
    from faster_whisper import WhisperModel
    return WhisperModel(cfg.get("voice_model", DEFAULT_MODEL), device="cpu", compute_type="int8")


def transcribe(model, path: Path, language: str, should_stop) -> tuple[str, bool]:
    """Returns (text, stopped). Stops between segments when the machine is needed back."""
    segments, _info = model.transcribe(str(path), language=language or None, vad_filter=True, beam_size=1)
    parts: list[str] = []
    for seg in segments:
        if should_stop():
            return "", True
        parts.append(seg.text.strip())
    return " ".join(p for p in parts if p), False


def run(payload: dict, ctx, model=None) -> dict:
    cfg = getattr(ctx, "config", {}) or {}
    audio_id = int(payload["audio_id"])
    work = Path(ctx.state_dir) / "voice"
    work.mkdir(parents=True, exist_ok=True)
    dest = work / f"{audio_id}.audio"
    try:
        ctx.download(f"/nodes/{ctx.agent.device_id}/audio/{audio_id}", dest)
        text, stopped = transcribe(model or load_model(cfg), dest, cfg.get("voice_language", "es"), ctx.should_stop)
    finally:
        shutil.rmtree(work, ignore_errors=True)         # no copy of the audio stays on this machine
    if stopped:
        return {"stopped": True}
    return {"text": text}
