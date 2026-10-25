"""Reviews: sets of items a person judges by hand, the yardstick the machine's output is scored against.

Generalized from two pages built for the research, hands_on_dl's gold coding ("is this block a public comment?")
and AITranscribe's stress labelling ("which word is stressed?"). What they share, every set here keeps:

- Blind by construction. The page never receives what a set keeps hidden (strata, votes), and receives the
  machine's answer, if the set has one, only after the person has answered, unless they ask to see it first, or
  never (`reveal: never`, as the pre-registered gold sample needs). Each answer records whether the machine's answer
  had been seen (machine_seen), and scoring uses the first unseen answer per item.
- One seeded shuffle, so any prefix of the order is a random sample: stopping after 150 of 400 is fine.
- An append-only log (answers.jsonl): nothing is lost, the latest answer per item wins, and each answer carries the
  seconds spent on it, which give the time left at this person's pace.
- The item's own audio, cut from the recording on first play, and its neighbors as context.

A set is a folder in <workspace>/reviews/: set.json (the question and the items, never changed once made) and
answers.jsonl. Sets come from runs (`from_run`: comments, speakers, transcript) or from a JSON file that another
project writes (`create`; scripts/ has the two pages above). Answer kinds:

    choice  one of the options (yes / no / unsure)
    words   some of the item's words (the stressed ones), or one of the options (none, unsure, bad)
    text    a text (a corrected line, a speaker's name), or one of the options
    check   a speaker turn checked word by word on the Check page (/check): the corrected text, each edit (made
            with a choice key, typed, deleted), the codes applied to stretches of it, and which control scheme did
            it. The machine's text is shown (it is what is being corrected), so these answers measure the person's
            corrections and speed, not Whisper without bias: use a blind transcript set for that.

An option with `"score": false` (unsure, bad) is recorded but left out of scoring; an option's `reasons` are a
second choice it requires (bad: cut off, wrong words, two readings).
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import statistics
import subprocess
import threading
import time
from pathlib import Path

KINDS = ("choice", "words", "text", "check")
_WRITE = threading.Lock()
# The Check page's code keys, in the order codes take them: left hand, number row then top row
CODE_KEYS = ("1", "2", "3", "4", "5", "Q", "W", "E", "R", "T")
# Placeholders for a council meeting until a project names its own (`review new --codes`)
DEFAULT_CODES = [{"key": k, "name": n} for k, n in zip(CODE_KEYS, (
    "Public comment", "Question", "Request", "Concern", "Support", "Procedure", "Personal story", "Fact or figure",
    "Disagreement", "Come back to this"))]


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def root(workspace: Path) -> Path:
    return workspace / "reviews"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "review"


# ---------------------------------------------------------------------------------------------- making a set
def create(workspace: Path, spec: dict) -> str:
    """Write a new set from a spec (see the module docstring and scripts/), and return its id."""
    ans = spec.get("answer") or {}
    if ans.get("kind") not in KINDS:
        raise ValueError(f"answer.kind must be one of {', '.join(KINDS)}")
    if ans["kind"] == "choice" and not ans.get("options"):
        raise ValueError("a choice set needs answer.options")
    items = spec.get("items") or []
    ids = [str(it.get("id", "")) for it in items]
    if not items or not all(ids) or len(set(ids)) != len(ids):
        raise ValueError("a set needs items, each with its own id")
    if ans["kind"] in ("words", "check") and not all(it.get("words") for it in items):
        raise ValueError(f"a {ans['kind']} set needs each item's words")
    if ans["kind"] == "check":
        ans = {"codes": DEFAULT_CODES, **ans}
        keys = [c.get("key") for c in ans["codes"]]
        if len(keys) != len(set(keys)) or not set(keys) <= set(CODE_KEYS):
            raise ValueError(f"each code needs its own key, one of {' '.join(CODE_KEYS)}")
    seed = int(spec.get("seed", 20261023))
    order = list(ids)
    if spec.get("order", "shuffled") == "shuffled":
        random.Random(seed).shuffle(order)
    base = _slug(spec.get("name") or "review") + time.strftime("-%Y%m%d")
    set_id, n = base, 1
    while (root(workspace) / set_id).exists():
        n += 1
        set_id = f"{base}-{n}"
    out = {"id": set_id, "name": spec.get("name") or set_id, "created_utc": utc(),
           "question": spec.get("question", ""), "answer": {"note": True, "flags": [], "options": [], **ans},
           "machine_label": spec.get("machine_label", "the machine"),
           "reveal": spec.get("reveal", "after") if any("machine" in it for it in items) else "never",
           "prefill": bool(spec.get("prefill")), "order": order, "seed": seed,
           "source": spec.get("source") or {}, "items": items}
    d = root(workspace) / set_id
    d.mkdir(parents=True)
    (d / "set.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    return set_id


def load(workspace: Path, set_id: str) -> dict:
    p = root(workspace) / set_id / "set.json"
    if not re.fullmatch(r"[a-z0-9-]+", set_id) or not p.is_file():
        raise KeyError(set_id)
    s = json.loads(p.read_text(encoding="utf-8"))
    s["by_id"] = {str(it["id"]): it for it in s["items"]}
    return s


def list_sets(workspace: Path) -> list[dict]:
    out = []
    for p in sorted(root(workspace).glob("*/set.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            out.append(progress(workspace, p.parent.name))
        except (KeyError, json.JSONDecodeError):
            continue
    return out


# ---------------------------------------------------------------------------------------------- answering
def rows(workspace: Path, set_id: str) -> list[dict]:
    p = root(workspace) / set_id / "answers.jsonl"
    out = []
    for line in (p.read_text(encoding="utf-8").splitlines() if p.is_file() else []):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _append(workspace: Path, set_id: str, row: dict) -> None:
    with _WRITE, open(root(workspace) / set_id / "answers.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def latest(log: list[dict]) -> dict[str, dict]:
    """The latest answer per item (reveal events are not answers)."""
    out = {}
    for r in log:
        if "value" in r:
            out[str(r["item"])] = r
    return out


def public_item(s: dict, item_id: str, k: int = 2) -> dict:
    """What the page may see of an item: never `hidden`, never `machine` (that comes from reveal()); the prefill
    only when the set prefills; context trimmed to k neighbors either side."""
    it = s["by_id"][item_id]
    out = {key: v for key, v in it.items() if key not in ("hidden", "machine", "prefill", "context", "clip")}
    out["context"] = [c for c in it.get("context", []) if abs(c.get("offset", 0)) <= k]
    out["more_context"] = any(abs(c.get("offset", 0)) > k for c in it.get("context", []))
    out["has_clip"] = bool(it.get("clip"))
    out["has_machine"] = "machine" in it and s["reveal"] != "never"
    c = it.get("clip") or {}
    if c.get("path"):  # where the clip starts in the recording, so the page can play word by word
        out["clip_start"] = max(0.0, float(c["start"]) - 1)
    if s.get("prefill"):
        out["prefill"] = it.get("prefill", it.get("machine"))
    return out


def _check(s: dict, it: dict, value, reason: str) -> str | None:
    """Why an answer is not valid for this item, or None."""
    opts = {o["value"]: o for o in s["answer"].get("options", [])}
    kind = s["answer"]["kind"]
    if isinstance(value, str) and value in opts:
        reasons = [r["value"] for r in opts[value].get("reasons", [])]
        if reasons and reason not in reasons:
            return f"{value!r} needs a reason: {', '.join(reasons)}"
        return None
    if kind == "choice":
        return f"the answer must be one of {', '.join(opts)}"
    if kind == "check":
        if not isinstance(value, dict):
            return "a checked turn, or one of the options"
        n = len(value["words"])
        codes = {c["key"] for c in s["answer"].get("codes", [])}
        if any(c["code"] not in codes or not 0 <= c["from"] <= c["to"] < n for c in value["codes"]):
            return "a code is not one of the set's, or covers words the turn does not have"
        return None
    if kind == "words":
        if not isinstance(value, list) or not value or not all(isinstance(i, int) for i in value):
            return "pick at least one word, or one of the options"
        if any(not 0 <= i < len(it["words"]) for i in value):
            return "a word index is out of range"
        return None
    return None if isinstance(value, str) and value.strip() else "write something, or pick one of the options"


def answer(workspace: Path, set_id: str, item_id: str, value, *, reason: str = "", flags: dict | None = None,
           note: str = "", seconds: float | None = None, by: str = "me") -> dict:
    """Record an answer; returns it, with the machine's answer when the set reveals it after answering."""
    s = load(workspace, set_id)
    it = s["by_id"].get(str(item_id))
    if it is None:
        raise KeyError(f"no item {item_id} in {set_id}")
    if isinstance(value, list):
        value = sorted({int(i) for i in value})
    if isinstance(value, dict):
        value = clean_check(value)
    problem = _check(s, it, value, reason)
    if problem:
        raise ValueError(problem)
    log = rows(workspace, set_id)
    seen = (s.get("prefill") and s["answer"]["kind"] in ("text", "check")
            or any(r.get("event") == "reveal" and str(r["item"]) == str(item_id) for r in log)
            or any("value" in r and str(r["item"]) == str(item_id) for r in log) and s["reveal"] == "after")
    names = {f["name"] for f in s["answer"].get("flags", [])}
    row = {"item": str(item_id), "value": value.strip() if isinstance(value, str) else value,
           "reason": reason or "", "flags": {k: bool(v) for k, v in (flags or {}).items() if k in names},
           "note": " ".join(str(note or "").split()), "seconds": round(seconds) if seconds else None,
           "machine_seen": int(bool(seen)), "by": by or "me", "utc": utc()}
    _append(workspace, set_id, row)
    if s["reveal"] == "after" and "machine" in it:
        return {**row, "machine": it["machine"]}
    return row


