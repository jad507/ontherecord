"""The library: every recording any run has touched, and what can be played, read and searched for each one.

A recording is an item (a video id, or a local file). A *view* of it is one run. Nothing here knows a stage by
name: a run record lists each step's artifact types (`makes`) and wiring (`from`, `from_artifact`), and the view is
found by type (audit A2):

- the transcript: the run's `segments` with text, preferring ones with speakers, then segment-sized units (Whisper's
  lines over turns), then the latest; never `clip` units (excerpts), which cover only part of a recording;
- the audio: the `audio` that transcript's inputs trace back to, so the player plays what the transcriber heard;
- the media and info: the latest `media` and `info`;
- the codes: every `annotations` artifact, by step.

A recording can have several views (the left-channel run and the averaged one, say); the library opens the best
one. Runs recorded before artifacts had types are not read. Everything here reads run records, receipts and their
files; nothing is computed or changed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .store import Store


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def typed(rec: dict) -> bool:
    """Whether a run record lists its steps' artifact types (every run since the shared formats)."""
    return all("makes" in s for s in rec.get("stages") or [])


def output_path(store_root: Path, rec: dict, item: str, step: str, artifact: str) -> Path | None:
    """The file one step made for one item, or None when it has none (not run, failed, blocked)."""
    cell = rec["cells"].get(item, {}).get(step) or {}
    if not cell.get("receipt"):
        return None
    r = _load(store_root / cell["receipt"])
    out = ((r or {}).get("outputs") or {}).get(artifact)
    if not out:
        return None
    p = Path(out["file"])
    return p if p.is_absolute() else (store_root / cell["receipt"]).parent / p


def of_type(rec: dict, typ: str) -> list[tuple[str, str]]:
    """Every (step, artifact) of this type in the run, in step order."""
    return [(s["name"], a) for s in rec["stages"] for a, t in s["makes"].items() if t == typ]


def trace_to(rec: dict, step: str, typ: str) -> tuple[str, str] | None:
    """The artifact of type `typ` that `step`'s inputs come from, followed back through the wiring (inputs in the
    order the stage declares them): for a transcript, the audio its transcriber heard."""
    by = {s["name"]: s for s in rec["stages"]}
    todo, seen = [step], set()
    while todo:
        s = by.get(todo.pop(0))
        if not s or s["name"] in seen:
            continue
        seen.add(s["name"])
        for need, src in (s.get("from") or {}).items():
            art = (s.get("from_artifact") or {}).get(need)
            if src in by and by[src]["makes"].get(art) == typ:
                return src, art
        todo += list((s.get("from") or {}).values())
    return None


_SEGS: dict[Path, dict] = {}   # store files never change, so what they hold can be remembered by path


def segments_summary(path: Path) -> dict:
    """What a segments file holds: unit kind, whether it has text, speakers and words, its word count, language."""
    if path not in _SEGS:
        d = _load(path) or {}
        units = d.get("units") or []
        _SEGS[path] = {"unit": d.get("unit"), "text": any((u.get("text") or "").strip() for u in units),
                       "speakers": any(u.get("speaker") for u in units), "words": any(u.get("words") for u in units),
                       "n_words": sum(len((u.get("text") or "").split()) for u in units),
                       "language": d.get("language")}
    return _SEGS[path]


def best_transcript(store_root: Path, rec: dict, item: str) -> tuple[str, str, Path] | None:
    """The run's transcript for this item: (step, artifact, path), by the ranking in the module docstring."""
    best, rank = None, None
    for n, (step, art) in enumerate(of_type(rec, "segments")):
        p = output_path(store_root, rec, item, step, art)
        if not p:
            continue
        s = segments_summary(p)
        r = (s["text"], s["speakers"], s["unit"] == "segment", n)
        if s["text"] and s["unit"] != "clip" and (rank is None or r > rank):   # clips cover part of a recording
            best, rank = (step, art, p), r
    return best


def latest_with(store_root: Path, rec: dict, item: str, typ: str, want=lambda p: True) -> tuple[str, str, Path] | None:
    """The latest artifact of a type that this item has (and that `want` accepts)."""
    for step, art in reversed(of_type(rec, typ)):
        p = output_path(store_root, rec, item, step, art)
        if p and want(p):
            return step, art, p
    return None


