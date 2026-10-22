"""Who spoke when: pyannote speaker diarization, ported from hands_on_dl/audio_pipeline/diarize.py."""
from __future__ import annotations

import json
from pathlib import Path

from .base import Check, Context, Item, Result, Setting, Stage


def hf_snapshot(repo_id: str) -> str | None:
    """The commit of the locally cached model, so the receipt names the exact weights."""
    try:
        from huggingface_hub import snapshot_download
        return Path(snapshot_download(repo_id, local_files_only=True)).name
    except Exception:
        return None


def write_rttm(annotation, file_id: str, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for seg, _, spk in annotation.itertracks(yield_label=True):
            f.write(f"SPEAKER {file_id} 1 {seg.start:.3f} {seg.end - seg.start:.3f} <NA> <NA> {spk} <NA> <NA>\n")


def stats(annotation) -> dict:
    spk = {s: round(annotation.label_duration(s), 3) for s in sorted(annotation.labels())}
    total = sum(spk.values())
    try:
        support = annotation.get_timeline().support().duration()
    except Exception:
        support = total
    return {"speaker_count": len(spk), "speakers": spk, "speech_s": round(support, 1),
            "overlap_s": round(max(0.0, total - support), 1)}


class Diarize(Stage):
    """Find speaker turns with pyannote. pyannote 4 returns both an overlap-preserving ('standard') and a
    one-speaker-at-a-time ('exclusive') answer from one pass; both are kept and later stages pick one."""
    name = "diarize"
    label = "Diarize speakers"
    version = "1"
    group = "audio"
    gpu = True
    needs = ("audio",)
    makes = ("rttm_standard", "rttm_exclusive", "diarization_stats")
    packages = ("pyannote.audio", "torch", "huggingface_hub")
    settings = {
        "model": Setting("pyannote/speaker-diarization-community-1", "Hugging Face pipeline id (gated: accept "
                         "its terms once, and give a token through the machine config's env_file)."),
        "device": Setting("auto", "auto = cuda, then mps, then cpu.", choices=["auto", "cuda", "mps", "cpu"],
                          in_key=False),
        "min_speakers": Setting(None, "Optional lower bound passed to pyannote."),
        "max_speakers": Setting(None, "Optional upper bound passed to pyannote."),
    }

    def setup(self, settings: dict, ctx: Context):
        import os

        import torch
        from pyannote.audio import Pipeline
        dev = settings["device"]
        if dev == "auto":
            dev = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
        ctx.log(f"loading {settings['model']} on {dev}")
        pipe = Pipeline.from_pretrained(settings["model"], token=os.environ.get("HF_TOKEN") or None)
        pipe.to(torch.device(dev))
        return {"pipeline": pipe, "device": dev, "revision": hf_snapshot(settings["model"])}

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        import soundfile as sf
        import torch
        wav, sr = sf.read(str(inputs["audio"]), dtype="float32", always_2d=True)
        audio = {"waveform": torch.from_numpy(wav.T.copy()), "sample_rate": sr}
        kw = {k: settings[k] for k in ("min_speakers", "max_speakers") if settings[k] is not None}
        res = state["pipeline"](audio, **kw)
        duration = wav.shape[0] / sr
        write_rttm(res.speaker_diarization, item.id, out / "standard.rttm")
        write_rttm(res.exclusive_speaker_diarization, item.id, out / "exclusive.rttm")
        st = {"standard": stats(res.speaker_diarization), "exclusive": stats(res.exclusive_speaker_diarization),
              "duration_s": round(duration, 1)}
        (out / "stats.json").write_text(json.dumps(st, indent=1), encoding="utf-8")
        n, speech = st["standard"]["speaker_count"], st["standard"]["speech_s"]
        frac = speech / duration if duration else 0.0
        checks = [
            Check("speakers", "warn" if n <= 2 and duration > 20 * 60 else "ok", n,
                  f"{n} speakers in {duration / 60:.0f} min"),
            Check("speech_fraction", "warn" if frac < 0.4 else "ok", round(frac, 3),
                  f"{frac:.0%} of the recording is someone speaking"
                  + (" — low for a meeting; check the audio stage" if frac < 0.4 else "")),
        ]
        return Result(files={"rttm_standard": "standard.rttm", "rttm_exclusive": "exclusive.rttm",
                             "diarization_stats": "stats.json"},
                      metrics=st, checks=checks, device=state["device"],
                      tools={"model": settings["model"], "model_revision": state["revision"]})

    def teardown(self, state) -> None:
        state.clear()
