"""How much work this machine has done, what the language-model part would have cost through an API, how fast it
ran, and how long new work will take.

Everything comes from receipts and the assistant's log. Each receipt in the store is one stage done for one item (a
reused result is not done again, so nothing is counted twice), with its time, its device and, for the stages that
use a language model, its tokens and the server's speeds. Nothing here goes online: the API prices are written below
with the day they were read from the providers' pricing pages, and change only when someone edits them.

    ontherecord usage          compute hours, tokens, the API-price equivalent, speeds by software version
"""
from __future__ import annotations

import functools
import json
import socket
import statistics
import subprocess
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PRICES_READ = "2026-10-23"
# USD per million tokens, standard rates for prompts under the long-context tier (every call here is), without
# prompt caching or batch discounts. Tiers as Jeff pairs them: Terra ~ Sonnet, Sol ~ Opus, Astra ~ Fable.
ANTHROPIC = "https://platform.claude.com/docs/en/about-claude/pricing"
OPENAI = "https://developers.openai.com/api/docs/pricing"
PRICES = {
    "claude-sonnet-5-5": {"name": "Claude Sonnet 5.5", "tier": "Sonnet/Terra", "input": 2.00, "output": 10.00,
                          "source": ANTHROPIC},
    "gpt-5.6-terra": {"name": "GPT-5.6 Terra", "tier": "Sonnet/Terra", "input": 2.00, "output": 12.00,
                      "source": OPENAI},
    "claude-opus-5-5": {"name": "Claude Opus 5.5", "tier": "Opus/Sol", "input": 4.00, "output": 20.00,
                        "source": ANTHROPIC},
    "gpt-5.6-sol": {"name": "GPT-5.6 Sol", "tier": "Opus/Sol", "input": 4.00, "output": 20.00, "source": OPENAI,
                    "note": "promotional pricing, at least through Nov 21, 2026"},
    "claude-fable-5-1": {"name": "Claude Fable 5.1", "tier": "Fable/Astra", "input": 10.00, "output": 50.00,
                         "source": ANTHROPIC},
    "gpt-6-astra": {"name": "GPT-6 Astra", "tier": "Fable/Astra", "input": 10.00, "output": 50.00, "source": OPENAI},
}
HEADLINE = ("claude-sonnet-5-5", "gpt-5.6-terra")


def receipts(workspace: Path) -> list[dict]:
    out = []
    for p in (workspace / "store").glob("*/*/*/receipt.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def assistant_log(workspace: Path) -> Path:
    return workspace / "usage" / "assistant.jsonl"


def record_assistant(workspace: Path, model: str | None, response: dict, seconds: float) -> None:
    """One line per call the assistant makes to its model: tokens (usage) and llama-server's own timings."""
    usage, t = response.get("usage") or {}, response.get("timings") or {}
    row = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "model": model,
           "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
           "prompt_n": t.get("prompt_n"), "prompt_ms": t.get("prompt_ms"), "gen_n": t.get("predicted_n"),
           "gen_ms": t.get("predicted_ms"), "seconds": round(seconds, 2)}
    p = assistant_log(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def llm_metrics(calls: list[dict]) -> dict:
    """What a stage's model calls add to its metrics: tokens in and out (the server's count), the number of calls,
    and llama-server's own prompt and generation speeds, which make runs comparable across software updates."""
    out = {"input_tokens": sum(c.get("input_tokens") or 0 for c in calls),
           "output_tokens": sum(c.get("output_tokens") or 0 for c in calls), "llm_calls": len(calls)}
    for kind in ("prompt", "gen"):
        n, ms = sum(c.get(f"{kind}_n") or 0 for c in calls), sum(c.get(f"{kind}_ms") or 0 for c in calls)
        if n and ms:
            out[f"{kind}_tokens_per_s"] = round(n / ms * 1000, 1)
    return out


def _price(tokens_in: int, tokens_out: int) -> dict:
    return {k: round((tokens_in * p["input"] + tokens_out * p["output"]) / 1e6, 2) for k, p in PRICES.items()}


def _median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 2) if xs else None