def reveal(workspace: Path, set_id: str, item_id: str, by: str = "me"):
    """The machine's answer, before answering: logged, so the answer that follows counts as seen."""
    s = load(workspace, set_id)
    it = s["by_id"].get(str(item_id))
    if it is None or "machine" not in it or s["reveal"] == "never":
        raise KeyError(f"{set_id}/{item_id} has no machine answer to show")
    _append(workspace, set_id, {"item": str(item_id), "event": "reveal", "by": by, "utc": utc()})
    return it["machine"]


# ---------------------------------------------------------------------------------------------- progress, scores
def progress(workspace: Path, set_id: str) -> dict:
    s = load(workspace, set_id)
    log = rows(workspace, set_id)
    done = latest(log)
    secs = [r["seconds"] for r in log if r.get("seconds")]
    pace = statistics.median(secs) if secs else None
    left = len(s["order"]) - len(done)
    sc = score(s, log) if any("machine" in it for it in s["items"]) else None
    return {"id": set_id, "name": s["name"], "kind": s["answer"]["kind"], "created_utc": s["created_utc"],
            "source": s.get("source", {}), "items": len(s["order"]), "answered": len(done), "left": left,
            "seconds_per_item": round(pace) if pace else None, "left_s": round(pace * left) if pace else None,
            "agreement": sc.get("agreement") if sc else None, "machine_label": s.get("machine_label"),
            "reveal": s["reveal"]}