def units_of(store_root: Path, rec: dict, item: str, step: str) -> dict[str, dict]:
    """The units an annotations step coded: its segments input, by id."""
    s = next(x for x in rec["stages"] if x["name"] == step)
    for need, typ in (s.get("needs") or {}).items():
        if typ == "segments":
            src, art = s["from"][need], s["from_artifact"][need]
            p = output_path(store_root, rec, item, src, art)
            if p:
                return {u["id"]: u for u in (_load(p) or {}).get("units", [])}
    return {}


def coded(store_root: Path, rec: dict, item: str, step: str, art: str) -> dict | None:
    """One annotations artifact, each annotation with its units' times, speakers and text laid over it."""
    p = output_path(store_root, rec, item, step, art)
    d = _load(p) if p else None
    if not d:
        return None
    units = units_of(store_root, rec, item, step)
    out = []
    for a in d.get("annotations", []):
        t, row = a["target"], {"id": a["id"], "code": a["code"], "attributes": a.get("attributes") or {},
                               "note": a.get("note", "")}
        ids = t.get("units") or ([t["unit"]] if "unit" in t else [])
        us = [units[i] for i in ids if i in units]
        if us:
            row.update(units=ids, start=us[0]["start"], end=us[-1]["end"],
                       speakers=sorted({u.get("speaker", "") for u in us} - {""}),
                       text=" ".join((u.get("text") or "").strip() for u in us))
        elif "start" in t:
            row.update(start=t["start"], end=t["end"], speakers=[], text="")
        out.append(row)
    return {"step": step, "artifact": art, "codebook": d.get("codebook"), "by": d.get("by"), "annotations": out}