def ledger(workspace: Path, jobs: list[dict] | None = None) -> dict:
    """Everything `ontherecord usage` shows. `jobs` (the queue's list) adds the time the machine was busy."""
    recs = receipts(workspace)
    by_stage: dict[str, dict] = defaultdict(lambda: {"receipts": 0, "seconds": 0.0, "input_tokens": 0,
                                                     "output_tokens": 0, "uncounted": 0})
    by_device: dict[str, float] = defaultdict(float)
    by_month: dict[str, float] = defaultdict(float)
    setups: dict[tuple, float] = {}
    llm_speed: dict[tuple, list] = defaultdict(list)
    audio_speed: dict[tuple, list] = defaultdict(list)
    for r in recs:
        st, t, m = r["stage"], r.get("timing") or {}, r.get("metrics") or {}
        s = by_stage[st]
        s["receipts"] += 1
        s["seconds"] += t.get("elapsed_s") or 0
        by_device[r.get("device") or "cpu"] += t.get("elapsed_s") or 0
        by_month[(t.get("started_utc") or "?")[:7]] += t.get("elapsed_s") or 0
        if t.get("batch_setup_s"):  # every receipt of a batch repeats its one setup (loading the model)
            setups[(r.get("run_id"), st, t["batch_setup_s"])] = t["batch_setup_s"]
        if r.get("device") == "server" or "input_tokens" in m:
            if "input_tokens" in m:
                s["input_tokens"] += m.get("input_tokens") or 0
                s["output_tokens"] += m.get("output_tokens") or 0
            else:
                s["uncounted"] += 1
        gpu = ((r.get("environment") or {}).get("gpu") or {}).get("name", "cpu")
        tools = r.get("tools") or {}
        if m.get("gen_tokens_per_s"):
            model = (tools.get("model") or {}).get("file") if isinstance(tools.get("model"), dict) else tools.get("model")
            llm_speed[(st, model, tools.get("llama_server", "?").split("(")[-1].rstrip(")"), gpu)].append(m)
        if m.get("duration_s") and t.get("elapsed_s"):
            pk = (r.get("environment") or {}).get("packages") or {}
            versions = ", ".join(f"{k} {v}" for k, v in sorted(pk.items()) if v and k in (
                "faster-whisper", "ctranslate2", "pyannote.audio", "torch"))
            label = f"{(r.get('settings') or {}).get('model', '')} {m.get('compute_type_used', '')}".strip()
            audio_speed[(st, label, versions, gpu)].append(m["duration_s"] / t["elapsed_s"])
    asst = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0}
    p = assistant_log(workspace)
    for line in (p.read_text(encoding="utf-8").splitlines() if p.is_file() else []):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        asst["calls"] += 1
        asst["input_tokens"] += row.get("input_tokens") or 0
        asst["output_tokens"] += row.get("output_tokens") or 0
        asst["seconds"] += row.get("seconds") or 0
    tin = sum(s["input_tokens"] for s in by_stage.values()) + asst["input_tokens"]
    tout = sum(s["output_tokens"] for s in by_stage.values()) + asst["output_tokens"]
    work = sum(s["seconds"] for s in by_stage.values()) + sum(setups.values())
    busy = {}
    for j in jobs or []:
        if j.get("started_utc") and j.get("finished_utc"):
            sec = (datetime.fromisoformat(j["finished_utc"]) - datetime.fromisoformat(j["started_utc"])).total_seconds()
            kind = "recipes" if j.get("backend") == "local" else "imports, downloads and commands"
            busy[kind] = busy.get(kind, 0) + sec
    times = sorted(r["timing"]["started_utc"] for r in recs if (r.get("timing") or {}).get("started_utc"))
    return {
        "since": times[0][:10] if times else None, "receipts": len(recs),
        "compute": {"hours": round(work / 3600, 2), "stage_hours": round((work - sum(setups.values())) / 3600, 2),
                    "setup_hours": round(sum(setups.values()) / 3600, 2),
                    "by_device_hours": {k: round(v / 3600, 2) for k, v in sorted(by_device.items())},
                    "by_month_hours": {k: round(v / 3600, 2) for k, v in sorted(by_month.items())},
                    "assistant_hours": round(asst["seconds"] / 3600, 2),
                    "busy_hours": {k: round(v / 3600, 2) for k, v in busy.items()}},
        "stages": {k: {**v, "hours": round(v["seconds"] / 3600, 2)} for k, v in sorted(by_stage.items())},
        "assistant": asst,
        "tokens": {"input": tin, "output": tout},
        "api_equivalent": {"prices_read": PRICES_READ, "usd": _price(tin, tout), "prices": PRICES,
                           "headline": list(HEADLINE),
                           "uncounted_receipts": sum(s["uncounted"] for s in by_stage.values())},
        "speeds": {
            "llm": [{"stage": k[0], "model": k[1], "llama_build": k[2], "gpu": k[3], "runs": len(v),
                     "prompt_tokens_per_s": _median([m["prompt_tokens_per_s"] for m in v if m.get("prompt_tokens_per_s")]),
                     "gen_tokens_per_s": _median([m["gen_tokens_per_s"] for m in v])} for k, v in sorted(llm_speed.items(), key=str)],
            "audio": [{"stage": k[0], "setting": k[1], "software": k[2], "gpu": k[3], "runs": len(v),
                       "times_real_time": _median(v)} for k, v in sorted(audio_speed.items(), key=str)]},
    }