def clean_check(value: dict) -> dict:
    """A checked turn as it is recorded: only the known fields, with their types."""
    def num(v, default=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    words = [{"t": str(w.get("t", "")), "i": num(w.get("i"), -1), **({"unclear": True} if w.get("unclear") else {})}
             for w in (value.get("words") or []) if isinstance(w, dict)]
    edits = [{"i": num(e.get("i"), -1), "n": num(e.get("n"), 1), "from": str(e.get("from", "")),
              "to": str(e.get("to", "")), "how": str(e.get("how", "typed"))[:12]}
             for e in (value.get("edits") or []) if isinstance(e, dict)]
    codes = [{"code": str(c.get("code", "")), "from": num(c.get("from"), -1), "to": num(c.get("to"), -1)}
             for c in (value.get("codes") or []) if isinstance(c, dict)]
    text = " ".join(str(value.get("text", "")).split()) or " ".join(w["t"] for w in words if w["t"])
    return {"text": text, "words": words, "edits": edits, "codes": codes, "scheme": str(value.get("scheme", ""))[:24],
            "keys": num(value.get("keys")), "replays": num(value.get("replays"))}


def _text(value) -> str:
    return value.get("text", "") if isinstance(value, dict) else str(value)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def _edits(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def scoring_answers(s: dict, log: list[dict]) -> dict[str, dict]:
    """The answer each item is scored on: the first one given before the machine's answer was seen, else (sets
    without a hidden machine answer, or post-editing) the latest. Options marked score: false are left out."""
    skip = {o["value"] for o in s["answer"].get("options", []) if o.get("score") is False}
    first_blind, last = {}, {}
    for r in log:
        if "value" not in r:
            continue
        last[r["item"]] = r
        if not r.get("machine_seen") and r["item"] not in first_blind:
            first_blind[r["item"]] = r
    use = first_blind if s["reveal"] == "after" and not s.get("prefill") else last
    return {k: r for k, r in use.items() if not (isinstance(r["value"], str) and r["value"] in skip)}


def score(s: dict, log: list[dict]) -> dict:
    """How often the machine agreed with the person, by kind: agreement and Cohen's kappa with a confusion table
    (choice); exact match and whether the machine's top word was among the person's (words); word error rate of
    the machine's text against the person's (text)."""
    use = scoring_answers(s, log)
    pairs = [(s["by_id"][k]["machine"], r) for k, r in use.items() if "machine" in s["by_id"].get(k, {})]
    kind = s["answer"]["kind"]
    out = {"scored": len(pairs), "answered": len(latest(log)),
           "blind": s["reveal"] == "after" and not s.get("prefill"), "machine_label": s.get("machine_label")}
    if not pairs:
        return out
    if kind == "choice":
        conf: dict = {}
        for m, r in pairs:
            conf.setdefault(str(m), {}).setdefault(str(r["value"]), 0)
            conf[str(m)][str(r["value"])] += 1
        agree = sum(str(m) == str(r["value"]) for m, r in pairs) / len(pairs)
        labels = sorted({str(m) for m, _ in pairs} | {str(r["value"]) for _, r in pairs})
        n = len(pairs)
        pe = sum((sum(conf.get(l, {}).values()) / n) * (sum(c.get(l, 0) for c in conf.values()) / n) for l in labels)
        out.update(agreement=round(agree, 3), kappa=round((agree - pe) / (1 - pe), 3) if pe < 1 else None,
                   confusion=conf)
    elif kind == "words":
        picked = [(m if isinstance(m, list) else [m], r) for m, r in pairs if isinstance(r["value"], list)]
        if picked:
            out.update(exact=round(sum(sorted(m) == r["value"] for m, r in picked) / len(picked), 3),
                       agreement=round(sum(m[0] in r["value"] for m, r in picked) / len(picked), 3),
                       scored=len(picked))
    else:
        pairs = [(m, r) for m, r in pairs if kind != "check" or isinstance(r["value"], dict)]
        ed = sum(_edits(_words(_text(r["value"])), _words(str(m))) for m, r in pairs)
        ref = sum(len(_words(_text(r["value"]))) for _, r in pairs)
        out.update(wer=round(ed / ref, 3) if ref else None, agreement=round(1 - ed / ref, 3) if ref else None,
                   scored=len(pairs))
        if kind == "check":
            out.update(check_summary(s, [r for _, r in pairs]))
    return out


def check_summary(s: dict, answers: list[dict]) -> dict:
    """What the choices were worth and how fast each control scheme went: edits by how they were made; of the
    words a person typed over that had choices, how often the typed word was one of them (a choice that went
    unnoticed or unreachable); codes applied; and per scheme, seconds per minute of audio."""
    how: dict[str, int] = {}
    typed_doubtful = typed_was_choice = codes = 0
    schemes: dict[str, dict] = {}
    for r in answers:
        v, it = r["value"], s["by_id"].get(r["item"], {})
        words = it.get("words") or []
        for e in v["edits"]:
            how[e["how"]] = how.get(e["how"], 0) + 1
            alts = words[e["i"]].get("alts") if 0 <= e["i"] < len(words) else None
            if e["how"] == "typed" and alts:
                typed_doubtful += 1
                typed_was_choice += any(_words(a["t"]) == _words(e["to"]) for a in alts)
        codes += len(v["codes"])
        sc = schemes.setdefault(v["scheme"] or "unknown", {"turns": 0, "audio_s": 0.0, "seconds": 0})
        sc["turns"] += 1
        sc["audio_s"] += float(it.get("end", 0)) - float(it.get("start", 0))
        sc["seconds"] += r.get("seconds") or 0
    for sc in schemes.values():
        sc["audio_s"] = round(sc["audio_s"], 1)
        sc["seconds_per_audio_minute"] = round(sc["seconds"] / (sc["audio_s"] / 60), 1) if sc["audio_s"] else None
    doubtful = sum(1 for it in s["items"] for w in it.get("words", []) if w.get("alts"))
    return {"edits": how, "typed_over_a_doubtful_word": typed_doubtful, "typed_word_was_a_choice": typed_was_choice,
            "codes_applied": codes, "words_with_choices_in_set": doubtful, "by_scheme": schemes}


def export_rows(workspace: Path, set_id: str) -> list[dict]:
    """One row per answered item (its latest answer), with the item's machine answer and hidden fields, for
    analysis. Never served to the labelling page."""
    s = load(workspace, set_id)
    log = rows(workspace, set_id)
    scored = scoring_answers(s, log)
    out = []
    for k, r in latest(log).items():
        it = s["by_id"].get(k, {})
        out.append({"item": k, "title": it.get("title", ""), "text": it.get("text", ""), **{
            "answer": (" ".join(it["words"][i] for i in r["value"]) if isinstance(r["value"], list)
                       else _text(r["value"])),
            "answer_raw": json.dumps(r["value"]), "reason": r["reason"], "flags": json.dumps(r["flags"]),
            "note": r["note"], "seconds": r["seconds"], "machine_seen": r["machine_seen"], "by": r["by"],
            "utc": r["utc"], "scored_on": json.dumps(scored[k]["value"]) if k in scored else "",
            "machine": json.dumps(it.get("machine")) if "machine" in it else "",
            "hidden": json.dumps(it.get("hidden", {}))}})
    return out


def fingerprints(workspace: Path, set_id: str) -> dict:
    """sha256 of the set and of the answers as they stand: what a write-up cites when it reports a score."""
    d = root(workspace) / set_id
    h = lambda p: hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    return {"set_sha256": h(d / "set.json"), "answers_sha256": h(d / "answers.jsonl")}


# ---------------------------------------------------------------------------------------------- audio
def clip(workspace: Path, set_id: str, item_id: str, ffmpeg: str = "ffmpeg", ctx: int | None = None) -> Path | None:
    """The item's audio (a second either side), cut from its recording once and kept beside the set. With `ctx`, the
    audio of the neighboring block at that offset instead (only the item's own neighbors, from the same recording)."""
    s = load(workspace, set_id)
    it = s["by_id"].get(str(item_id)) or {}
    c = it.get("clip")
    if not c:
        return None
    if c.get("file"):  # already a clip of its own (the stress clips), with no recording to cut neighbors from
        p = Path(c["file"])
        return p if p.is_file() and ctx is None else None
    a, b, key = c["start"], c["end"], str(item_id)
    if ctx is not None:
        n = next((x for x in it.get("context", []) if x.get("offset") == ctx and x.get("start") is not None), None)
        if n is None:
            return None
        a, b, key = n["start"], n["end"], f"{item_id}#{ctx}"
    wav = c.get("format") == "wav"  # the Check page seeks word by word: PCM has no codec delay to allow for
    out = root(workspace) / set_id / "clips" / (hashlib.sha1(key.encode()).hexdigest()[:16]
                                                + (".wav" if wav else ".m4a"))
    if not out.is_file():
        src = Path(c["path"])
        if not src.is_file():
            return None
        start = max(0.0, float(a) - 1)
        out.parent.mkdir(exist_ok=True)
        tmp = out.with_suffix(".part" + out.suffix)
        codec = ["-ar", "16000", "-c:a", "pcm_s16le"] if wav else ["-c:a", "aac", "-b:a", "64k"]
        subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-t",
                        f"{float(b) + 1 - start:.3f}", "-i", str(src), "-vn", "-ac", "1", *codec, str(tmp)],
                       check=False, timeout=120)
        if not tmp.is_file():
            return None
        tmp.replace(out)
    return out


