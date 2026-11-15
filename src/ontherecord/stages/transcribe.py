"""Speech to text with faster-whisper, ported from hands_on_dl/audio_pipeline/transcribe.py (same defaults)."""
from __future__ import annotations

import os
from pathlib import Path

from ..artifacts import segments as segments_doc
from ..artifacts import write_json
from .base import Check, Context, Item, Result, Setting, Stage, UserError
from .diarize import hf_snapshot


class Transcribe(Stage):
    """Transcribe the whole recording with Whisper. Every decoding choice is a setting, so two transcripts that
    differ can be traced to the setting that differs. Writes segments (unit: segment), ids s0000, s0001..."""
    name = "transcribe"
    label = "Transcribe (Whisper)"
    version = "2"
    group = "audio"
    gpu = True
    needs = {"audio": "audio"}
    makes = {"transcript": "segments"}
    headline = ("words", "words")
    packages = ("faster-whisper", "ctranslate2")
    settings = {
        "model": Setting("large-v3", "faster-whisper model name (large-v3, large-v3-turbo, medium, small...) or a "
                         "local CTranslate2 directory, e.g. a model tuned for your language."),
        "compute_type": Setting("float16", "float16 or int8_float16 on a GPU; int8 for small cards and processors "
                                "(the processor always uses int8).", choices=["float16", "int8_float16", "int8"]),
        "device": Setting("auto", "auto = cuda if this machine has an NVIDIA card, else cpu (CTranslate2 has no Apple "
                          "GPU backend). Settled before the run, and part of the key: a processor's transcript is "
                          "not byte-identical to a card's.", choices=["auto", "cuda", "cpu"]),
        "language": Setting("", "Language code (en, es, de...), or empty to let Whisper detect it from the first 30 "
                            "seconds (the language check warns when it is unsure)."),
        "beam_size": Setting(10, "Beam search width."),
        "best_of": Setting(5, "Candidates when sampling at temperature > 0."),
        "vad_filter": Setting(True, "Skip non-speech with Silero VAD; prevents hallucination loops in silence."),
        "min_silence_duration_ms": Setting(500, "VAD: silence this long splits speech."),
        "condition_on_previous_text": Setting(False, "Feed the previous window's text as a prompt. Off prevents "
                                              "repetition loops."),
        "word_timestamps": Setting(False, "Also record a time for every word."),
    }

    @classmethod
    def resolve(cls, settings: dict, machine: dict) -> dict:
        from ..hwdetect import ct2_compute_types
        cards = machine.get("cuda") or []
        if settings["device"] == "auto":
            settings["device"] = "cuda" if cards else "cpu"
        if settings["device"] == "cpu":
            settings["compute_type"] = "int8"
        elif not cards:
            raise UserError("transcribe: device is cuda, but this machine has no NVIDIA card.",
                            "Set device: cpu (and compute_type: int8), or leave device: auto.")
        else:
            ok = ct2_compute_types(cards[0]["compute_cap"])
            if settings["compute_type"] not in ok:
                raise UserError(f"transcribe: {cards[0]['name']} (compute capability {cards[0]['compute_cap']}) "
                                f"cannot use compute_type {settings['compute_type']}.",
                                f"Use one it can: {', '.join(c for c in ok if c in cls.settings['compute_type'].choices)}.")
        return settings

    def setup(self, settings: dict, ctx: Context):
        try:
            # CTranslate2 needs cuBLAS and cuDNN but does not look in the nvidia-* pip packages that hold them;
            # importing torch loads them first. Without it, a machine with no system CUDA fails with
            # "libcublas.so.12 is not found" (found in the Docker image; shiro hid it with a system CUDA install).
            import torch  # noqa: F401
        except ImportError:
            pass
        from faster_whisper import WhisperModel
        dev, compute = settings["device"], settings["compute_type"]   # settled by resolve(); no fallback here
        if dev == "cuda" and not _cublas_loads():
            raise UserError("transcribe: CUDA 12's cuBLAS cannot be loaded, so Whisper cannot use the card.",
                            "Run `ontherecord setup --check`, or set device: cpu.")
        ctx.log(f"loading faster-whisper {settings['model']} ({compute}) on {dev}")
        model = WhisperModel(settings["model"], device=dev, compute_type=compute)
        try:
            from faster_whisper.utils import _MODELS  # its short names: large-v3-turbo is not a Systran repo
        except ImportError:
            _MODELS = {}
        name = settings["model"]
        repo = name if "/" in name or Path(name).is_dir() else _MODELS.get(name, f"Systran/faster-whisper-{name}")
        return {"model": model, "device": dev, "compute": compute,
                "revision": None if Path(name).is_dir() else hf_snapshot(repo)}

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        params = {k: settings[k] for k in ("beam_size", "best_of", "vad_filter", "condition_on_previous_text",
                                           "word_timestamps")}
        params["vad_parameters"] = {"min_silence_duration_ms": settings["min_silence_duration_ms"]}
        params["language"] = settings["language"] or None
        segments, info = state["model"].transcribe(str(inputs["audio"]), **params)
        units = []
        for i, s in enumerate(segments):
            d = {"id": f"s{i:04d}", "start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(),
                 "avg_logprob": round(s.avg_logprob, 4), "no_speech_prob": round(s.no_speech_prob, 4)}
            if settings["word_timestamps"] and s.words:
                d["words"] = [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word,
                               "p": round(w.probability, 4)} for w in s.words]
            units.append(d)
        doc = segments_doc(units, "segment")
        doc["language"] = info.language
        write_json(out / "transcript.json", doc)
        words = sum(len(u["text"].split()) for u in units)
        minutes = info.duration / 60 if info.duration else 0
        wpm = words / minutes if minutes else 0
        checks = [Check("words_per_minute", "warn" if wpm < 30 else "ok", round(wpm, 1),
                        f"{words:,} words over {minutes:.0f} min"
                        + (" — very little speech recognized" if wpm < 30 else ""))]
        if not settings["language"]:
            p = info.language_probability
            checks.append(Check("language", "warn" if p < 0.8 else "ok", round(p, 3),
                                f"detected {info.language} ({p:.0%} sure, from the first 30 seconds)"
                                + (" — set language: if this is wrong" if p < 0.8 else "")))
        return Result(files={"transcript": "transcript.json"}, checks=checks, device=state["device"],
                      metrics={"segments": len(units), "words": words, "duration_s": round(info.duration, 1),
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
