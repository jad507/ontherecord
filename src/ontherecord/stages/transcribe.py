"""Speech to text with faster-whisper, ported from hands_on_dl/audio_pipeline/transcribe.py (same defaults)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .base import Check, Context, Item, Result, Setting, Stage
from .diarize import hf_snapshot


class Transcribe(Stage):
    """Transcribe the whole recording with Whisper. Every decoding choice is a setting, so two transcripts that
    differ can be traced to the setting that differs."""
    name = "transcribe"
    label = "Transcribe (Whisper)"
    version = "1"
    group = "audio"
    gpu = True
    needs = {"audio": "audio"}
    makes = {"transcript": "other"}
    packages = ("faster-whisper", "ctranslate2")
    settings = {
        "model": Setting("large-v3", "faster-whisper model name or a local CTranslate2 directory.",
                         choices=["large-v3", "large-v3-turbo", "medium.en", "small.en"]),
        "compute_type": Setting("float16", "float16 on a GPU; int8 for small cards and CPUs (CPU forces int8).",
                                choices=["float16", "int8_float16", "int8"]),
        "device": Setting("auto", "auto = cuda if available, else cpu (CTranslate2 has no Apple GPU backend).",
                          choices=["auto", "cuda", "cpu"], in_key=False),
        "language": Setting("en", "Language code, or empty to let Whisper detect it."),
        "beam_size": Setting(10, "Beam search width."),
        "best_of": Setting(5, "Candidates when sampling at temperature > 0."),
        "vad_filter": Setting(True, "Skip non-speech with Silero VAD; prevents hallucination loops in silence."),
        "min_silence_duration_ms": Setting(500, "VAD: silence this long splits speech."),
        "condition_on_previous_text": Setting(False, "Feed the previous window's text as a prompt. Off prevents "
                                              "repetition loops."),
        "word_timestamps": Setting(False, "Also record a time for every word."),
    }

    def setup(self, settings: dict, ctx: Context):
        try:
            # CTranslate2 needs cuBLAS and cuDNN but does not look in the nvidia-* pip packages that hold them;
            # importing torch loads them first. Without it, a machine with no system CUDA fails with
            # "libcublas.so.12 is not found" (found in the Docker image; shiro hid it with a system CUDA install).
            import torch  # noqa: F401
        except ImportError:
            pass
        from faster_whisper import WhisperModel
        dev = settings["device"]
        if dev == "auto":
            try:
                import ctranslate2
                dev = "cuda" if ctranslate2.get_cuda_device_count() > 0 and _cublas_loads() else "cpu"
            except Exception:
                dev = "cpu"
        compute = settings["compute_type"] if dev == "cuda" else "int8"
        if dev == "cuda":
            # Cards older than compute capability 7.0 (a GTX 1080) have no fast float16; CTranslate2 refuses it
            import ctranslate2
            supported = ctranslate2.get_supported_compute_types("cuda")
            if compute not in supported:
                fallback = next(c for c in ("int8_float16", "int8_float32", "float32") if c in supported)
                ctx.log(f"this card does not support {compute}; using {fallback} (recorded as compute_type_used)")
                compute = fallback
        ctx.log(f"loading faster-whisper {settings['model']} ({compute}) on {dev}")
        model = WhisperModel(settings["model"], device=dev, compute_type=compute)
        try:
            from faster_whisper.utils import _MODELS  # its short names: large-v3-turbo is not a Systran repo
        except ImportError:
            _MODELS = {}
        name = settings["model"]
        repo = name if "/" in name else _MODELS.get(name, f"Systran/faster-whisper-{name}")
        return {"model": model, "device": dev, "compute": compute, "revision": hf_snapshot(repo)}

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        params = {k: settings[k] for k in ("beam_size", "best_of", "vad_filter", "condition_on_previous_text",
                                           "word_timestamps")}
        params["vad_parameters"] = {"min_silence_duration_ms": settings["min_silence_duration_ms"]}
        params["language"] = settings["language"] or None
        segments, info = state["model"].transcribe(str(inputs["audio"]), **params)
        segs = []
        for s in segments:
            d = {"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(),
                 "avg_logprob": round(s.avg_logprob, 4), "no_speech_prob": round(s.no_speech_prob, 4)}
            if settings["word_timestamps"] and s.words:
                d["words"] = [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word,
                               "p": round(w.probability, 4)} for w in s.words]
            segs.append(d)
        (out / "transcript.json").write_text(json.dumps({"segments": segs}, indent=1, ensure_ascii=False),
                                             encoding="utf-8")
        words = sum(len(s["text"].split()) for s in segs)
        minutes = info.duration / 60 if info.duration else 0
        wpm = words / minutes if minutes else 0
        checks = [Check("words_per_minute", "warn" if wpm < 30 else "ok", round(wpm, 1),
                        f"{words:,} words over {minutes:.0f} min"
                        + (" — very little speech recognized" if wpm < 30 else ""))]
        return Result(files={"transcript": "transcript.json"}, checks=checks, device=state["device"],
                      metrics={"segments": len(segs), "words": words, "duration_s": round(info.duration, 1),
                               "language": info.language, "language_probability": round(info.language_probability, 4),
                               "compute_type_used": state["compute"]},
                      tools={"model": settings["model"], "model_revision": state["revision"]})

    def teardown(self, state) -> None:
        state.clear()


def _cublas_loads() -> bool:
    """Whether CUDA 12's cuBLAS can be loaded (after torch has put its own on the search path). A machine with an
    NVIDIA card but PyTorch for the processor has none, and CTranslate2 would fail on the first sentence."""
    import ctypes
    try:
        ctypes.CDLL("cublas64_12.dll" if os.name == "nt" else "libcublas.so.12")
        return True
    except OSError:
        return False
