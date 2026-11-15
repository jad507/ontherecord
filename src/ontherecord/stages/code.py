"""Code any segments with any codebook, by a language model: the machine coder (audit B2).

The model sees the units as numbered lines (`[t0012] SPEAKER_02 0:14:05-0:15:10: text`) and answers by unit id
only, never by quoting, so the coded text is the transcript's own bytes and can be checked. The JSON schema it is held
to is built from the codebook: code ids as an enum, attributes as declared.

Two settings are research claims made runnable:

- `context`: what the model sees at once. `unit` (each unit alone), `window` (each unit with `window_size`
  neighbors either side, marked as context only) or `whole` (the whole recording, cut into overlapping pieces only
  when it does not fit). Two runs that differ only here measure what batching changes.
- `lens`: a theoretical stance, read before the codebook. The same coder under several lenses, like rotating
  graduate coders.

`mode: classify` gives every unit an answer (a code, or `none`); `mode: find` lists the stretches that match, each
one or more consecutive units. `instructions`, when given, replaces the system prompt generated from the codebook
(the council recipes give the paper's prompt there, word for word); the codebook still fixes the schema.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .. import codebooks
from ..artifacts import load_segments, write_json
from ..llm import close_model, est_tokens, open_model, parse_json
from ..usage import llm_metrics
from .base import Check, Context, Item, Result, Setting, Stage, UserError

UNIT_NOUN = {"segment": "Segments", "turn": "Speaker turns", "clip": "Clips", "interval": "Stretches"}


def hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def render(u: dict) -> str:
    who = f" {u['speaker']}" if u.get("speaker") else ""
    return f"[{u['id']}]{who} {hms(u['start'])}-{hms(u['end'])}: {u.get('text', '').strip()}"


def windows(units: list[dict], budget: int, overlap: int) -> list[list[dict]]:
    """All units if they fit `budget` tokens, else overlapping runs of units that each fit."""
    sizes = [est_tokens(render(u)) + 1 for u in units]
    if sum(sizes) <= budget:
        return [units]
    out, i = [], 0
    while i < len(units):
        j, used = i, 0
        while j < len(units) and used + sizes[j] <= budget:
            used += sizes[j]
            j += 1
        j = max(j, i + 1)
        out.append(units[i:j])
        if j >= len(units):
            break
        i = max(i + 1, j - overlap)
    return out


def merge_found(found: list[dict], order: dict[str, int]) -> list[dict]:
    """Stretches from overlapping pieces that share a unit and a code become one (the first one's attributes win,
    filled in from the others where empty)."""
    merged: list[dict] = []
    for f in found:
        ids = set(f["units"])
        hit = [m for m in merged if m["code"] == f["code"] and ids & set(m["units"])]
        if not hit:
            merged.append({**f, "attributes": dict(f["attributes"])})
            continue
        keep = hit[0]
        for m in hit[1:]:
            keep["units"] = list(set(keep["units"]) | set(m["units"]))
            merged.remove(m)
        keep["units"] = list(set(keep["units"]) | ids)
        for k, v in f["attributes"].items():
            if not keep["attributes"].get(k) and v:
                keep["attributes"][k] = v
    for m in merged:
        m["units"] = sorted(m["units"], key=order.__getitem__)
    return sorted(merged, key=lambda m: (order[m["units"][0]], m["code"]))


def schema(cb: dict, mode: str) -> dict:
    attrs = {a["id"]: ({"type": "string", "enum": a["choices"]} if a.get("choices")
                       else {"type": "string", "description": a["help"]}) for a in cb["attributes"]}
    codes = [c["id"] for c in cb["codes"]]
    if mode == "find":
        item = {"type": "object",
                "properties": {"units": {"type": "array", "items": {"type": "string"},
                                         "description": "the id of every unit of this stretch, in order"},
                               "code": {"type": "string", "enum": codes}, **attrs},
                "required": ["units", "code", *attrs]}
        return {"type": "object", "properties": {"found": {"type": "array", "items": item}}, "required": ["found"]}
    code = ({"type": "array", "items": {"type": "string", "enum": codes}} if cb["multiple"]
            else {"type": "string", "enum": [*codes, "none"]})
    item = {"type": "object", "properties": {"unit": {"type": "string"}, "code": code, **attrs},
            "required": ["unit", "code", *attrs]}
    return {"type": "object", "properties": {"coded": {"type": "array", "items": item}}, "required": ["coded"]}


def system_prompt(cb: dict, lens: str, instructions: str, mode: str, unit_noun: str) -> str:
    parts = []
    if lens.strip():
        parts.append("Read the material through this lens:\n" + lens.strip())
    if instructions.strip():
        parts.append(instructions.strip())
    else:
        parts.append(f"You are coding a transcript for a qualitative study. You will receive {unit_noun.lower()}, "
                     "each with an id in brackets. The transcript comes from automatic speech recognition: words "
                     "and speaker labels may be wrong. Apply the codebook below exactly as written.")
        parts.append(codebooks.describe(cb))
        if mode == "find":
            parts.append("List every stretch that takes a code: the ids of all its units, in order, and its code. "
                         "Answer by id only; do not quote the text.")
        else:
            parts.append("Give every unit marked for coding an answer, by its id: "
                         + ("its codes (an empty list when none applies)." if cb["multiple"] else
                            "its code, or none when no code applies.") + " Answer by id only; do not quote the text.")
        parts.append("Respond only with valid JSON matching the schema.")
    return "\n\n".join(parts)


class Code(Stage):
    """Apply a codebook to any segments (turns, Whisper's segments, an imported transcript) with a language model,
    and write annotations. See this module's docstring for context, lens and mode."""
    name = "code"
    label = "Code with a model"
    version = "1"
    gpu = True
    needs = {"units": "segments"}
    makes = {"annotations": "annotations", "annotations_csv": "table", "responses": "other"}
    headline = ("annotations", "codes")
    settings = {
        "codebook": Setting("public-comment.yaml", "The codebook (YAML): codes, definitions, attributes.",
                            kind="file"),
        "lens": Setting("", "Optional: a theoretical stance (a text file) the model reads before the codebook.",
                        kind="file"),
        "instructions": Setting("", "Optional: a system prompt (a text file) used in place of the one written from "
                                "the codebook. The codebook still fixes the codes and fields.", kind="file"),
        "context": Setting("whole", "What the model sees at once: each unit alone, each unit with neighbors, or the "
                           "whole recording.", choices=["unit", "window", "whole"]),
        "window_size": Setting(3, "context: window: neighbors shown either side of the unit being coded."),
        "mode": Setting("find", "find: list the stretches that take a code. classify: answer for every unit.",
                        choices=["find", "classify"]),
        "overlap": Setting(8, "context: whole: units shared by neighboring pieces when a recording does not fit."),
        "endpoint": Setting("managed", "'managed' starts llama-server with `model` and stops it afterwards; or "
                            "the URL of an OpenAI-compatible server already running, e.g. http://127.0.0.1:8081/v1."),
        "model": Setting("gemma-4-26B-A4B-it-MXFP4_MOE.gguf", "GGUF file for a managed server (relative to the "
                         "machine config's models_root)."),
        "context_tokens": Setting(65536, "The model's context window in tokens. 65536 fits the longest council "
                                  "meeting (~57k) whole."),
        "offload": Setting("-ngl 999 --n-cpu-moe 8; -ngl 999 --n-cpu-moe 14; -ngl 999 --n-cpu-moe 22",
                           "llama-server offload arguments, tried in order until the model loads (';'-separated). "
                           "A GPU with enough memory takes plain '-ngl 999'.", in_key=False),
        "thinking": Setting(False, "Let the model reason before answering."),
        "temperature": Setting(0.0, "0 = always the most likely token."),
        "seed": Setting(42, "Sampling seed."),
        "max_tokens": Setting(12000, "Answer length cap per call."),
    }

    def setup(self, settings: dict, ctx: Context):
        state = open_model(settings, ctx, settings["context_tokens"])
        state["codebook"] = codebooks.parse(settings["codebook"])
        return state

    def calls(self, units: list[dict], settings: dict, budget: int) -> list[tuple[list[dict], list[str]]]:
        """(units shown, ids to code) for each model call."""
        if settings["context"] == "whole":
            return [(w, [u["id"] for u in w]) for w in windows(units, budget, settings["overlap"]) if w]
        k = int(settings["window_size"]) if settings["context"] == "window" else 0
        return [(units[max(0, i - k):i + k + 1], [u["id"]]) for i, u in enumerate(units)]

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        llm, cb = state["llm"], state["codebook"]
        doc = load_segments(inputs["units"])
        units = [u for u in doc["units"] if u.get("text", "").strip()]
        by_id = {u["id"]: u for u in units}
        order = {u["id"]: n for n, u in enumerate(units)}
        noun = UNIT_NOUN.get(doc["unit"], "Units")
        system = system_prompt(cb, settings["lens"], settings["instructions"], settings["mode"], noun)
        sch = schema(cb, settings["mode"])
        codes = {c["id"] for c in cb["codes"]}
        budget = llm.n_ctx - settings["max_tokens"] - est_tokens(system) - 500
        if budget < 1000:
            raise UserError(f"code: the model's context ({llm.n_ctx} tokens) leaves no room for the transcript after "
                            f"the instructions and max_tokens ({settings['max_tokens']}).",
                            "Raise context_tokens or lower max_tokens.")
        plan = self.calls(units, settings, budget)
        raw, calls, found, unknown_ids, unknown_codes = [], [], [], [], []
        answered: dict[str, dict] = {}
        for k, (shown, targets) in enumerate(plan):
            tset = set(targets)
            if settings["context"] == "whole":
                part = f" (part {k + 1} of {len(plan)})" if len(plan) > 1 else ""
                user = f"{noun}{part}:\n" + "\n".join(map(render, shown))
            else:
                user = (f"{noun}; code only the one marked >>, the rest are context:\n"
                        + "\n".join((">> " if u["id"] in tset else "   ") + render(u) for u in shown))
            user += ("\n\nList every stretch that takes a code." if settings["mode"] == "find"
                     else "\n\nCode " + ("every unit above." if settings["context"] == "whole" else "the marked unit."))
            r = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                         temperature=settings["temperature"], max_tokens=settings["max_tokens"], schema=sch)
            data = parse_json(r["text"])
            calls.append(r)   # speeds go to the metrics only: they differ every run, and the output must not
            raw.append({"targets": [targets[0], targets[-1]], "parsed": data is not None,
                        **{k2: v for k2, v in r.items() if k2 not in ("prompt_n", "prompt_ms", "gen_n", "gen_ms")}})
            attrs = lambda a: {x["id"]: str(a.get(x["id"], "") or "") for x in cb["attributes"]}  # noqa: E731
            if settings["mode"] == "find":
                for f in (data or {}).get("found", []) or []:
                    if not isinstance(f, dict):
                        continue
                    ids = [i for i in f.get("units") or [] if i in tset]
                    unknown_ids += [i for i in f.get("units") or [] if i not in tset]
                    if f.get("code") not in codes:
                        unknown_codes.append(str(f.get("code")))
                    elif ids:
                        found.append({"units": sorted(set(ids), key=order.__getitem__), "code": f["code"],
                                      "attributes": attrs(f)})
            else:
                for f in (data or {}).get("coded", []) or []:
                    if not isinstance(f, dict):
                        continue
                    uid = f.get("unit")
                    if uid not in tset:
                        unknown_ids.append(str(uid))
                        continue
                    got = f.get("code")
                    got = [got] if isinstance(got, str) else list(got or [])
                    bad = [c for c in got if c not in codes and c != "none"]
                    unknown_codes += bad
                    if uid not in answered and not bad:   # the first answer for a unit wins
                        answered[uid] = {"codes": [c for c in got if c != "none"], "attributes": attrs(f)}
        if settings["mode"] == "find":
            stretches = merge_found(found, order)
        else:
            stretches = [{"units": [uid], "code": c, "attributes": a["attributes"]}
                         for uid, a in sorted(answered.items(), key=lambda kv: order[kv[0]]) for c in a["codes"]]
        anns = [{"id": f"a{n:04d}", "target": {"units": s["units"]}, "code": s["code"], "attributes": s["attributes"]}
                for n, s in enumerate(stretches, 1)]
        write_json(out / "annotations.json", {
            "format": "annotations/1", "codebook": {"name": cb["name"], "sha256": cb["sha256"]},
            "by": {"kind": "model", "id": state["tools"].get("served_model") or settings["model"]},
            "settings": {k: settings[k] for k in ("context", "mode", "window_size")},
            "annotations": anns})
        write_json(out / "responses.json", {"calls": raw})
        attr_ids = [a["id"] for a in cb["attributes"]]
        with open(out / "annotations.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "code", "units", "start", "end", "speakers", *attr_ids, "text"])
            for a in anns:
                us = [by_id[i] for i in a["target"]["units"]]
                w.writerow([a["id"], a["code"], " ".join(a["target"]["units"]), us[0]["start"], us[-1]["end"],
                            " ".join(sorted({u.get("speaker", "") for u in us} - {""})),
                            *[a["attributes"].get(x, "") for x in attr_ids],
                            " ".join(u.get("text", "").strip() for u in us)])
        failed = sum(not x["parsed"] for x in raw)
        per_code = {c: sum(a["code"] == c for a in anns) for c in sorted(codes)}
        checks = [Check("parsed", "fail" if failed else "ok", f"{len(plan) - failed}/{len(plan)}",
                        f"{failed} of {len(plan)} answers did not parse" if failed else "every answer parsed"),
                  Check("unknown_codes", "fail" if unknown_codes else "ok", len(unknown_codes),
                        f"the model gave codes not in the codebook: {sorted(set(unknown_codes))[:5]}"
                        if unknown_codes else "every code is in the codebook"),
                  Check("unknown_ids", "warn" if unknown_ids else "ok", len(unknown_ids),
                        f"the model named {len(unknown_ids)} unit ids that were not there to code" if unknown_ids
                        else "every id named was there")]
        if settings["mode"] == "classify":
            missing = len(units) - len(answered)
            checks.append(Check("coverage", "warn" if missing else "ok", missing,
                                f"{missing} of {len(units)} units got no answer" if missing else "every unit answered"))
        if settings["context"] == "whole":
            checks.append(Check("windows", "warn" if len(plan) > 1 else "ok", len(plan),
                                f"the recording did not fit; coded in {len(plan)} overlapping pieces" if len(plan) > 1
                                else "the whole recording in one call" if plan else "nothing to code"))
        return Result(files={"annotations": "annotations.json", "annotations_csv": "annotations.csv",
                             "responses": "responses.json"}, checks=checks, device="server", tools=state["tools"],
                      metrics={"annotations": len(anns), "units": len(units), "calls": len(plan),
                               **{f"code_{c}": n for c, n in per_code.items()}, **llm_metrics(calls)})

    def teardown(self, state) -> None:
        close_model(state)
