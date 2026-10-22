"""Have a long-context model list the public comments in a whole meeting. Ported from
hands_on_dl/extract_public_comments.py; the default prompt is that script's SYSTEM string, verbatim."""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

from ..llm import ManagedLlamaServer, Server, est_tokens, parse_json, quick_fingerprint
from .base import Check, Context, Item, Result, Setting, Stage

POSITIONS = ["support", "oppose", "mixed", "neutral_or_question", "unclear"]

# Verbatim from hands_on_dl (spellings included), because the schema is part of what the model reads.
SCHEMA = {
    "type": "object",
    "properties": {
        "public_comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "block_ids": {"type": "array", "items": {"type": "integer"},
                                  "description": "every block of this comment, in order"},
                    "speaker_name": {"type": "string", "description": "as stated, else empty"},
                    "residence_or_affiliation": {"type": "string",
                                                 "description": "block, ward, neighbourhood or organisation, as stated, else empty"},
                    "agenda_item": {"type": "string",
                                    "description": "the item or topic addressed, e.g. 'general public comment' or the ordinance"},
                    "position": {"type": "string", "enum": POSITIONS},
                    "summary": {"type": "string", "description": "one or two sentences in plain words"},
                    "boundary_note": {"type": "string",
                                      "description": "if a block also holds someone else's speech, say so; else empty"},
                },
                "required": ["block_ids", "speaker_name", "residence_or_affiliation", "agenda_item", "position",
                             "summary", "boundary_note"],
            },
        }
    },
    "required": ["public_comments"],
}

CSV_FIELDS = ["comment_id", "meeting_date", "meeting_title", "video_id", "comment_number", "timestamp", "start_s",
              "end_s", "block_ids", "speakers", "speaker_name", "residence_or_affiliation", "agenda_item", "position",
              "summary", "boundary_note", "word_count", "text"]


def hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def render_block(b: dict) -> str:
    return f'[{b["block_id"]}] {b["speaker"]} {hms(b["start"])}-{hms(b["end"])}: {b.get("text", "").strip()}'


def meeting_date(meeting: dict) -> str:
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", meeting["title"])
    if m:
        try:
            return datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
        except ValueError:
            pass
    d = meeting.get("upload_date", "")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else ""


def windows(blocks: list[dict], budget: int, overlap: int) -> list[list[dict]]:
    """The whole meeting if it fits `budget` tokens, else overlapping runs of blocks that each fit."""
    sizes = [est_tokens(render_block(b)) + 1 for b in blocks]
    if sum(sizes) <= budget:
        return [blocks]
    out, i = [], 0
    while i < len(blocks):
        j, used = i, 0
        while j < len(blocks) and used + sizes[j] <= budget:
            used += sizes[j]
            j += 1
        j = max(j, i + 1)
        out.append(blocks[i:j])
        if j >= len(blocks):
            break
        i = max(i + 1, j - overlap)
    return out


def merge(comments: list[dict]) -> list[dict]:
    """Comments from overlapping windows that share a block become one (the first one's metadata wins)."""
    merged: list[dict] = []
    for c in comments:
        ids = set(c["block_ids"])
        hit = [m for m in merged if ids & set(m["block_ids"])]
        if not hit:
            merged.append(dict(c))
            continue
        keep = hit[0]
        for m in hit[1:]:
            keep["block_ids"] = sorted(set(keep["block_ids"]) | set(m["block_ids"]))
            merged.remove(m)
        keep["block_ids"] = sorted(set(keep["block_ids"]) | ids)
        for k, v in c.items():
            if not keep.get(k) and v:
                keep[k] = v
    return sorted(merged, key=lambda m: m["block_ids"][0])


