"""Which words did the speaker stress: WhiStress on every recording in a stress test set. Ported from
AITranscribe/validate_whistress.py (the transcript given) and measure_whistress.py (audio only)."""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from .base import Check, Context, Item, Result, Setting, Stage

# Yosha et al. 2025, Table 3: WhiStress given the true transcript.
PUBLISHED_F1 = {"StressTest": 0.883, "StressPresso": 0.835}
WHISPER_WINDOW_S = 30.0  # Whisper pads or cuts every clip to 30 s; past that is never heard


def norm(word: str) -> str:
    return re.sub(r"[^\w']", "", word.lower())


def align(ref_words: list[str], pred_pairs: list[tuple]) -> tuple[list, int]:
    """One value per reference word from (word, value) pairs: equal words map directly, and a same-length run of
    substituted words maps position by position (a mishearing keeps the label of the word it replaced). Reference
    words left unmapped get 0. Returns the values and how many words were mapped."""
    ref, pred = [norm(w) for w in ref_words], [norm(w) for w, _ in pred_pairs]
    out, matched = [0] * len(ref), 0
    for op, a0, a1, b0, b1 in difflib.SequenceMatcher(a=ref, b=pred, autojunk=False).get_opcodes():
        if op == "equal" or (op == "replace" and a1 - a0 == b1 - b0):
            for k in range(a1 - a0):
                out[a0 + k] = pred_pairs[b0 + k][1]
                matched += 1
    return out, matched


def prf(pred: list[int], gold: list[int]) -> dict:
    tp = sum(p and g for p, g in zip(pred, gold))
    fp = sum(p and not g for p, g in zip(pred, gold))
    fn = sum(g and not p for p, g in zip(pred, gold))
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0}


def score(marks: list[dict], items: list[dict]) -> dict:
    gold = {c["id"]: c["stress_binary"] for c in items}
    out = {}
    for mode in ("given", "heard"):
        rows = [m for m in marks if mode in m]
        if rows:
            out[mode] = prf([x for m in rows for x in m[mode]["binary"]], [x for m in rows for x in gold[m["id"]]])
    tops = [m for m in marks if "heard" in m]
    if tops:
        hits = [m["heard"]["top"] is not None and gold[m["id"]][m["heard"]["top"]] == 1 for m in tops]
        out["top1"] = round(sum(hits) / len(hits), 3)
        out["words_aligned"] = round(sum(m["heard"]["aligned"] for m in tops)
                                     / sum(len(gold[m["id"]]) for m in tops), 3)
    return out


class WhiStressMarks(Stage):
    """Mark each word stressed or not with WhiStress (Whisper small.en plus a stress head; Yosha, Shechtman and
    Adi, 2025), two ways: given the true transcript, and from the audio alone (Whisper's own transcript, lined up
    with the true one afterwards). Scored against the set's labels: word-level F1, and top-1, whether the word it
    rates most stressed is a stressed word."""
    name = "whistress"
    label = "Mark stressed words (WhiStress)"
    version = "1"
    group = "stress"
    gpu = True
    needs = ("stress_set",)
    makes = ("stress_marks",)
    packages = ("transformers", "torch", "librosa", "soundfile")
    settings = {
        "weights_revision": Setting("3641786c97df31786d2ae17bb9307f7776cf7138", "Commit of slprl/WhiStress on "
                                    "Hugging Face (the stress head)."),
        "backbone_revision": Setting("e8727524f962ee844a7319d92be39ac1bd25655a", "Commit of "
                                     "openai/whisper-small.en (the Whisper underneath)."),
        "device": Setting("auto", "auto = cuda, then mps, then cpu.", choices=["auto", "cuda", "mps", "cpu"],
                          in_key=False),
    }

    def setup(self, settings: dict, ctx: Context):
        import torch
        from huggingface_hub import hf_hub_download

        from . import _whistress as W
        dev = settings["device"]
        if dev == "auto":
            dev = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        files = [Path(hf_hub_download(W.WEIGHTS_REPO, f, revision=settings["weights_revision"]))
                 for f in W.WEIGHT_FILES]
        ctx.log(f"loading WhiStress ({W.BACKBONE}) on {dev}")
        model = W.WhiStress(files[0].parent, dev, revision=settings["backbone_revision"])
        from ..hashing import sha256_file
        return {"model": model, "W": W, "device": dev,
                "tools": {"whistress_code": "slp-rl/WhiStress bb8921f5 (vendored, transformers 5 fix)",
                          "weights": {f.name: sha256_file(f) for f in files},
                          "weights_revision": settings["weights_revision"], "backbone": W.BACKBONE,
                          "backbone_revision": settings["backbone_revision"]}}

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        from .stress_set import clip_audio, read_set
        W, model = state["W"], state["model"]
        manifest, z = read_set(inputs["stress_set"])
        marks, seen, long_clips = [], {}, 0
        for n, c in enumerate(manifest["items"], 1):
            key = (c["audio"], c["transcription"])
            if key not in seen:  # an item asked twice reads one recording
                y, sr = clip_audio(z, c)
                long_clips += len(y) / sr > WHISPER_WINDOW_S
                feats = W.features(model, W.prepare_audio(y, sr))
                ref = c["transcription"].split()
                given = W.word_pairs(model, *model.given(feats, c["transcription"]))
                heard = W.word_pairs(model, *model.heard(feats))
                g_bin, _ = align(ref, [(w, lab) for w, lab, _ in given])
                h_bin, matched = align(ref, [(w, lab) for w, lab, _ in heard])
                h_prob, _ = align(ref, [(w, p) for w, _, p in heard])
                top = max(range(len(ref)), key=lambda k: h_prob[k]) if matched else None
                seen[key] = {"given": {"binary": g_bin, "words": [list(x) for x in given]},
                             "heard": {"binary": h_bin, "probs": [round(p, 4) for p in h_prob], "top": top,
                                       "aligned": matched, "transcript": " ".join(w for w, _, _ in heard),
                                       "words": [list(x) for x in heard]}}
            marks.append({"id": c["id"], **seen[key]})
            if n % 50 == 0:
                ctx.log(f"  {n}/{len(manifest['items'])}")
        (out / "marks.jsonl").write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in marks),
                                         encoding="utf-8")
        s = score(marks, manifest["items"])
        blind = {c["id"] for c in manifest["items"] if c.get("blind")}
        if blind:
            s["blind"] = score([m for m in marks if m["id"] in blind], manifest["items"])
        checks = []
        pub = PUBLISHED_F1.get(manifest["name"])
        if pub is not None:
            gap = s["given"]["f1"] - pub
            checks.append(Check("published_f1", "ok" if abs(gap) <= 0.02 else "warn", s["given"]["f1"],
                                f"F1 {s['given']['f1']:.3f} with the true transcript; the paper reports {pub:.3f}"))
        if s.get("words_aligned", 1) < 0.9:
            checks.append(Check("words_aligned", "warn", s["words_aligned"], f"only {s['words_aligned']:.0%} of "
                                "words heard could be lined up with the true transcript"))
        if long_clips:
            checks.append(Check("clip_length", "warn", long_clips, f"{long_clips} recordings are longer than 30 s; "
                                "WhiStress never hears past 30 s"))
        return Result(files={"stress_marks": "marks.jsonl"}, metrics={**s, "recordings": len(seen)}, checks=checks,
                      tools=state["tools"], device=state["device"])
