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


def decode(path: Path):
    """16 kHz mono float32 samples, decoded with PyAV directly.
    faster-whisper's own decoder calls av.open(..., metadata_errors=...), an argument PyAV 15 removed, so on a node
    that installed a newer PyAV every transcription failed with «open() got an unexpected keyword argument»."""
    import av
    import numpy as np
    chunks = []
    with av.open(str(path), mode="r") as container:
        stream = container.streams.audio[0]
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):             # flush what the resampler still holds
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def transcribe(model, path: Path, language: str, should_stop) -> tuple[str, bool]:
    """Returns (text, stopped). Stops between segments when the machine is needed back."""
    try:
        audio = decode(path)
    except ImportError:                                  # tests and machines without PyAV: let faster-whisper read the file
        audio = str(path)
    segments, _info = model.transcribe(audio, language=language or None, vad_filter=True, beam_size=1)
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