# ---------------------------------------------------------------------------------------------- sets from runs
GOLD_QUESTION = ("Is the TARGET block a member of the public addressing the body? Council members, staff, presenters "
                 "and procedural speech are no. If the block runs two speakers together (say a public comment and the "
                 "chair's reply), answer unsure, tick fused, and say what is fused in the note. Grey blocks are "
                 "context only.")


def _output(store_root: Path, rec: dict, item: str, stage: str, name: str) -> Path | None:
    """The file a run's step of this stage made for this item (the last such step)."""
    found = None
    for st in rec["stages"]:
        if st.get("stage", st["name"]) != stage:
            continue
        cell = rec["cells"].get(item, {}).get(st["name"]) or {}
        if not cell.get("receipt"):
            continue
        r = json.loads((store_root / cell["receipt"]).read_text(encoding="utf-8"))
        out = (r.get("outputs") or {}).get(name)
        if out:
            p = Path(out["file"])
            found = p if p.is_absolute() else (store_root / cell["receipt"]).parent / p
    return found


def _link(url: str | None, t: float) -> str | None:
    if url and ("youtube.com" in url or "youtu.be" in url):
        return f"{url}{'&' if '?' in url else '?'}t={int(t)}s"
    return None


def turns(segments: list[dict], limit_s: float = 60.0, gap_s: float = 5.0) -> list[dict]:
    """Speaker turns from word_choices' lines: consecutive lines of one speaker, cut into parts of at most
    limit_s seconds (at line boundaries) and wherever the speaker is silent for gap_s. Each turn has its words."""
    out: list[dict] = []
    for sg in segments:
        if not sg.get("words"):
            continue
        last = out[-1] if out else None
        if (last and last["speaker"] == sg.get("speaker", "") and sg["end"] - last["start"] <= limit_s
                and sg["start"] - last["end"] < gap_s):
            last["end"] = sg["end"]
            last["text"] += " " + sg["text"]
            last["words"] += sg["words"]
        else:
            out.append({"speaker": sg.get("speaker", ""), "start": sg["start"], "end": sg["end"], "text": sg["text"],
                        "words": list(sg["words"])})
    k = 0
    while k < len(out):  # number the parts of a long turn: part 2 of 5
        j = k
        while j + 1 < len(out) and out[j + 1]["speaker"] == out[k]["speaker"] and out[j + 1]["start"] - out[j]["end"] < gap_s:
            j += 1
        if j > k:
            for n in range(k, j + 1):
                out[n]["part"] = [n - k + 1, j - k + 1]
        k = j + 1
    return out


