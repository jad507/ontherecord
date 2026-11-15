"""Merge aligned segments into speaker blocks, the unit the coders read. Ported from
hands_on_dl/audio_pipeline/group_speakers.py and extract_commenter_blocks.py."""
from __future__ import annotations

import json
from pathlib import Path

from .base import Check, Context, Item, Result, Setting, Stage


def merge_segments(segments: list[dict], max_gap_s: float) -> list[dict]:
    """A new block starts when the speaker changes or the same speaker pauses longer than max_gap_s."""
    blocks: list[dict] = []
    for s in segments:
        b = blocks[-1] if blocks else None
        if b and s["speaker"] == b["speaker"] and s["start"] - b["end"] <= max_gap_s:
            b["end"] = s["end"]
            b["texts"].append(s["text"])
            b["segment_count"] += 1
        else:
            blocks.append({"speaker": s["speaker"], "start": s["start"], "end": s["end"], "texts": [s["text"]],
                           "segment_count": 1})
    return blocks


class Blocks(Stage):
    """Join each speaker's consecutive segments into blocks, and guess who is meeting staff ('recurring': talks a
    lot, in short bursts) and who might be a member of the public. The guess only orders the coder's attention."""
    name = "blocks"
    label = "Build speaker blocks"
    version = "1"
    needs = {"aligned": "other", "info": "info"}
    makes = {"blocks": "other"}
    settings = {
        "max_gap_s": Setting(30.0, "Merge a speaker's segments separated by at most this many seconds."),
        "recurring_threshold_s": Setting(180.0, "A speaker with more total speech than this ..."),
        "avg_segment_threshold_s": Setting(15.0, "... and average segments shorter than this is 'recurring'."),
    }

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        aligned = json.loads(inputs["aligned"].read_text(encoding="utf-8"))
        info = json.loads(inputs["info"].read_text(encoding="utf-8"))
        raw = merge_segments(aligned["segments"], settings["max_gap_s"])
        classes = {}
        for spk, st in aligned["speaker_summary"].items():
            avg = st["total_speech_s"] / st["segment_count"] if st["segment_count"] else 0.0
            recurring = st["total_speech_s"] > settings["recurring_threshold_s"] and avg < settings["avg_segment_threshold_s"]
            classes[spk] = {"category": "recurring" if recurring else "commenter_candidate",
                            "total_speech_s": st["total_speech_s"], "segment_count": st["segment_count"],
                            "avg_segment_s": round(avg, 1), "word_count": st["word_count"],
                            "block_count": sum(b["speaker"] == spk for b in raw)}
        blocks = []
        for i, b in enumerate(raw):
            text = " ".join(b["texts"]).strip()
            blocks.append({"block_id": i, "speaker": b["speaker"],
                           "category": classes.get(b["speaker"], {}).get("category", "commenter_candidate"),
                           "start": round(b["start"], 3), "end": round(b["end"], 3),
                           "duration_s": round(b["end"] - b["start"], 1), "segment_count": b["segment_count"],
                           "word_count": len(text.split()), "text": text})
        doc = {"title": info.get("title") or item.title or item.id, "video_id": info.get("id") or item.id,
               "upload_date": info.get("upload_date", ""), "rttm_mode": aligned["overlap_mode"],
               "speakers": classes, "blocks": blocks}
        (out / "blocks.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        cands = sum(b["category"] == "commenter_candidate" for b in blocks)
        return Result(files={"blocks": "blocks.json"},
                      metrics={"blocks": len(blocks), "speakers": len(classes), "commenter_candidate_blocks": cands},
                      checks=[Check("blocks", "warn" if len(blocks) < 3 else "ok", len(blocks),
                                    f"{len(blocks)} blocks from {len(classes)} speakers")])
