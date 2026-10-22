"""The library: every recording any run has touched, and what can be played, read and searched for each one.

A recording is an item (a video id, or a local file). A *view* of it is one run: the audio that run's Whisper heard,
its speaker-labelled transcript, its speaker blocks and its comments. A recording can have several views (the
left-channel run and the averaged one, say); the library opens the newest view that has a transcript.

Everything here reads run records and receipts; nothing is computed or changed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .store import Store

TEXT_STAGES = ("align", "transcribe")


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class Library:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------ runs and steps
    def _runs(self) -> list[dict]:
        """Run records, newest first."""
        recs = [_load(p) for p in self.store.list_runs()]
        recs = [r for r in recs if r]
        return sorted(recs, key=lambda r: r.get("created_utc") or "", reverse=True)

    @staticmethod
    def _steps(rec: dict) -> list[dict]:
        """The run's steps, with the stage type and input wiring filled in for records made before those existed."""
        steps, latest = [], {}
        from . import stages as registry
        for s in rec["stages"]:
            stage = s.get("stage", s["name"])
            try:
                cls = registry.get(stage)
                needs, makes = cls.needs, cls.makes
            except Exception:  # a plugin stage that is not installed here
                needs, makes = (), ()
            frm = s.get("from") or {n: latest.get(n) for n in needs}
            steps.append({"name": s["name"], "stage": stage, "from": frm})
            latest.update({m: s["name"] for m in makes})
        return steps

    def _receipt(self, rec: dict, item: str, step: str | None) -> tuple[str, dict] | None:
        if not step:
            return None
        cell = rec["cells"].get(item, {}).get(step) or {}
        if not cell.get("receipt"):
            return None
        r = _load(self.store.root / cell["receipt"])
        return (cell["receipt"], r) if r else None

    def _view(self, rec: dict, item: str) -> dict:
        """Which receipts make up this run's view of one item."""
        steps = self._steps(rec)
        last = {}
        for s in steps:
            last[s["stage"]] = s
        tr = last.get("transcribe")
        audio_step = tr["from"].get("audio") if tr else (last.get("audio") or {}).get("name")
        dl = last.get("download")
        want = {"download": dl and dl["name"], "audio": audio_step, "transcribe": tr and tr["name"],
                "align": (last.get("align") or {}).get("name"), "blocks": (last.get("blocks") or {}).get("name"),
                "extract_comments": (last.get("extract_comments") or {}).get("name")}
        return {k: self._receipt(rec, item, v) for k, v in want.items()}

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
                if view["download"] and entry["date"] is None:
                    info = self._info(view["download"])
                    entry.update(title=info.get("title") or entry["title"], date=_date(info),
                                 duration_s=info.get("duration"), channel=info.get("channel"))
                has = [k for k in ("audio", "align", "transcribe", "extract_comments") if view[k]]
                if has:
                    fails = sum(c["status"] == "fail" for k, v in view.items() if v and k != "download"
                                for c in v[1].get("checks", []))
                    fails += sum(c["status"] == "fail" for cell in rec["cells"][iid].values()
                                 for c in cell.get("run_checks", []))
                    entry["views"].append({"run_id": rec["run_id"], "run_name": rec["name"],
                                           "created_utc": rec.get("created_utc"), "has": has, "failed_checks": fails,
                                           "words": view["transcribe"][1]["metrics"].get("words")
                                           if view["transcribe"] else None})
        recs = [r for r in out.values() if r["views"]]
        for r in recs:
            # the newest view with words in it and no failed check, else the newest with any transcript
            best = sorted(r["views"], key=lambda v: (bool(v["words"]), not v["failed_checks"],
                                                     "transcribe" in v["has"], v["created_utc"] or ""), reverse=True)
            r["default_run"] = best[0]["run_id"]
        return sorted(recs, key=lambda r: (r["date"] or "", r["title"]), reverse=True)

    def _info(self, dl: tuple[str, dict]) -> dict:
        rel, r = dl
        out = r["outputs"].get("info")
        if not out:
            return {}
        return _load((self.store.root / rel).parent / out["file"]) or {}

    # ------------------------------------------------------------------ one recording
    def recording(self, run_id: str, item: str) -> dict:
        rec = _load(self.store.run_dir(run_id) / "run.json")
        if not rec or item not in rec["cells"]:
            raise KeyError(f"{run_id} / {item}")
        view = self._view(rec, item)
        it = next(i for i in rec["items"] if i["id"] == item)
        info = self._info(view["download"]) if view["download"] else {}
        segments, speakers, source = [], {}, None
        for stage in TEXT_STAGES:
            if view[stage]:
                rel, r = view[stage]
                name = "aligned" if stage == "align" else "transcript"
                d = _load((self.store.root / rel).parent / r["outputs"][name]["file"]) or {}
                segments = [{"start": s["start"], "end": s["end"], "text": s.get("text", "").strip(),
                             "speaker": s.get("speaker")} for s in d.get("segments", [])]
                source = stage
                break
        for s in segments:
            if s["speaker"]:
                speakers[s["speaker"]] = speakers.get(s["speaker"], 0) + s["end"] - s["start"]
        comments = []
        if view["extract_comments"]:
            rel, r = view["extract_comments"]
            d = _load((self.store.root / rel).parent / r["outputs"]["comments"]["file"]) or {}
            comments = [{k: c.get(k) for k in ("comment_number", "timestamp", "start_s", "end_s", "speaker_name",
                                               "residence_or_affiliation", "agenda_item", "position", "summary")}
                        for c in d.get("comments", [])]
        problems = []
        for step, cell in rec["cells"][item].items():
            for c in cell.get("run_checks", []):
                problems.append({"stage": step, **c})
            if cell.get("checks") in ("warn", "fail") and cell.get("receipt"):
                r = _load(self.store.root / cell["receipt"]) or {}
                problems += [{"stage": step, **c} for c in r.get("checks", []) if c["status"] != "ok"]
        audio = view["audio"]
        return {
            "run_id": run_id, "run_name": rec["name"], "item": it, "title": info.get("title") or it.get("title") or item,
            "date": _date(info), "duration_s": info.get("duration"), "channel": info.get("channel"),
            "url": it.get("url"),
            "audio": {"receipt": audio[0], "name": "audio", "settings": audio[1]["settings"]} if audio else None,
            "media": {"receipt": view["download"][0], "name": "media",
                      "has_video": bool(info.get("vcodec") and info.get("vcodec") != "none")} if view["download"] else None,
            "text_from": source, "segments": segments,
            "speakers": dict(sorted(speakers.items(), key=lambda kv: -kv[1])), "comments": comments,
            "problems": problems,
        }

    # ------------------------------------------------------------------ search
    def search(self, query: str, limit: int = 200) -> list[dict]:
        """Every transcript line, across all recordings, that contains every word of the query (any case). Each
        recording is searched in its default view."""
        words = [stem(w) for w in re.findall(r"[\w']+", query.lower()) if w]
        if not words:
            return []
        hits = []
        for r in self.recordings():
            try:
                view = self.recording(r["default_run"], r["id"])
            except KeyError:
                continue
            for s in view["segments"]:
                low = s["text"].lower()
                if all(w in low for w in words):
                    hits.append({"id": r["id"], "run_id": r["default_run"], "title": r["title"], "date": r["date"],
                                 "start": s["start"], "speaker": s["speaker"], "text": s["text"]})
                    if len(hits) >= limit:
                        return hits
        return hits


def stem(word: str) -> str:
    """A crude stem, so 'values', 'parking' and 'proposed' find 'value', 'park' and 'propose'."""
    for suffix in ("ies", "ing", "es", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _date(info: dict) -> str | None:
    """The meeting date from a title like '... - October 28, 2025', else the upload date (YYYY-MM-DD)."""
    from datetime import datetime
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", info.get("title") or "")
    if m:
        try:
            return datetime.strptime(m.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    u = info.get("upload_date")
    return f"{u[:4]}-{u[4:6]}-{u[6:]}" if u and len(u) == 8 else None