def from_run(store_root: Path, run_id: str, kind: str, sample: int | None = 100, seed: int = 20261023,
             name: str | None = None, items: list[str] | None = None, blind: bool = False,
             codes: list[str] | None = None, turn_seconds: float = 60.0) -> dict:
    """A set spec from a finished run, for create():

    comments    blocks sampled half from those the comment extractor put in a public comment and half from the
                rest, each asked the gold question (yes / no / unsure, fused); the extractor's answer is revealed
                after answering. Needs the blocks and extract_comments stages.
    speakers    one item per speaker label per recording: its longest lines and a clip, answered with a name or a
                role ("Chair", "Clerk", "member of the public"). Needs align.
    transcript  lines sampled across the recordings, the transcript's text to correct (or, blind, to write from
                scratch); scored as the machine's word error rate. Needs transcribe or align.
    check       every speaker turn (parts of at most turn_seconds), in order, to check word by word and code on the
                Check page; `codes` names the codes for keys 1-5 and Q-T. A sample, if given, is drawn at random and
                kept in order. Needs word_choices.
    """
    rec = json.loads((store_root / "runs" / run_id / "run.json").read_text(encoding="utf-8"))
    rng = random.Random(seed)
    meta = {i["id"]: i for i in rec["items"]}
    wanted = [i for i in rec["cells"] if not items or i in items]
    out_items: list[dict] = []
    for item in wanted:
        audio = _output(store_root, rec, item, "audio", "audio")
        url, title = meta[item].get("url"), meta[item].get("title") or item
        info = _output(store_root, rec, item, "download", "info")
        if info and info.is_file():  # the recording's own title (yt-dlp's, or the file's name), as the Library shows
            title = json.loads(info.read_text(encoding="utf-8")).get("title") or title
        clip = (lambda a, b: {"path": str(audio), "start": a, "end": b}) if audio else (lambda a, b: None)
        if kind == "check":
            wp = _output(store_root, rec, item, "word_choices", "words")
            if not wp:
                continue
            tt = turns(json.loads(wp.read_text(encoding="utf-8"))["segments"], turn_seconds)
            for n, t in enumerate(tt):
                ctx = [{"offset": j - n, "speaker": x["speaker"], "start": x["start"], "end": x["end"],
                        "text": x["text"]} for j, x in enumerate(tt) if 0 < abs(j - n) <= 3]
                c = clip(t["start"], t["end"])
                out_items.append({"id": f"{item}:{n:04d}", "recording": item, "title": title, "speaker": t["speaker"],
                                  "start": t["start"], "end": t["end"], "text": t["text"], "words": t["words"],
                                  **({"part": t["part"]} if t.get("part") else {}), "context": ctx,
                                  "clip": {**c, "format": "wav"} if c else None, "link": _link(url, t["start"]),
                                  "machine": t["text"]})
            continue
        if kind == "comments":
            bp, cp = _output(store_root, rec, item, "blocks", "blocks"), _output(store_root, rec, item,
                                                                                    "extract_comments", "comments")
            if not bp or not cp:
                continue
            meeting = json.loads(bp.read_text(encoding="utf-8"))
            blocks = meeting["blocks"]
            title = meeting.get("title") or title
            flagged = {i for c in json.loads(cp.read_text(encoding="utf-8")).get("comments", []) for i in c["block_ids"]}
            for n, b in enumerate(blocks):
                ctx = [{"offset": j - n, "speaker": x["speaker"], "start": x["start"], "end": x["end"],
                        "text": x.get("text", "")} for j, x in enumerate(blocks) if abs(j - n) <= 6]
                out_items.append({"id": f"{item}:{b['block_id']}", "recording": item, "title": title,
                                  "speaker": b["speaker"], "start": b["start"], "end": b["end"],
                                  "text": b.get("text", ""), "context": ctx, "clip": clip(b["start"], b["end"]),
                                  "link": _link(url, b["start"]),
                                  "machine": "yes" if b["block_id"] in flagged else "no",
                                  "hidden": {"stratum": "flagged" if b["block_id"] in flagged else "not flagged"}})
        else:
            src = _output(store_root, rec, item, "align", "aligned") or _output(store_root, rec, item, "transcribe",
                                                                                 "transcript")
            if not src:
                continue
            segs = [s for s in json.loads(src.read_text(encoding="utf-8")).get("segments", []) if s.get("text")]
            if kind == "speakers":
                by: dict[str, list] = {}
                for s in segs:
                    if s.get("speaker"):
                        by.setdefault(s["speaker"], []).append(s)
                for spk, ss in sorted(by.items(), key=lambda kv: -sum(s["end"] - s["start"] for s in kv[1])):
                    top = sorted(ss, key=lambda s: s["start"] - s["end"])[:4]
                    longest = top[0]
                    out_items.append({
                        "id": f"{item}:{spk}", "recording": item, "title": title, "speaker": spk,
                        "start": longest["start"], "end": min(longest["end"], longest["start"] + 30),
                        "text": longest["text"].strip(), "clip": clip(longest["start"],
                                                                      min(longest["end"], longest["start"] + 30)),
                        "link": _link(url, longest["start"]),
                        "context": [{"offset": k + 1, "speaker": spk, "start": s["start"], "end": s["end"],
                                     "text": s["text"].strip()} for k, s in enumerate(sorted(top[1:], key=lambda s: s["start"]))],
                        "hidden": {"talk_s": round(sum(s["end"] - s["start"] for s in ss)), "lines": len(ss)}})
            elif kind == "transcript":
                for n, s in enumerate(segs):
                    ctx = [{"offset": j - n, "speaker": x.get("speaker") or "", "start": x["start"], "end": x["end"],
                            "text": x["text"].strip()} for j, x in enumerate(segs) if 0 < abs(j - n) <= 3]
                    out_items.append({"id": f"{item}:{n}", "recording": item, "title": title,
                                      "speaker": s.get("speaker") or "", "start": s["start"], "end": s["end"],
                                      "text": "" if blind else s["text"].strip(), "context": ctx,
                                      "clip": clip(s["start"], s["end"]), "link": _link(url, s["start"]),
                                      "machine": s["text"].strip()})
            else:
                raise ValueError(f"no review kind {kind!r}: comments, speakers, transcript or check")
    if not out_items:
        need = {"comments": "blocks and extract_comments", "speakers": "align", "transcript": "transcribe or align",
                "check": "word_choices"}
        raise ValueError(f"run {run_id} has nothing to review as {kind}: it needs {need.get(kind, kind)}")
    if kind == "comments":
        yes = [i for i in out_items if i["machine"] == "yes"]
        no = [i for i in out_items if i["machine"] == "no"]
        sample = sample or 100
        k = min(len(yes), sample // 2)
        out_items = rng.sample(yes, k) + rng.sample(no, min(len(no), sample - k))
    elif kind == "transcript":
        out_items = rng.sample(out_items, min(len(out_items), sample or 100))
    elif kind == "check" and sample and sample < len(out_items):
        out_items = [out_items[k] for k in sorted(rng.sample(range(len(out_items)), sample))]
    label = rec.get("name", run_id)
    common = {"seed": seed, "source": {"run": run_id, "kind": kind, "made": utc()}}
    if kind == "comments":
        return {**common, "name": name or f"public comments, {label}", "question": GOLD_QUESTION,
                "machine_label": "the comment extractor", "items": out_items,
                "answer": {"kind": "choice", "options": [
                    {"value": "yes", "label": "Yes", "key": "y"}, {"value": "no", "label": "No", "key": "n"},
                    {"value": "unsure", "label": "Unsure", "key": "u", "score": False}],
                    "flags": [{"name": "fused", "label": "fuses two speakers", "key": "f"}]}}
    if kind == "check":
        keys = list(CODE_KEYS)
        named = [{"key": k, "name": n.strip()} for k, n in zip(keys, codes or []) if n.strip()]
        return {**common, "name": name or f"check, {label}", "order": "given", "items": out_items, "prefill": True,
                "machine_label": "Whisper",
                "question": "Fix what the transcript got wrong in this turn, then code it.",
                "answer": {"kind": "check", "codes": named or DEFAULT_CODES,
                           "flags": [{"name": "wrong_speaker", "label": "wrong speaker label"},
                                     {"name": "crosstalk", "label": "people talk over each other"}],
                           "options": [{"value": "no speech", "label": "No speech", "score": False},
                                       {"value": "unsure", "label": "Can't make it out", "score": False}]}}
    if kind == "speakers":
        return {**common, "name": name or f"speakers, {label}", "order": "given", "items": out_items,
                "question": "Who is this speaker label? Give a name, or a role such as Chair, Clerk, staff or member "
                            "of the public. If the label covers more than one person, say so.",
                "answer": {"kind": "text", "options": [
                    {"value": "several people", "label": "Several people", "key": "s", "score": False},
                    {"value": "unsure", "label": "Can't tell", "key": "u", "score": False}]}}
    return {**common, "name": name or f"transcript lines, {label}", "items": out_items, "prefill": not blind,
            "machine_label": "Whisper",
            "question": ("Write exactly what is said in the TARGET line." if blind else
                         "Correct the TARGET line so it says exactly what is said: fix wrong words, add missing ones, "
                         "delete invented ones. Grey lines are context only."),
            "answer": {"kind": "text", "options": [
                {"value": "no speech", "label": "No speech", "key": "x", "score": False},
                {"value": "unsure", "label": "Can't make it out", "key": "u", "score": False}]}}
