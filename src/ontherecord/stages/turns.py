"""Speaker turns: consecutive segments of one speaker merged into one unit, the unit a coder reads.

Replaces three copies of the same logic (the `blocks` stage, the review set builder's turns and the Check set's).
Each turn names the segments it was built from (`from`), so a code on a turn can be followed down to them, and
carries their words when they have any (word_choices' doubtful words reach the Check page this way).
"""
from __future__ import annotations

from pathlib import Path

from ..artifacts import load_segments, segments, write_json
from .base import Check, Context, Item, Result, Setting, Stage, UserError


def merge(units: list[dict], max_gap_s: float, max_turn_s: float | None) -> list[dict]:
    """A new turn starts when the speaker changes, when the same speaker is silent for more than max_gap_s, or
    when adding the next segment would make the turn longer than max_turn_s (None: no limit). A turn cut for length
    continues in the next one; those parts are numbered ("part": [2, 5])."""
    out: list[dict] = []
    cont: list[bool] = []          # this turn continues the one before it (cut for length only)
    for u in units:
        t = out[-1] if out else None
        same = t is not None and u["speaker"] == t["speaker"] and u["start"] - t["end"] <= max_gap_s
        if same and (max_turn_s is None or u["end"] - t["start"] <= max_turn_s):
            t["end"] = u["end"]
            t["from"].append(u["id"])
            if u.get("text"):
                t["text"] = f"{t['text']} {u['text']}".strip()
            if "words" in t:
                t["words"] += u.get("words", [])
            continue
        out.append({"id": f"t{len(out):04d}", "start": u["start"], "end": u["end"], "text": u.get("text", ""),
                    "speaker": u["speaker"], "from": [u["id"]],
                    **({"words": list(u["words"])} if "words" in u else {})})
        cont.append(same)
    k = 0
    while k < len(out):
        j = k
        while j + 1 < len(out) and cont[j + 1]:
            j += 1
        if j > k:
            for n in range(k, j + 1):
                out[n]["part"] = [n - k + 1, j - k + 1]
        k = j + 1
    return out


class Turns(Stage):
    """Join each speaker's consecutive segments into turns. Takes the latest segments with speakers (align's, or a
    transcriber that names speakers itself) and keeps their words. A turn longer than max_turn_s is cut at a
    segment boundary into numbered parts."""
    name = "turns"
    label = "Speaker turns"
    version = "1"
    needs = {"units": "segments"}
    makes = {"turns": "segments"}
    headline = ("turns", "turns")
    settings = {
        "max_gap_s": Setting(5.0, "A speaker's segments separated by at most this many seconds of silence join one "
                             "turn. (Council recipes use 30.)"),
        "max_turn_s": Setting(60.0, "Longest turn in seconds; a longer one is cut at a segment boundary into parts. "
                              "null: no limit. (Council recipes use null.)"),
    }

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        doc = load_segments(inputs["units"])
        units = doc["units"]
        missing = sum(1 for u in units if not u.get("speaker"))
        if missing:
            raise UserError(f"turns: {missing} of {len(units)} segments have no speaker, so there are no turns to "
                            "find.", "Put align (after diarize) before turns, or use a transcriber that names "
                            "speakers.")
        limit = settings["max_turn_s"]
        tt = merge(units, float(settings["max_gap_s"]), None if limit is None else float(limit))
        out_doc = segments(tt, "turn")
        if doc.get("language"):
            out_doc["language"] = doc["language"]
        write_json(out / "turns.json", out_doc)
        speakers = len({t["speaker"] for t in tt})
        return Result(files={"turns": "turns.json"},
                      metrics={"turns": len(tt), "speakers": speakers, "segments": len(units),
                               "cut_for_length": sum(1 for t in tt if t.get("part", [1])[0] > 1)},
                      checks=[Check("turns", "warn" if len(tt) < 3 else "ok", len(tt),
                                    f"{len(tt)} turns from {speakers} speakers")])
