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
                Tool("public_comments", self.public_comments),
                Tool("transcribe", self.transcribe, confirm=True), Tool("jobs_status", self.jobs_status),
                Tool("cancel_job", self.cancel_job, confirm=True), Tool("devices", self.devices),
                Tool("import_device", self.import_device, confirm=True)]

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
            done = ", ".join({"align": "speakers", "transcribe": "transcript", "extract_comments": "public comments"}[h]
                             for h in best["has"] if h in ("align", "transcribe", "extract_comments"))
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
        """Facts about one recording: date, length, how many speakers, and the public comments extracted from it
        (who spoke, their position, a one-line summary, and when). recording_id can be the id or the title's
        date, e.g. "October 28"."""
        r = self._match(recording_id)
        if not r:
            return f"No single recording matches {recording_id!r}. Use find_recordings to get its id."
        v = self.library.recording(r["default_run"], r["id"])
        words = sum(len(s["text"].split()) for s in v["segments"])
        lines = [f"{v['title']} ({v['date']}), {round((v['duration_s'] or 0) / 60)} min, id={r['id']}.",
                 f"Transcript: {len(v['segments'])} lines, {words} words, {len(v['speakers'])} speaker label(s)."]
        fails = [p for p in v["problems"] if p["status"] == "fail"]
        if fails:
            lines.append("Warning, a check failed in this run: " + "; ".join(p["message"] for p in fails[:2]))
        if v["comments"]:
            lines.append(f"{len(v['comments'])} public comment(s):")
            for c in v["comments"][:MAX_LINES]:
                lines.append(f"- [{c['timestamp']}] {c['speaker_name'] or 'Unidentified'} ({c['position']}, "
                             f"{c['agenda_item']}): {c['summary']} ({link(v['run_id'], r['id'], c['start_s'] or 0)})")
        else:
            lines.append("No public comments have been extracted from this recording yet.")
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

    def public_comments(self, words: str = "", meetings: str = "") -> str:
        """Public comments across many meetings, already counted: how many, for and against, by agenda item and by
        meeting, with a few examples. words narrows to comments about something (e.g. "data center"); meetings
        narrows by title or date words (e.g. "November" or "budget"). Use it for questions about what the public
        said overall."""
        ws = [w for w in re.findall(r"[\w-]+", words.lower()) if len(w) > 2]
        ms = [w for w in re.findall(r"[\w-]+", meetings.lower()) if len(w) > 1]
        from .library import stem
        found, per_meeting = [], {}
        for r in self.library.recordings():
            if ms and not all(m in f"{r['title']} {r['date']}".lower() for m in ms):
                continue
            v = self.library.recording(r["default_run"], r["id"])
            for c in v["comments"]:
                text = " ".join(str(c.get(k) or "") for k in ("summary", "agenda_item", "speaker_name")).lower()
                if all(stem(w) in text for w in ws):
                    found.append((r, v, c))
                    per_meeting[r["title"]] = per_meeting.get(r["title"], 0) + 1
        if not found:
            scope = (f" about {words!r}" if ws else "") + (f" in meetings matching {meetings!r}" if ms else "")
            return f"No extracted public comments{scope}. Comments exist only for recordings run with comment extraction."
        from collections import Counter
        pos = Counter((c.get("position") or "unclear") for _, _, c in found)
        items = Counter((c.get("agenda_item") or "unknown") for _, _, c in found)
        lines = [f"{len(found)} public comment(s) in {len(per_meeting)} meeting(s).",
                 "Positions: " + ", ".join(f"{k} {n}" for k, n in pos.most_common()),
                 "Most common agenda items: " + "; ".join(f"{k} ({n})" for k, n in items.most_common(5)),
                 "By meeting: " + "; ".join(f"{k} ({n})" for k, n in sorted(per_meeting.items())), "Examples:"]
        for r, v, c in found[:10]:
            lines.append(f"- {v['date']} [{c['timestamp']}] {c['speaker_name'] or 'Unidentified'} ({c['position']}): "
                         f"{c['summary']} ({link(v['run_id'], r['id'], c['start_s'] or 0)})")
        if len(found) > 10:
            lines.append(f"... and {len(found) - 10} more; narrow with words or meetings to see others.")
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
