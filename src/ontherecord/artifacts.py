"""The shared artifact formats, checked where they are written.

A stage declares the type of each output (stages/base.py, TYPES). The worker calls validate() on every output of a
checked type before it writes the receipt, so a malformed file fails its stage, with the field named, and no reader
has to be defensive. This module is a validator and two loaders. It reads one shape per format: there is no reader
for older shapes, and none should be added (audit K3).

segments/1: stretches of one recording.

    {"format": "segments/1", "unit": "segment" | "turn" | "clip" | "interval",
     "units": [{"id": "t0007", "start": 12.3, "end": 19.8,              # required
                "text": "...", "speaker": "SPEAKER_02",                  # optional
                "from": ["s0031", "s0032"],                              # optional: the input units it was built from
                "words": [{"start", "end", "word", "p", "alts"}]}]}     # optional: p and alts optional too

    A unit with no text is legal (a stretch of video).

annotations/1: labels on units or on spans of time.

    {"format": "annotations/1", "codebook": {"name", "sha256"} | null, "by": {"kind": "model" | "person", "id"},
     "annotations": [{"id", "code", "target": {"units": [...]} | {"start", "end"}
                                              | {"unit", "word_from", "word_to"},
                      "attributes": {...}, "note": ""}]}

speakers: RTTM, one SPEAKER line per turn.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

SEGMENTS = "segments/1"
ANNOTATIONS = "annotations/1"
UNIT_KINDS = ("segment", "turn", "clip", "interval")
CHECKED = ("segments", "annotations", "speakers")


class ArtifactError(ValueError):
    pass


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _span(where: str, d: dict) -> None:
    for k in ("start", "end"):
        if not _num(d.get(k)):
            raise ArtifactError(f"{where}: {k} must be a number, got {d.get(k)!r}")
    if d["start"] < 0:
        raise ArtifactError(f"{where}: start {d['start']} is before the recording begins")
    if d["end"] < d["start"]:
        raise ArtifactError(f"{where}: end {d['end']} is before its start {d['start']}")


def check_segments(doc) -> dict:
    if not isinstance(doc, dict) or doc.get("format") != SEGMENTS:
        raise ArtifactError(f"format must be {SEGMENTS!r}")
    if doc.get("unit") not in UNIT_KINDS:
        raise ArtifactError(f"unit must be one of {', '.join(UNIT_KINDS)}, got {doc.get('unit')!r}")
    units = doc.get("units")
    if not isinstance(units, list):
        raise ArtifactError("units must be a list")
    seen: set[str] = set()
    for i, u in enumerate(units):
        where = f"units[{i}]"
        if not isinstance(u, dict):
            raise ArtifactError(f"{where} must be an object")
        uid = u.get("id")
        if not isinstance(uid, str) or not uid:
            raise ArtifactError(f"{where}: id is missing")
        if uid in seen:
            raise ArtifactError(f"{where}: id {uid!r} is used twice")
        seen.add(uid)
        where = f"unit {uid}"
        _span(where, u)
        for k in ("text", "speaker"):
            if k in u and not isinstance(u[k], str):
                raise ArtifactError(f"{where}: {k} must be text")
        if "from" in u and not (isinstance(u["from"], list) and all(isinstance(x, str) for x in u["from"])):
            raise ArtifactError(f"{where}: from must be a list of unit ids")
        if "words" in u:
            if not isinstance(u["words"], list):
                raise ArtifactError(f"{where}: words must be a list")
            for j, w in enumerate(u["words"]):
                if not isinstance(w, dict) or not isinstance(w.get("word"), str):
                    raise ArtifactError(f"{where}: words[{j}].word must be text")
                _span(f"{where} words[{j}]", w)
    return doc


def check_annotations(doc) -> dict:
    if not isinstance(doc, dict) or doc.get("format") != ANNOTATIONS:
        raise ArtifactError(f"format must be {ANNOTATIONS!r}")
    cb = doc.get("codebook")
    if cb is not None and not (isinstance(cb, dict) and isinstance(cb.get("name"), str)
                               and isinstance(cb.get("sha256"), str)):
        raise ArtifactError("codebook must be {name, sha256} or null")
    by = doc.get("by")
    if not isinstance(by, dict) or by.get("kind") not in ("model", "person") or not isinstance(by.get("id"), str):
        raise ArtifactError('by must be {"kind": "model" | "person", "id": "..."}')
    anns = doc.get("annotations")
    if not isinstance(anns, list):
        raise ArtifactError("annotations must be a list")
    seen: set[str] = set()
    for i, a in enumerate(anns):
        where = f"annotations[{i}]"
        if not isinstance(a, dict):
            raise ArtifactError(f"{where} must be an object")
        aid = a.get("id")
        if not isinstance(aid, str) or not aid:
            raise ArtifactError(f"{where}: id is missing")
        if aid in seen:
            raise ArtifactError(f"{where}: id {aid!r} is used twice")
        seen.add(aid)
        where = f"annotation {aid}"
        if not isinstance(a.get("code"), str) or not a["code"]:
            raise ArtifactError(f"{where}: code is missing")
        t = a.get("target")
        if not isinstance(t, dict):
            raise ArtifactError(f"{where}: target is missing")
        if "units" in t:
            if not (isinstance(t["units"], list) and t["units"] and all(isinstance(x, str) for x in t["units"])):
                raise ArtifactError(f"{where}: target.units must be a non-empty list of unit ids")
        elif "unit" in t:
            if not isinstance(t["unit"], str):
                raise ArtifactError(f"{where}: target.unit must be a unit id")
            f, to = t.get("word_from"), t.get("word_to")
            if not (isinstance(f, int) and isinstance(to, int) and 0 <= f <= to):
                raise ArtifactError(f"{where}: target needs word_from <= word_to, both whole numbers from 0")
        elif "start" in t:
            _span(f"{where} target", t)
        else:
            raise ArtifactError(f"{where}: target must name units, a unit's words, or a start and end")
        if "attributes" in a and not isinstance(a["attributes"], dict):
            raise ArtifactError(f"{where}: attributes must be an object")
        if "note" in a and not isinstance(a["note"], str):
            raise ArtifactError(f"{where}: note must be text")
    return doc


def check_rttm(text: str) -> None:
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        f = line.split()
        if len(f) < 8 or f[0] != "SPEAKER":
            raise ArtifactError(f"line {n}: an RTTM line is 'SPEAKER file channel start duration <NA> <NA> speaker ...'")
        try:
            start, dur = float(f[3]), float(f[4])
        except ValueError:
            raise ArtifactError(f"line {n}: start and duration must be numbers") from None
        if start < 0 or dur < 0:
            raise ArtifactError(f"line {n}: start and duration must not be negative")


def _json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ArtifactError(f"not readable JSON: {e}") from None


def validate(path: Path, typ: str) -> None:
    """Raise ArtifactError, naming the field, when a file of a checked type is malformed. Other types pass."""
    if typ == "segments":
        check_segments(_json(path))
    elif typ == "annotations":
        check_annotations(_json(path))
    elif typ == "speakers":
        check_rttm(Path(path).read_text(encoding="utf-8"))


def load_segments(path: Path) -> dict:
    return check_segments(_json(path))


def load_annotations(path: Path) -> dict:
    return check_annotations(_json(path))


def segments(units: list[dict], unit: str = "segment") -> dict:
    """A segments document, for a stage to write."""
    return {"format": SEGMENTS, "unit": unit, "units": units}


def write_json(path: Path, doc: dict) -> None:
    """How every stage writes a JSON artifact: UTF-8, indented, keys in the order given (byte-identical reruns)."""
    Path(path).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
