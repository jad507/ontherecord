"""Recipes: what to run, as a file a person can read, diff and cite.

    name: council-left-channel
    description: one meeting, left channel, the hands_on_dl defaults
    source:
      urls:
        - https://www.youtube.com/watch?v=fe0TavdWvEk
    stages:
      - download: {}
      - audio: {channel: left}
      - diarize: {}
      - transcribe: {beam_size: 10}
      - align: {overlap_mode: standard}
      - blocks: {}
      - extract_comments: {thinking: false}

A setting left out takes the stage's default, and the default is written into the run record and every receipt,
so a later change of default cannot silently change what an old run means. A stage entry may also carry
`on_fail: continue`, which lets later stages run on an item even when one of this stage's checks failed (to
measure the damage rather than stop at it).

Each input is typed (segments, speakers, audio...; see stages/base.py) and comes from the latest earlier stage that
makes an artifact of its type: `turns` takes whatever segments came last, from Whisper, from Nemotron or from an
imported transcript. When that stage makes two of the type, the one named like the input wins, else the first it
declares. Opaque types (table, other) are wired by name. To wire it differently, name a stage with `as:` and point
a later stage at it with `from:` (a step, or `step.artifact` to pick one of its outputs). This recipe reproduces how 11 meetings in the ISLS 2027 corpus were made,
with speaker turns from averaged audio and words from the left channel:

      - audio: {channel: average}
        as: audio_average
      - audio: {channel: left}
      - diarize: {}
        from: {audio: audio_average}
      - transcribe: {}          # the latest audio: left

The runner then flags any stage whose inputs trace back to different bytes of the same artifact (here: align, whose
speaker turns and words come from two different audio files).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import stages as registry
from .hashing import sha256_file, sha256_text
from .stages.base import OPAQUE, TYPES, Item, Stage

PROMPTS_DIR = Path(__file__).parent / "prompts"
CODEBOOKS_DIR = Path(__file__).parent / "codebooks"
YOUTUBE_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")
BRACKET_ID = re.compile(r"\[([A-Za-z0-9_-]{6,})\]$")


class RecipeError(ValueError):
    pass


@dataclass
class StageSpec:
    name: str
    cls: type[Stage]
    given: dict                      # as written in the recipe
    run_settings: dict               # what run() receives: defaults filled, file settings replaced by their text
    key_settings: dict               # what the cache key hashes: file settings as their sha256
    record_settings: dict            # what receipts and the run record show: file settings as name, hash and text
    on_fail: str = "stop"
    inputs_from: dict = field(default_factory=dict)   # input -> name of the earlier step it comes from
    mixed: dict = field(default_factory=dict)         # artifact -> the 2+ earlier stages this step's inputs trace to
    input_artifacts: dict = field(default_factory=dict)   # input -> which of that step's artifacts it is

    @property
    def stage(self) -> str:
        """The stage type (what the store and cache key use); `name` is this step's name within the recipe."""
        return self.cls.name

    def info(self) -> dict:
        return {"name": self.name, "stage": self.stage, "label": self.cls.label, "from": self.inputs_from,
                "from_artifact": self.input_artifacts, "mixed": self.mixed, "on_fail": self.on_fail}


@dataclass
class Recipe:
    name: str
    description: str
    items: list[Item]
    stages: list[StageSpec]
    path: Path | None = None
    text: str = ""
    raw: dict = field(default_factory=dict)


def slug(text: str, n: int = 48) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:n] or "item"


def item_for_url(url: str) -> Item:
    m = YOUTUBE_ID.search(url)
    if m:
        return Item(id=m.group(1), url=url)
    m = re.search(r"huggingface\.co/datasets/[^/]+/([^/?#]+)", url)
    if m:
        return Item(id=m.group(1), url=url, title=m.group(1))
    m = re.search(r"/video/(\d+)", url)  # TikTok
    if m:
        return Item(id=f"tt{m.group(1)}", url=url)
    return Item(id=hashlib.sha1(url.encode()).hexdigest()[:12], url=url)


def item_for_file(path: Path) -> Item:
    """A file named the way yt-dlp names them ("Title [id].mp4") keeps its video id."""
    m = BRACKET_ID.search(path.stem)
    return Item(id=m.group(1) if m else slug(path.stem), path=str(path.resolve()), title=path.stem)