class Library:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------ runs
    def _runs(self) -> list[dict]:
        """Typed run records, newest first."""
        recs = [_load(p) for p in self.store.list_runs()]
        recs = [r for r in recs if r and typed(r)]
        return sorted(recs, key=lambda r: r.get("created_utc") or "", reverse=True)

    def _receipt(self, rec: dict, item: str, step: str | None) -> tuple[str, dict] | None:
        cell = rec["cells"].get(item, {}).get(step) or {} if step else {}
        r = _load(self.store.root / cell["receipt"]) if cell.get("receipt") else None
        return (cell["receipt"], r) if r else None

    def _view(self, rec: dict, item: str) -> dict:
        """Which artifacts make up this run's view of one item."""
        root = self.store.root
        tr = best_transcript(root, rec, item)
        audio = trace_to(rec, tr[0], "audio") if tr else None
        if not audio:
            audio = next(iter(reversed(of_type(rec, "audio"))), None)
        media = next(iter(reversed(of_type(rec, "media"))), None)
        info = next(iter(reversed(of_type(rec, "info"))), None)
        codes = [(s, a) for s, a in of_type(rec, "annotations") if output_path(root, rec, item, s, a)]
        return {"transcript": tr,
                "audio": audio and (audio[1], self._receipt(rec, item, audio[0])),
                "media": media and (media[1], self._receipt(rec, item, media[0])),
                "info": info and output_path(root, rec, item, *info), "codes": codes}

    # ------------------------------------------------------------------ the list
    def recordings(self) -> list[dict]:
        out: dict[str, dict] = {}
        for rec in self._runs():
            for it in rec["items"]:
                iid = it["id"]
                view = self._view(rec, iid)
                entry = out.setdefault(iid, {"id": iid, "url": it.get("url"), "path": it.get("path"),
                                             "title": it.get("title") or iid, "date": None, "duration_s": None,
                                             "views": []})
                if view["info"] and entry["date"] is None:
                    info = _load(view["info"]) or {}
                    entry.update(title=info.get("title") or entry["title"], date=_date(info),
                                 duration_s=info.get("duration"), channel=info.get("channel"))
                tr = view["transcript"]
                summ = segments_summary(tr[2]) if tr else {}
                has = [k for k, v in (("audio", view["audio"] and view["audio"][1]),
                                      ("transcript", tr or latest_with(self.store.root, rec, iid, "segments")),
                                      ("speakers", summ.get("speakers")), ("codes", view["codes"])) if v]
                if has:
                    fails = sum(c["status"] == "fail" for cell in rec["cells"][iid].values()
                                for c in cell.get("run_checks", []))
                    fails += sum(1 for cell in rec["cells"][iid].values() if cell.get("checks") == "fail")
                    entry["views"].append({"run_id": rec["run_id"], "run_name": rec["name"],
                                           "created_utc": rec.get("created_utc"), "has": has, "failed_checks": fails,
                                           "words": summ.get("n_words") if tr else
                                           0 if latest_with(self.store.root, rec, iid, "segments") else None})
        recs = [r for r in out.values() if r["views"]]
        for r in recs:
            # the newest view with words in it and no failed check, else the newest with any transcript
            best = sorted(r["views"], key=lambda v: (bool(v["words"]), not v["failed_checks"],
                                                     "transcript" in v["has"], v["created_utc"] or ""), reverse=True)
            r["default_run"] = best[0]["run_id"]
        return sorted(recs, key=lambda r: (r["date"] or "", r["title"]), reverse=True)

    # ------------------------------------------------------------------ one recording
    def recording(self, run_id: str, item: str) -> dict:
        rec = _load(self.store.run_dir(run_id) / "run.json")
        if not rec or not typed(rec) or item not in rec["cells"]:
            raise KeyError(f"{run_id} / {item}")
        view = self._view(rec, item)
        it = next(i for i in rec["items"] if i["id"] == item)
        info = (_load(view["info"]) or {}) if view["info"] else {}
        segments, speakers, source, language = [], {}, None, None
        if view["transcript"]:
            step, art, p = view["transcript"]
            d = _load(p) or {}
            segments = [{"id": u["id"], "start": u["start"], "end": u["end"], "text": (u.get("text") or "").strip(),
                         "speaker": u.get("speaker")} for u in d.get("units", [])]
            source, language = step, d.get("language")
        for s in segments:
            if s["speaker"]:
                speakers[s["speaker"]] = speakers.get(s["speaker"], 0) + s["end"] - s["start"]
        codes = [c for c in (coded(self.store.root, rec, item, s, a) for s, a in view["codes"]) if c]
        problems = []
        for step, cell in rec["cells"][item].items():
            for c in cell.get("run_checks", []):
                problems.append({"stage": step, **c})
            if cell.get("checks") in ("warn", "fail") and cell.get("receipt"):
                r = _load(self.store.root / cell["receipt"]) or {}
                problems += [{"stage": step, **c} for c in r.get("checks", []) if c["status"] != "ok"]
        audio, media = view["audio"], view["media"]
        return {
            "run_id": run_id, "run_name": rec["name"], "item": it, "title": info.get("title") or it.get("title") or item,
            "date": _date(info), "duration_s": info.get("duration"), "channel": info.get("channel"),
            "url": it.get("url"), "language": language,
            "audio": {"receipt": audio[1][0], "name": audio[0], "settings": audio[1][1]["settings"]}
            if audio and audio[1] else None,
            "media": {"receipt": media[1][0], "name": media[0],
                      "has_video": bool(info.get("vcodec") and info.get("vcodec") != "none")}
            if media and media[1] else None,
            "text_from": source, "segments": segments,
            "speakers": dict(sorted(speakers.items(), key=lambda kv: -kv[1])), "codes": codes,
            "problems": problems,
        }

    # ------------------------------------------------------------------ search
    def search(self, query: str, limit: int = 200) -> list[dict]:
        """Every transcript line, across all recordings, that contains every word of the query (any case). Each
        recording is searched in its default view. Words are stemmed only in English recordings."""
        plain = [w for w in re.findall(r"[\w']+", query.lower()) if w]
        if not plain:
            return []
        stemmed = [stem(w) for w in plain]
        hits = []
        for r in self.recordings():
            try:
                view = self.recording(r["default_run"], r["id"])
            except KeyError:
                continue
            words = stemmed if (view["language"] or "en") == "en" else plain
            for s in view["segments"]:
                low = s["text"].lower()
                if all(w in low for w in words):
                    hits.append({"id": r["id"], "run_id": r["default_run"], "title": r["title"], "date": r["date"],
                                 "start": s["start"], "speaker": s["speaker"], "text": s["text"]})
                    if len(hits) >= limit:
                        return hits
        return hits


