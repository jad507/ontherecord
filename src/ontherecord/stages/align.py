"""Attach a speaker to every transcript segment, ported from hands_on_dl/audio_pipeline/align.py."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .base import Check, Context, Item, Result, Setting, Stage


def load_rttm(path: Path) -> list[dict]:
    """RTTM rows as {start, end, speaker}. Fields are read from the right, so a file id with spaces still parses."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 10 or parts[0] != "SPEAKER":
            continue
        start, dur = float(parts[-7]), float(parts[-6])
        rows.append({"start": start, "end": start + dur, "speaker": parts[-3]})
    return rows


def assign_speaker(start: float, end: float, turns: list[dict]) -> str:
    """The speaker whose turns overlap [start, end] the most; UNKNOWN when none does."""
    best, best_ov = "UNKNOWN", 0.0
    for t in turns:
        if t["end"] < start or t["start"] > end:
            continue
        ov = max(0.0, min(end, t["end"]) - max(start, t["start"]))
        if ov > best_ov:
            best, best_ov = t["speaker"], ov
    return best


class Align(Stage):
    """Give each Whisper segment the speaker it overlaps most in the diarization. A segment that spans a speaker
    change goes wholly to one speaker; that is a known limit of segment-level alignment."""
    name = "align"
    label = "Align speakers to text"
    version = "1"
    needs = ("transcript", "rttm_standard", "rttm_exclusive")
    makes = ("aligned",)
    settings = {
        "overlap_mode": Setting("standard", "Which diarization to align against: 'standard' keeps overlapping "
                                "speech, 'exclusive' allows one speaker at a time.", choices=["standard", "exclusive"]),
    }

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        segs = json.loads(inputs["transcript"].read_text(encoding="utf-8"))["segments"]
        turns = load_rttm(inputs[f"rttm_{settings['overlap_mode']}"])
        aligned = [{"start": s["start"], "end": s["end"], "text": s["text"],
                    "speaker": assign_speaker(s["start"], s["end"], turns)} for s in segs]
        summary = defaultdict(lambda: {"total_speech_s": 0.0, "segment_count": 0, "word_count": 0})
        for s in aligned:
            d = summary[s["speaker"]]
            d["total_speech_s"] += s["end"] - s["start"]
            d["segment_count"] += 1
            d["word_count"] += len(s["text"].split())
        summary = {k: {**v, "total_speech_s": round(v["total_speech_s"], 1)} for k, v in sorted(summary.items())}
        (out / "aligned.json").write_text(json.dumps({"overlap_mode": settings["overlap_mode"],
                                                      "speaker_summary": summary, "segments": aligned},
                                                     indent=1, ensure_ascii=False), encoding="utf-8")
        unknown = sum(s["speaker"] == "UNKNOWN" for s in aligned)
        share = unknown / len(aligned) if aligned else 0.0
        return Result(files={"aligned": "aligned.json"},
                      metrics={"segments": len(aligned), "speakers": len(summary), "unattributed": unknown},
                      checks=[Check("unattributed", "warn" if share > 0.1 else "ok", round(share, 3),
                                    f"{unknown} of {len(aligned)} segments overlap no speaker turn")])