def load_items(source: dict, base: Path) -> list[Item]:
    items: list[Item] = []
    urls = list(source.get("urls") or [])
    if source.get("url_file"):
        f = (base / source["url_file"]).expanduser()
        urls += [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
    items += [item_for_url(u) for u in urls]
    for pattern in source.get("files") or []:
        named = pattern if isinstance(pattern, dict) else {}  # {path: ..., id: ...} names the item itself
        pattern = named.get("path", pattern)
        p = Path(pattern).expanduser()
        p = p if p.is_absolute() else base / p
        # A name like "Title [id].mp4" is a file, not a pattern, when that exact file exists
        matches = [p] if p.is_file() or not any(c in p.name for c in "*?[") else sorted(p.parent.glob(p.name))
        if not matches or not all(m.is_file() for m in matches):
            raise RecipeError(f"source file not found: {pattern}")
        if named.get("id"):
            if len(matches) != 1:
                raise RecipeError(f"source {pattern}: an id names one file, but {len(matches)} match")
            items.append(Item(id=str(named["id"]), path=str(matches[0].resolve()), title=str(named["id"])))
        else:
            items += [item_for_file(m) for m in matches]
    seen: dict[str, Item] = {}
    for it in items:
        if it.id in seen:
            raise RecipeError(f"two sources share the item id {it.id!r}: {seen[it.id].as_dict()} and {it.as_dict()}")
        seen[it.id] = it
    if not items:
        raise RecipeError("the recipe's source lists no urls or files")
    return items


def resolve_file(value: str, base: Path) -> Path:
    for cand in (Path(value).expanduser(), base / value, PROMPTS_DIR / value, CODEBOOKS_DIR / value):
        if cand.is_file():
            return cand.resolve()
    raise RecipeError(f"file not found: {value} (looked beside the recipe, in {PROMPTS_DIR} and in {CODEBOOKS_DIR})")


def resolve_settings(cls: type[Stage], given: dict, base: Path) -> tuple[dict, dict, dict]:
    unknown = set(given) - set(cls.settings)
    if unknown:
        raise RecipeError(f"{cls.name}: unknown setting(s) {sorted(unknown)}; it takes {sorted(cls.settings)}")
    run, key, record = {}, {}, {}
    for k, s in cls.settings.items():
        v = given.get(k, s.default)
        if s.choices and v not in s.choices:
            raise RecipeError(f"{cls.name}.{k} = {v!r}; choose one of {s.choices}")
        if s.kind == "file" and v in ("", None):   # an optional file left out: nothing to read or hash
            run[k], record[k] = "", None
            if s.in_key:
                key[k] = None
        elif s.kind == "file":
            path = resolve_file(str(v), base)
            text = path.read_text(encoding="utf-8")
            run[k] = text
            digest = sha256_text(text)
            if s.in_key:
                key[k] = digest
            record[k] = {"file": str(v), "path": str(path), "sha256": digest, "text": text}
        else:
            run[k] = v
            if s.in_key:
                key[k] = v
            record[k] = v
    return run, key, record


STEP_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")


def wire_key(name: str, typ: str) -> str:
    """What an input is matched on: its type, or for an opaque type its name too."""
    return f"{typ}:{name}" if typ in OPAQUE else typ


def pick_artifact(makes: dict[str, str], need: str, typ: str) -> str | None:
    """Which of a step's outputs fills an input: of the input's type, the one named like the input, else the first."""
    cands = [a for a, t in makes.items() if wire_key(a, t) == wire_key(need, typ)]
    return need if need in cands else (cands[0] if cands else None)


def check_declaration(cls: type[Stage]) -> None:
    for kind in ("needs", "makes"):
        decl = getattr(cls, kind)
        if not isinstance(decl, dict) or any(t not in TYPES for t in decl.values()):
            raise RecipeError(f"stage {cls.name}: {kind} must map names to artifact types ({', '.join(TYPES)}); "
                              f"got {decl!r}")


def parse_stages(entries: list, base: Path) -> list[StageSpec]:
    specs: list[StageSpec] = []
    by_name: dict[str, StageSpec] = {}
    latest: dict[str, str] = {}                  # wire key (type) -> the latest step that makes one
    upstream: dict[str, dict[str, set]] = {}     # step -> artifact -> every step upstream of it that made one
    for i, entry in enumerate(entries or []):
        if isinstance(entry, str):
            entry = {entry: {}}
        names = [k for k in entry if k in registry.names()]
        if len(names) != 1:
            raise RecipeError(f"stage entry {i + 1} must name exactly one stage ({registry.names()}): {entry}")
        name = names[0]
        extra = set(entry) - {name, "on_fail", "as", "from"}
        if extra:
            raise RecipeError(f"stage entry {name}: unknown key(s) {sorted(extra)}")
        cls = registry.get(name)
        check_declaration(cls)
        step = str(entry.get("as") or name)
        if not STEP_NAME.match(step):
            raise RecipeError(f"{name}: as: {step!r} must be a letter then letters, digits, _ or -")
        if step != name and step in registry.names():
            raise RecipeError(f"{name}: as: {step!r} is the name of another stage type")
        if step in by_name:
            raise RecipeError(f"two stages are named {step!r}; give the second one its own name with as:")
        wiring = entry.get("from") or {}
        if not isinstance(wiring, dict) or set(wiring) - set(cls.needs):
            raise RecipeError(f"{step}: from: maps the inputs it needs ({list(cls.needs)}) to earlier stages; "
                              f"got {wiring}")
        inputs_from: dict[str, str] = {}
        input_artifacts: dict[str, str] = {}
        for n, typ in cls.needs.items():
            ref = wiring.get(n)
            src, _, art = str(ref).partition(".") if ref else (latest.get(wire_key(n, typ)), "", "")
            if src is None:
                raise RecipeError(f"{step} needs {n} ({typ}), which no earlier stage makes")
            src_makes = by_name[src].cls.makes if src in by_name else {}
            if art and src_makes.get(art) != typ:
                raise RecipeError(f"{step}: from {n}: {src!r} is not an earlier stage that makes {art} as {typ}")
            art = art or pick_artifact(src_makes, n, typ)
            if not art:
                raise RecipeError(f"{step}: from {n}: {src!r} is not an earlier stage that makes {n} ({typ})")
            inputs_from[n], input_artifacts[n] = str(src), art
        ups: dict[str, set] = {}
        already_mixed = set()
        for n, src in inputs_from.items():
            # traced by the producer's artifact name: transcript -> aligned -> turns is one line, not a mix
            for art, steps in [*upstream[src].items(), (input_artifacts[n], {src})]:
                ups.setdefault(art, set()).update(steps)
                if len(steps) > 1:
                    already_mixed.add(art)
        given = entry[name] or {}
        run, key, record = resolve_settings(cls, given, base)
        on_fail = entry.get("on_fail", "stop")
        if on_fail not in ("stop", "continue"):
            raise RecipeError(f"{step}: on_fail must be 'stop' or 'continue'")
        # Flagged only where the mixing first happens; the stages after it inherit it.
        spec = StageSpec(step, cls, given, run, key, record, on_fail, inputs_from,
                         {a: sorted(s) for a, s in ups.items() if len(s) > 1 and a not in already_mixed},
                         input_artifacts)
        specs.append(spec)
        by_name[step] = spec
        upstream[step] = ups
        latest.update({wire_key(m, t): step for m, t in cls.makes.items()})
    if not specs:
        raise RecipeError("the recipe lists no stages")
    return specs


def load_recipe(path: str | Path) -> Recipe:
    path = Path(path).resolve()
    text = path.read_text(encoding="utf-8")
    return recipe_from_text(text, path.parent, path)


def recipe_from_text(text: str, base: Path, path: Path | None = None) -> Recipe:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise RecipeError(f"the recipe is not valid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise RecipeError("a recipe is a set of 'key: value' lines (name, source, stages)")
    items = load_items(raw.get("source") or {}, base)
    stages = parse_stages(raw.get("stages"), base)
    return Recipe(name=str(raw.get("name") or (path.stem if path else "run")), description=raw.get("description", ""),
                  items=items, stages=stages, path=path, text=text, raw=raw)


def source_identity(item: Item) -> str:
    """What the first stage's cache key hashes in place of an input: the URL, or the bytes of a local file."""
    return item.url if item.url else "sha256:" + sha256_file(item.path)
