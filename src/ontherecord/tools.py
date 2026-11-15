"""What an assistant may do, as a short list of plain tools. Written once, used twice: by the assistant inside the
GUI (a small local model) and by `ontherecord mcp` (Claude Desktop, Claude Code, any MCP client).

Built for a small model to use well:
- few tools, each one task a person would name, with no settings to get wrong (presets instead of recipes);
- answers are short plain text, already summarized, so the model only has to pass them on;
- quotes carry their recording, time and speaker, and a link that opens the player at that moment;
- tools that stop or start something big are marked `confirm`: the GUI shows the person a button instead of acting,
  and MCP clients ask the person before calling them. Nothing here deletes anything.
"""
from __future__ import annotations

import inspect
import json
import re
import typing
from dataclasses import dataclass
from pathlib import Path

from .config import MachineConfig
from .library import Library
from .store import Store

MAX_LINES = 30


@dataclass
class Tool:
    name: str
    fn: typing.Callable
    confirm: bool = False

    @property
    def description(self) -> str:
        return inspect.cleandoc(self.fn.__doc__ or "")

    def schema(self) -> dict:
        """The tool as an OpenAI-style function definition, from the method's signature."""
        props, required = {}, []
        hints = typing.get_type_hints(self.fn)
        for name, p in inspect.signature(self.fn).parameters.items():
            t = hints.get(name, str)
            if typing.get_origin(t) is list:
                props[name] = {"type": "array", "items": {"type": "string"}}
            else:
                props[name] = {"type": {str: "string", int: "integer", bool: "boolean", float: "number"}.get(t, "string")}
            if p.default is inspect.Parameter.empty:
                required.append(name)
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": {"type": "object", "properties": props,
                                                                "required": required}}}


def hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def link(run_id: str, item: str, t: float = 0) -> str:
    return f"#play/{run_id}/{item}/{int(t)}"


def coded_line(a: dict) -> str:
    """One coded stretch for the assistant: when, who, the code, its fields, and a little of the text."""
    fields = ", ".join(f"{k}: {v}" for k, v in a["attributes"].items() if v and len(str(v)) <= 40)
    summary = a["attributes"].get("summary") or (a.get("text") or "")[:160]
    who = ", ".join(a.get("speakers") or []) or "speaker"
    return (f"[{hms(a.get('start') or 0)}] {who} {a['code']}" + (f" ({fields})" if fields else "")
            + (f": {summary}" if summary else ""))