class ExtractComments(Stage):
    """Give the model one whole meeting as numbered speech blocks and have it list every public comment, by block
    id, with the speaker's name, affiliation, agenda item, position and a summary. Answers by block id, so every
    comment's text is the transcript's own and can be checked against the meeting."""
    name = "extract_comments"
    label = "Extract public comments (LLM)"
    version = "1"
    gpu = True
    needs = ("blocks",)
    makes = ("comments", "comments_csv")
    settings = {
        "endpoint": Setting("managed", "'managed' starts llama-server with `model` and stops it afterwards; or "
                            "the URL of an OpenAI-compatible server already running, e.g. http://127.0.0.1:8081/v1."),
        "model": Setting("gemma-4-26B-A4B-it-MXFP4_MOE.gguf", "GGUF file for a managed server (relative to the "
                         "machine config's models_root)."),
        "context": Setting(65536, "Context window in tokens. 65536 fits the longest council meeting (~57k)."),
        "offload": Setting("-ngl 999 --n-cpu-moe 8; -ngl 999 --n-cpu-moe 14; -ngl 999 --n-cpu-moe 22",
                           "llama-server offload arguments, tried in order until the model loads (';'-separated). "
                           "A GPU with enough memory takes plain '-ngl 999'.", in_key=False),
        "system_prompt": Setting("extract_public_comments_v1.txt", "The instructions the model works from.",
                                 kind="file"),
        "thinking": Setting(False, "Let the model reason before answering."),
        "temperature": Setting(0.0, "0 = always the most likely token."),
        "seed": Setting(42, "Sampling seed."),
        "max_tokens": Setting(12000, "Answer length cap per call."),
        "overlap": Setting(8, "Blocks shared by neighboring windows when a meeting does not fit the context."),
    }

    def setup(self, settings: dict, ctx: Context):
        state = {"server": None}
        if settings["endpoint"] == "managed":
            from ..envinfo import tool_version
            binary = ctx.config.tool("llama_server")
            model = ctx.config.model_path(settings["model"])
            if not model.is_file():
                raise FileNotFoundError(f"model not found: {model} (set models_root in the machine config)")
            offloads = [s.strip() for s in settings["offload"].split(";") if s.strip()]
            srv = ManagedLlamaServer(binary, model, settings["context"], offloads,
                                     ctx.scratch / "llama-server.log", log=ctx.log)
            state.update(server=srv, url=srv.url,
                         tools={"llama_server": tool_version([binary, "--version"]), "offload_used": srv.offload,
                                "model": quick_fingerprint(model)})
        else:
            state.update(url=settings["endpoint"], tools={})
        state["llm"] = Server(state["url"], thinking=settings["thinking"], seed=settings["seed"])
        state["tools"]["served_model"] = state["llm"].served_model
        state["tools"]["n_ctx"] = state["llm"].n_ctx
        return state

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        llm, system = state["llm"], settings["system_prompt"]
        meeting = json.loads(inputs["blocks"].read_text(encoding="utf-8"))
        blocks = meeting["blocks"]
        by_id = {b["block_id"]: b for b in blocks}
        budget = llm.n_ctx - settings["max_tokens"] - est_tokens(system) - 500
        wins = windows(blocks, budget, settings["overlap"])
        raw, found, dropped = [], [], []
        for k, win in enumerate(wins):
            part = (f" (part {k + 1} of {len(wins)}, blocks {win[0]['block_id']}-{win[-1]['block_id']})"
                    if len(wins) > 1 else "")
            user = (f"Meeting: {meeting['title']}{part}\n\nSpeech blocks:\n" + "\n".join(map(render_block, win))
                    + "\n\nList every public comment in the blocks above.")
            r = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                         temperature=settings["temperature"], max_tokens=settings["max_tokens"], schema=SCHEMA)
            data = parse_json(r["text"])
            raw.append({"window": [win[0]["block_id"], win[-1]["block_id"]], "parsed": data is not None, **r})
            for c in (data or {}).get("public_comments", []):
                ids = [i for i in c.get("block_ids", []) if i in by_id]
                dropped += [i for i in c.get("block_ids", []) if i not in by_id]
                if ids:
                    found.append({**c, "block_ids": sorted(set(ids))})
        date, vid = meeting_date(meeting), meeting.get("video_id", item.id)
        rows = []
        for n, c in enumerate(merge(found), 1):
            bs = [by_id[i] for i in c["block_ids"]]
            text = " ".join(b.get("text", "").strip() for b in bs)
            rows.append({"comment_id": f"{date}_{vid}_{n:03d}", "meeting_date": date, "meeting_title": meeting["title"],
                         "video_id": vid, "comment_number": n, "timestamp": hms(bs[0]["start"]),
                         "start_s": bs[0]["start"], "end_s": bs[-1]["end"], "block_ids": c["block_ids"],
                         "speakers": sorted({b["speaker"] for b in bs}),
                         **{k: c.get(k, "") for k in ("speaker_name", "residence_or_affiliation", "agenda_item",
                                                      "position", "summary", "boundary_note")},
                         "word_count": len(text.split()), "text": text})
        failed = sum(not w["parsed"] for w in raw)
        (out / "comments.json").write_text(json.dumps({"comments": rows, "windows": len(wins), "failed_windows": failed,
                                                       "dropped_block_ids": dropped, "raw": raw},
                                                      indent=1, ensure_ascii=False), encoding="utf-8")
        with open(out / "comments.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({**r, "block_ids": " ".join(map(str, r["block_ids"])), "speakers": " ".join(r["speakers"])})
        tokens_in = sum(w.get("input_tokens") or 0 for w in raw)
        tokens_out = sum(w.get("output_tokens") or 0 for w in raw)
        checks = [Check("parsed", "fail" if failed else "ok", f"{len(wins) - failed}/{len(wins)}",
                        f"{failed} of {len(wins)} answers did not parse" if failed else "every answer parsed"),
                  Check("comments", "ok", len(rows), f"{len(rows)} public comments")]
        if dropped:
            checks.append(Check("dropped_ids", "warn", len(dropped), f"model named {len(dropped)} block ids that do "
                                                                     "not exist"))
        return Result(files={"comments": "comments.json", "comments_csv": "comments.csv"}, checks=checks,
                      metrics={"comments": len(rows), "windows": len(wins), "input_tokens": tokens_in,
                               "output_tokens": tokens_out},
                      device="server", tools=state["tools"])

    def teardown(self, state) -> None:
        srv = state.get("server")
        if srv:
            srv.stop()
            srv.log_file.close()