# ---------------------------------------------------------------------------------------------- estimates
def fits(seconds: float) -> str:
    """Jeff's yardsticks for whether a job fits the time someone has."""
    if seconds <= 15 * 60:
        return "a coffee break"
    if seconds <= 3600:
        return "a lunch break"
    if seconds <= 3 * 3600:
        return "a long meeting"
    if seconds <= 12 * 3600:
        return "overnight"
    return "more than a night"


def duration_text(s: float) -> str:
    m = round(s / 60)
    return f"{m} min" if m < 90 else f"{s / 3600:.1f} h"


def _model(settings: dict) -> str | None:
    v = (settings or {}).get("model")
    return v if isinstance(v, str) else None


class Rates:
    """This machine's pace per stage, from its own receipts: seconds per minute of audio where the stage's work grows
    with the recording, else seconds per item, plus the one-time setup (loading a model) per batch. A stage's pace is
    looked up for its model setting first (large-v3 and large-v3-turbo differ several times), then for the stage."""

    def __init__(self, workspace: Path, host: str | None = None):
        recs = receipts(workspace)
        host = host or socket.gethostname()
        mine = [r for r in recs if (r.get("environment") or {}).get("hostname") == host] or recs
        self.durations = {}
        for r in recs:
            d = (r.get("metrics") or {}).get("duration_s")
            if d:
                self.durations[r["item"]["id"]] = d
        per_min, per_item, setup = defaultdict(list), defaultdict(list), defaultdict(list)
        for r in mine:
            t = r.get("timing") or {}
            if t.get("elapsed_s") is None:
                continue
            keys = [(r["stage"], _model(r.get("settings"))), (r["stage"], None)]
            d = self.durations.get(r["item"]["id"])
            for k in dict.fromkeys(keys):
                if d:
                    per_min[k].append(t["elapsed_s"] / (d / 60))
                else:
                    per_item[k].append(t["elapsed_s"])
                if t.get("batch_setup_s") is not None:
                    setup[k].append(t["batch_setup_s"])
        self.per_min = {k: statistics.median(v) for k, v in per_min.items()}
        self.per_item = {k: statistics.median(v) for k, v in per_item.items()}
        self.setup = {k: statistics.median(v) for k, v in setup.items()}

    def _get(self, table: dict, stage: str, model: str | None):
        return table.get((stage, model), table.get((stage, None)))

    def step(self, stage: str, settings: dict, minutes: float | None) -> float | None:
        """Seconds for one stage on one item (without setup), or None when this machine has never done it."""
        m = _model(settings)
        rate = self._get(self.per_min, stage, m)
        if rate is not None and minutes:
            return rate * minutes
        item = self._get(self.per_item, stage, m)
        return item if item is not None else (rate * minutes if rate and minutes else None)