class Toolbox:
    def __init__(self, cfg: MachineConfig):
        from .jobs import JobQueue
        self.cfg = cfg
        self.store = Store(cfg.workspace)
        self.library = Library(self.store)
        self.queue = JobQueue(cfg)

    def tools(self) -> list[Tool]:
        return [Tool("find_recordings", self.find_recordings), Tool("search_transcripts", self.search_transcripts),
                Tool("recording_overview", self.recording_overview), Tool("read_transcript", self.read_transcript),
                Tool("coded", self.coded),
                Tool("transcribe", self.transcribe, confirm=True), Tool("jobs_status", self.jobs_status),
                Tool("cancel_job", self.cancel_job, confirm=True), Tool("devices", self.devices),
                Tool("import_device", self.import_device, confirm=True), Tool("review_progress", self.review_progress),
                Tool("make_review", self.make_review, confirm=True)]

    def call(self, name: str, args: dict) -> str:
        tool = next((t for t in self.tools() if t.name == name), None)
        if not tool:
            return f"There is no tool named {name!r}. The tools are: {', '.join(t.name for t in self.tools())}."
        try:
            return tool.fn(**{k: v for k, v in (args or {}).items()
                              if k in inspect.signature(tool.fn).parameters})
        except TypeError as e:
            return f"That call was missing something: {e}"
        except Exception as e:  # a tool must answer, not crash the conversation
            return f"That did not work: {type(e).__name__}: {e}"

    # ------------------------------------------------------------------ the library
    def _match(self, recording_id: str) -> dict | None:
        """A recording by its id, or by words of its title or its date ('November 20', '2025-11-20')."""
        recs = self.library.recordings()
        for r in recs:
            if r["id"] == recording_id:
                return r
        words = [w for w in re.findall(r"[\w-]+", recording_id.lower()) if len(w) > 1]
        scored = [(sum(w in f"{r['title']} {r['date']}".lower() for w in words), r) for r in recs]
        scored = [s for s in scored if s[0] == max((x[0] for x in scored), default=0) and s[0] > 0]
        return scored[0][1] if len(scored) == 1 else None

    def find_recordings(self, words: str = "") -> str:
        """List the recordings in the library: title, date, length, and what has been done with each. Optional
        words narrow the list by title or date, e.g. "November" or "budget hearing". Use it to find a
        recording_id for the other tools."""
        recs = self.library.recordings()
        ws = [w for w in re.findall(r"[\w-]+", words.lower()) if len(w) > 1]
        if ws:
            recs = [r for r in recs if all(w in f"{r['title']} {r['date']}".lower() for w in ws)]
        if not recs:
            return "No recordings match." if ws else "The library is empty. Use transcribe to add recordings."
        lines = [f"{len(recs)} recording(s):"]
        for r in recs[:MAX_LINES]:
            best = next(v for v in r["views"] if v["run_id"] == r["default_run"])
            done = ", ".join(h for h in best["has"] if h in ("transcript", "speakers", "codes"))
            dur = f"{round(r['duration_s'] / 60)} min" if r.get("duration_s") else ""
            lines.append(f"- {r['title']} ({r['date'] or 'no date'}, {dur}) id={r['id']}: {done or 'audio only'}")
        if len(recs) > MAX_LINES:
            lines.append(f"... and {len(recs) - MAX_LINES} more; give words to narrow the list.")
        return "\n".join(lines)

    def search_transcripts(self, words: str) -> str:
        """Find where people said something, across every transcript. Give two or three distinctive words, e.g.
        "data center" or "parking enforcement". Returns quotes with the recording, the time and the speaker."""
        hits = self.library.search(words, limit=200)
        if not hits:
            return f"No transcript line contains all of: {words}. Try fewer or different words."
        per: dict[str, list] = {}
        for h in hits:
            per.setdefault(h["id"], []).append(h)
        lines = [f"{len(hits)} line(s) in {len(per)} recording(s). The first ones:"]
        n = 0
        for rid, hs in per.items():
            lines.append(f"{hs[0]['title']} ({hs[0]['date']}), id={rid}:")
            for h in hs[:3]:
                lines.append(f'  [{hms(h["start"])}] {h["speaker"] or "speaker"}: "{h["text"]}" '
                             f"({link(h['run_id'], rid, h['start'])})")
                n += 1
            if n >= 15:
                break
        return "\n".join(lines)

    def recording_overview(self, recording_id: str) -> str:
        """Facts about one recording: date, length, how many speakers, and what was coded in it (for example
        public comments: who spoke, their position, a one-line summary, and when). recording_id can be the id or
        the title's date, e.g. "October 28"."""
        r = self._match(recording_id)
        if not r:
            return f"No single recording matches {recording_id!r}. Use find_recordings to get its id."
        v = self.library.recording(r["default_run"], r["id"])
        words = sum(len(s["text"].split()) for s in v["segments"])
        lines = [f"{v['title']} ({v['date'] or 'no date'}), {round((v['duration_s'] or 0) / 60)} min, id={r['id']}.",
                 f"Transcript: {len(v['segments'])} lines, {words} words, {len(v['speakers'])} speaker label(s)."]
        fails = [p for p in v["problems"] if p["status"] == "fail"]
        if fails:
            lines.append("Warning, a check failed in this run: " + "; ".join(p["message"] for p in fails[:2]))
        for c in v["codes"]:
            book = (c.get("codebook") or {}).get("name") or c["step"]
            lines.append(f"{len(c['annotations'])} stretch(es) coded with {book}:")
            for a in c["annotations"][:MAX_LINES]:
                lines.append(f"- {coded_line(a)} ({link(v['run_id'], r['id'], a.get('start') or 0)})")
        if not v["codes"]:
            lines.append("Nothing has been coded in this recording yet (no code step was run on it).")
        return "\n".join(lines)

    def read_transcript(self, recording_id: str, from_minute: int = 0, minutes: int = 10) -> str:
        """Show what was said in part of one recording (its transcript, with times and speakers): from_minute on,
        for up to 15 minutes. recording_id can be the id, or the meeting's date or title words, e.g. "January 29".
        For "the first five minutes" use from_minute=0, minutes=5."""
        r = self._match(recording_id)
        if not r:
            return f"No single recording matches {recording_id!r}. Use find_recordings to get its id."
        v = self.library.recording(r["default_run"], r["id"])
        a, b = from_minute * 60, (from_minute + min(max(minutes, 1), 15)) * 60
        segs = [s for s in v["segments"] if a <= s["start"] < b]
        if not segs:
            return f"Nothing is said between minute {from_minute} and {from_minute + minutes} of {v['title']}."
        out, prev = [f"{v['title']}, minutes {from_minute} to {from_minute + min(minutes, 15)}:"], None
        for s in segs:
            who = s["speaker"] or "speaker"
            out.append(f"[{hms(s['start'])}] {who + ': ' if who != prev else ''}{s['text']}")
            prev = who
        return "\n".join(out)

    def coded(self, code: str = "", words: str = "", recordings: str = "") -> str:
        """What was coded across many recordings, already counted: how many stretches of each code (for example
        public_comment), by recording, the counts of each field with fixed choices (for example position:
        support, oppose), and a few examples. code narrows to one code, e.g. "public_comment"; words narrows to
        stretches whose text or fields mention something, e.g. "data center"; recordings narrows by title or date
        words, e.g. "November" or "budget". Use it for questions about what people said overall."""
        from collections import Counter
        from .library import stem
        ws = [w for w in re.findall(r"[\w-]+", words.lower()) if len(w) > 2]
        ms = [w for w in re.findall(r"[\w-]+", recordings.lower()) if len(w) > 1]
        want = code.strip().lower().replace(" ", "_")
        found, per_rec = [], Counter()
        for r in self.library.recordings():
            if ms and not all(m in f"{r['title']} {r['date']}".lower() for m in ms):
                continue
            v = self.library.recording(r["default_run"], r["id"])
            for c in v["codes"]:
                for a in c["annotations"]:
                    if want and a["code"] != want:
                        continue
                    text = " ".join([a.get("text", ""), *map(str, a["attributes"].values())]).lower()
                    if all(stem(w) in text for w in ws):
                        found.append((r, v, a))
                        per_rec[r["title"]] += 1
        if not found:
            scope = (f" as {code!r}" if want else "") + (f" about {words!r}" if ws else "") + (
                f" in recordings matching {recordings!r}" if ms else "")
            return f"Nothing coded{scope}. Codes exist only for recordings run with a code step."
        codes = Counter(a["code"] for _, _, a in found)
        lines = [f"{len(found)} coded stretch(es) in {len(per_rec)} recording(s).",
                 "Codes: " + ", ".join(f"{k} {n}" for k, n in codes.most_common())]
        fields: dict[str, Counter] = {}
        for _, _, a in found:
            for k, val in a["attributes"].items():
                if val and len(str(val)) <= 30:
                    fields.setdefault(k, Counter())[str(val)] += 1
        for k, cnt in fields.items():
            if len(cnt) <= 8:   # a field with few values is a choice worth counting; free text is not
                lines.append(f"{k}: " + ", ".join(f"{x} {n}" for x, n in cnt.most_common()))
        lines.append("By recording: " + "; ".join(f"{k} ({n})" for k, n in sorted(per_rec.items())))
        lines.append("Examples:")
        for r, v, a in found[:10]:
            lines.append(f"- {v['date'] or v['title']} {coded_line(a)} ({link(v['run_id'], r['id'], a.get('start') or 0)})")
        if len(found) > 10:
            lines.append(f"... and {len(found) - 10} more; narrow with code, words or recordings to see others.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ jobs
    def transcribe(self, links: list[str], with_comments: bool = False) -> str:
        """Start transcribing video or audio links (YouTube or other sites): download, find who speaks when,
        transcribe, and build speaker turns. with_comments=true also lists the public comments with the large
        language model, which takes longer. It runs in the background; this returns its job id."""
        from . import presets
        links = [str(l).strip() for l in (links if isinstance(links, list) else [links]) if str(l).strip()]
        if not links:
            return "Give at least one link."
        missing = presets.missing_models(self.cfg, with_comments)
        if missing:
            return (f"Cannot start yet: these models are not downloaded: {', '.join(missing)}. "
                    "The Setup tab downloads them.")
        from .usage import estimate_text
        text = presets.transcript_recipe(links, with_comments)
        try:
            est = estimate_text(self.cfg, text, self.cfg.workspace / "recipes")["text"]
        except Exception:  # an estimate is a courtesy; the job goes ahead without one
            est = ""
        run_id = self.queue.submit(text, self.cfg.workspace / "recipes")
        waiting = sum(j["status"] in ("queued", "running") for j in self.queue.list() if j["run_id"] != run_id)
        return (f"Started job {run_id} for {len(links)} link(s)"
                + (f"; {waiting} job(s) ahead of it" if waiting else "") + ". Its progress is on the Jobs tab."
                + (f" It should take {est}." if est else ""))

    # ------------------------------------------------------------------ reviews (checking by hand)
    def review_progress(self) -> str:
        """How far each hand-checking set (a review) has got: items answered, how many are left and the time that
        takes at the person's pace, and how often the machine agreed with the person so far."""
        from . import reviews
        sets = reviews.list_sets(self.cfg.workspace)
        if not sets:
            return "No reviews yet. make_review starts one."
        lines = []
        for p in sets[:MAX_LINES]:
            left = (f"; {p['left']} left, about {round(p['left_s'] / 60)} min at your pace" if p["left_s"] and p["left"]
                    else f"; {p['left']} left" if p["left"] else "; finished")
            agree = (f"; {p['machine_label']} agreed with you on {p['agreement']:.0%} so far"
                     if p["agreement"] is not None else "")
            who = (" (" + ", ".join(f"{b} {n}" for b, n in sorted(p["annotators"].items())) + ")"
                   if len(p["annotators"]) > 1 else "")
            lines.append(f"- {p['name']}: {p['answered']} of {p['items']} answered{who}{left}{agree} (#review/{p['id']})")
        return "\n".join(lines)

    def make_review(self, kind: str, recording_id: str = "", how_many: int = 100) -> str:
        """Make a set of items for the person to check by hand in the Review tab. kind is "codes" (check what the
        model coded, e.g. is each sampled speaker turn a public comment?), "speakers" (who is each speaker label?)
        or "transcript" (correct sampled lines). recording_id can be the id or a date like "January 29"; leave it
        empty to use every recording in the newest run that has what the kind needs."""
        from . import reviews
        need = {"codes": "codes", "speakers": "speakers", "transcript": "transcript"}.get(kind)
        if not need:
            return 'kind must be "codes", "speakers" or "transcript".'
        recs = self.library.recordings()
        if recording_id:
            r = self._match(recording_id)
            if not r:
                return f"No single recording matches {recording_id!r}. Use find_recordings to get its id."
            recs = [r]
        views = sorted((v for r in recs for v in r["views"] if need in v.get("has", [])),
                       key=lambda v: v["created_utc"], reverse=True)
        if not views:
            return (f"No run has what a {kind} review needs ({need}). "
                    f"Transcribe{' with comments' if kind == 'codes' else ''} first.")
        run = views[0]["run_id"]
        try:
            spec = reviews.from_run(self.cfg.workspace, run, kind, how_many,
                                    items=[recs[0]["id"]] if recording_id else None)
            sid = reviews.create(self.cfg.workspace, spec)
        except ValueError as e:
            return str(e)
        return (f"Made the review \"{spec['name']}\" with {len(spec['items'])} items. It is in the Review tab "
                f"(#review/{sid}).")

    def jobs_status(self) -> str:
        """What is running, waiting, or finished recently, with how far along each one is."""
        jobs = self.queue.list(limit=8)
        if not jobs:
            return "No jobs yet."
        lines = []
        for j in jobs:
            rec = self.store.run_dir(j["run_id"]) / "run.json"
            prog = ""
            if rec.is_file():
                record = json.loads(rec.read_text(encoding="utf-8"))
                cells = [c["status"] for row in record["cells"].values() for c in row.values()]
                prog = f", {sum(c in ('ran', 'cached') for c in cells)} of {len(cells)} steps done"
                if any(c == "failed" for c in cells):
                    prog += f", {sum(c == 'failed' for c in cells)} failed"
                if j["status"] == "running" and record.get("stages"):
                    from .usage import Rates, estimate_run, left_text
                    prog += ", " + left_text(estimate_run(Rates(self.cfg.workspace), record,
                                                          self.cfg.tool("ffprobe"))["seconds"])
            lines.append(f"- {j['name']} (job {j['run_id']}): {j['status']}{prog}")
        return "\n".join(lines)

    def cancel_job(self, job_id: str) -> str:
        """Stop a running or waiting job. Finished work is kept. The person is asked to confirm first."""
        job = self.queue.cancel(job_id)
        return f"Job {job_id} is {job['status']}." if job else f"No job {job_id}."

    def devices(self) -> str:
        """Camera cards, memory cards and USB drives that are plugged in, which can be imported."""
        from .ingest import volumes
        vols = volumes()
        if not vols:
            return "No camera card or drive is plugged in."
        return "\n".join(f"- {v['label']} at {v['mount']}: {v['used'] / 1e9:.1f} GB used"
                         + (" (a camera card)" if v["camera"] else "") for v in vols)

    def import_device(self, device: str, project: str = "Project") -> str:
        """Copy everything from a plugged-in card or drive into the archive, checking every copy, and name the
        files by date and camera. The card is not changed. The person is asked to confirm first."""
        from .ingest import DEFAULT_TEMPLATE, volumes
        vols = volumes()
        v = next((x for x in vols if device in (x["mount"], x["label"])), None)
        if not v:
            return f"No device called {device!r} is plugged in. Plugged in: {', '.join(x['label'] for x in vols) or 'none'}."
        spec = {"source": v["mount"], "archive": str(self.cfg.archive), "project": project, "template": DEFAULT_TEMPLATE}
        run_id = self.queue.submit_task("ingest", spec, f"import {v['label']}")
        return f"Started importing {v['label']} into {self.cfg.archive} (job {run_id}). Progress is on the Import tab."


def openai_tools(tb: Toolbox) -> list[dict]:
    return [t.schema() for t in tb.tools()]