def stem(word: str) -> str:
    """A crude English stem, so 'values', 'parking' and 'proposed' find 'value', 'park' and 'propose'."""
    for suffix in ("ies", "ing", "es", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _date(info: dict) -> str | None:
    """The recording's date: its upload date (YYYY-MM-DD). Never guessed from words in a title (audit D8)."""
    u = info.get("upload_date")
    return f"{u[:4]}-{u[4:6]}-{u[6:]}" if u and len(u) == 8 else None


# ---------------------------------------------------------------------------------------------- comparing two runs
def headline(stage: str) -> tuple[str, str] | None:
    """The one metric that says most about a stage's output, and its wording: the stage's own `headline`."""
    from . import stages as registry
    try:
        return registry.get(stage).headline
    except Exception:  # a plugin stage that is not installed here
        return None


def compare(store: Store, run_a: str, run_b: str) -> dict:
    """Two runs side by side, for the items they share: per stage, whether the outputs are byte-identical, the
    headline number of each, the settings that differ, and the worst check on each side."""
    ra, rb = (_load(store.run_dir(r) / "run.json") for r in (run_a, run_b))
    if not ra or not rb:
        raise KeyError(run_a if not ra else run_b)

    def steps(rec):
        return [(s["name"], s.get("stage", s["name"])) for s in rec["stages"]]

    sa, sb = steps(ra), steps(rb)
    pairs = []  # (name in a, name in b, stage type): same name first, else the only step of that type on each side
    for name, stage in sa:
        if any(n == name for n, _ in sb):
            pairs.append((name, name, stage))
        else:
            ta, tb = [n for n, s in sa if s == stage], [n for n, s in sb if s == stage]
            if len(ta) == 1 and len(tb) == 1:
                pairs.append((name, tb[0], stage))
    for name, stage in sb:
        if not any(p[1] == name for p in pairs):
            pairs.append((None, name, stage))
    items = [i["id"] for i in ra["items"] if any(j["id"] == i["id"] for j in rb["items"])]
    rows = {}
    for item in items:
        row = {}
        for na, nb, stage in pairs:
            ca = ra["cells"].get(item, {}).get(na) if na else None
            cb = rb["cells"].get(item, {}).get(nb)
            reca = _load(store.root / ca["receipt"]) if ca and ca.get("receipt") else None
            recb = _load(store.root / cb["receipt"]) if cb and cb.get("receipt") else None
            cell = {"a": {"status": ca.get("status") if ca else None, "checks": ca.get("checks") if ca else None},
                    "b": {"status": cb.get("status") if cb else None, "checks": cb.get("checks") if cb else None}}
            head = headline(stage)
            for side, rec in (("a", reca), ("b", recb)):
                if rec and head:
                    cell[side]["value"], cell["unit"] = rec["metrics"].get(head[0]), head[1]
                if rec:
                    cell[side]["outputs"] = {k: v["sha256"] for k, v in rec["outputs"].items()}
                    cell[side]["key"] = rec["key"]
            if reca and recb:
                cell["same"] = cell["a"]["outputs"] == cell["b"]["outputs"]
                sa_, sb_ = reca["settings"], recb["settings"]
                flat = lambda v: v.get("sha256") if isinstance(v, dict) and "sha256" in v else v  # noqa: E731
                cell["settings_differ"] = {k: [flat(sa_.get(k)), flat(sb_.get(k))] for k in sorted(set(sa_) | set(sb_))
                                           if flat(sa_.get(k)) != flat(sb_.get(k))}
                ia = {k: v["sha256"] for k, v in reca["inputs"].items()}
                ib = {k: v["sha256"] for k, v in recb["inputs"].items()}
                cell["inputs_differ"] = sorted(k for k in set(ia) | set(ib) if ia.get(k) != ib.get(k))
            row[nb or na] = {"stage": stage, "a_step": na, "b_step": nb, **cell}
        rows[item] = row
    return {"a": {k: ra[k] for k in ("run_id", "name", "description", "created_utc")},
            "b": {k: rb[k] for k in ("run_id", "name", "description", "created_utc")},
            "columns": [{"name": nb or na, "stage": stage, "a_step": na, "b_step": nb} for na, nb, stage in pairs],
            "items": {i["id"]: i for i in rb["items"] if i["id"] in items}, "rows": rows}