@functools.lru_cache(maxsize=4096)
def probe_minutes(ffprobe: str, path: str) -> float | None:
    """A local file's length, from its header (instant)."""
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip()) / 60
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def estimate(rates: Rates, steps: list[dict], items: list[dict], todo: dict[str, set[str]],
             ffprobe: str = "ffprobe") -> dict:
    """How long the steps still to do will take on this machine. steps: [{name, stage, settings}]; items: [{id,
    path?}]; todo: item id -> names of the steps it still needs. Items whose length is unknown (a link not yet
    downloaded) are counted at an hour each and named, and the pace per hour of audio is given."""
    total, unknown_len, never = 0.0, [], set()
    batches = set()
    per_hour = 0.0
    for st in steps:
        r = rates._get(rates.per_min, st["stage"], _model(st.get("settings")))
        per_hour += (r or 0) * 60
    for it in items:
        names = todo.get(it["id"]) or set()
        if not names:
            continue
        minutes = rates.durations.get(it["id"])
        minutes = minutes / 60 if minutes else (probe_minutes(ffprobe, it["path"]) if it.get("path") else None)
        if minutes is None:
            unknown_len.append(it["id"])
        for st in steps:
            if st["name"] not in names:
                continue
            s = rates.step(st["stage"], st.get("settings") or {}, minutes or 60)
            if s is None:
                never.add(st["stage"])
                continue
            total += s
            batches.add((st["stage"], _model(st.get("settings"))))
    total += sum(rates._get(rates.setup, *b) or 0 for b in batches)
    text = f"about {duration_text(total)} on this computer, so it fits {fits(total)}" if total else ""
    if total and fits(total) == "more than a night":
        text = f"about {duration_text(total)} on this computer: more than a night"
    if unknown_len:
        many = len(unknown_len) > 1
        text += (f" (counting {len(unknown_len) if many else 'a'} recording{'s' if many else ''} of unknown length as "
                 f"an hour{' each' if many else ''}; this computer takes about {duration_text(per_hour)} per hour of "
                 "audio)")
    if never:
        text += f"; never done here before, so not counted: {', '.join(sorted(never))}"
    return {"seconds": round(total), "fits": fits(total) if total else None, "unknown_length": unknown_len,
            "never_done": sorted(never), "per_hour_of_audio_s": round(per_hour), "text": text.strip("; ")}


def estimate_plan(rates: Rates, recipe, cells: dict, ffprobe: str = "ffprobe") -> dict:
    """For a plan (Runner.plan): the steps that will or may run."""
    steps = [{"name": s.name, "stage": s.stage, "settings": s.run_settings} for s in recipe.stages]
    todo = {i: {n for n, c in row.items() if c["status"] in ("run", "run-if-changed")} for i, row in cells.items()}
    return estimate(rates, steps, [i.as_dict() for i in recipe.items], todo, ffprobe)


def estimate_run(rates: Rates, rec: dict, ffprobe: str = "ffprobe") -> dict:
    """For a run record still going: the cells not yet done."""
    steps = [{"name": s["name"], "stage": s.get("stage", s["name"]), "settings": s.get("settings") or {}}
             for s in rec["stages"]]
    todo = {i: {n for n, c in row.items() if c.get("status") in ("pending", "queued", "running", "run",
                                                                 "run-if-changed")}
            for i, row in rec["cells"].items()}
    return estimate(rates, steps, rec["items"], todo, ffprobe)


def estimate_text(cfg, text: str, base: Path, rates: Rates | None = None) -> dict:
    """The estimate for recipe text, as `plan` would see it now."""
    from .recipe import recipe_from_text
    from .runner import Runner
    r = recipe_from_text(text, base)
    return estimate_plan(rates or Rates(cfg.workspace), r, Runner(r, cfg).plan(), cfg.tool("ffprobe"))


def left_text(seconds: float) -> str:
    return f"about {duration_text(seconds)} left" if seconds >= 60 else "under a minute left"
