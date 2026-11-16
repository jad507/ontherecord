"""Excerpts: the stretches an earlier coding pass found, as units of their own, so a second pass codes only them.

A two-pass study: a first `code` step finds the stretches that matter (public comments in a council meeting, stories
in an interview) and a second `code` step labels each of them with a different codebook. This stage sits between the
two. Each excerpt is one annotation of the first pass: its units joined (text, words, times), with the
annotation's id as its own, so a second-pass code on excerpt `a0007` is a code on the first pass's `a0007`, and
`from` names the units it was cut from.

Excerpts are written as segments with `unit: clip`: they cover only part of a recording, so readers that want a
recording's transcript skip them (library.best_transcript).
"""
from __future__ import annotations

from pathlib import Path

from ..artifacts import load_annotations, load_segments, segments, write_json
from .base import Check, Context, Item, Result, Setting, Stage, UserError


def wanted_codes(text: str) -> set[str]:
    return {c.strip() for c in (text or "").split(",") if c.strip()}


def cut(units: list[dict], annotations: list[dict], codes: set[str]) -> list[dict]:
    """One excerpt per annotation whose code is in `codes` (all of them when empty), in the order given."""
    by_id = {u["id"]: u for u in units}
    out = []
    for a in annotations:
        if codes and a["code"] not in codes:
            continue
        t = a["target"]
        if "units" not in t:
            raise UserError(f"excerpts: annotation {a['id']} marks a span of time or a unit's words, not whole units; "
                            "excerpts can only be cut from codes on whole units.",
                            "Code with mode: find or classify on segments or turns first.")
        missing = [i for i in t["units"] if i not in by_id]
        if missing:
            raise UserError(f"excerpts: annotation {a['id']} names units {', '.join(missing[:5])}, which are not in the "
                            "segments this step was given.",
                            "Give excerpts the segments the coding step read, with from: {units: STEP}.")
        us = [by_id[i] for i in t["units"]]
        speakers = {u.get("speaker") for u in us}
        e = {"id": a["id"], "start": us[0]["start"], "end": us[-1]["end"],
             "text": " ".join((u.get("text") or "").strip() for u in us).strip(), "from": list(t["units"])}
        if len(speakers) == 1 and None not in speakers:
            e["speaker"] = us[0]["speaker"]
        if any("words" in u for u in us):
            e["words"] = [w for u in us for w in u.get("words", [])]
        out.append(e)
    return out


class Excerpts(Stage):
    """Turn a coding pass's finds into units of their own, so the next code step reads only them. Takes the latest
    segments and the latest annotations before it; they must be the segments that coding step read."""
    name = "excerpts"
    label = "Excerpts of what was found"
    version = "1"
    needs = {"units": "segments", "annotations": "annotations"}
    makes = {"excerpts": "segments"}
    headline = ("excerpts", "excerpts")
    settings = {
        "codes": Setting("", "Which codes to cut out, separated by commas (e.g. public_comment). Empty: every code."),
    }

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        doc = load_segments(inputs["units"])
        ann = load_annotations(inputs["annotations"])
        codes = wanted_codes(settings["codes"])
        ex = cut(doc["units"], ann["annotations"], codes)
        out_doc = segments(ex, "clip")
        if doc.get("language"):
            out_doc["language"] = doc["language"]
        write_json(out / "excerpts.json", out_doc)
        seconds = sum(e["end"] - e["start"] for e in ex)
        checks = [Check("excerpts", "ok", len(ex), f"{len(ex)} excerpts, {seconds / 60:.1f} minutes")]
        if ann["annotations"] and not ex:   # found things, none with these codes: likely a misspelled code
            found = sorted({a["code"] for a in ann["annotations"]})
            checks.append(Check("codes", "warn", 0, f"none of the {len(ann['annotations'])} annotations has code "
                                f"{', '.join(sorted(codes))}; they have {', '.join(found)}"))
        return Result(files={"excerpts": "excerpts.json"},
                      metrics={"excerpts": len(ex), "minutes": round(seconds / 60, 2), "units": len(doc["units"])},
                      checks=checks)
