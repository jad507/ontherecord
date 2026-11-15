"""Codebooks: the one object a human coder, a model coder and the write-up share (audit B1).

A codebook is a YAML file, given to the `code` stage as a file setting, so its whole text and sha256 land in every
receipt. Shipped examples live in src/ontherecord/codebooks/; a recipe may also name one beside it.

    name: public-comment
    question: Is this speech by a member of the public addressing the body?
    multiple: false            # may one unit carry several codes?
    codes:
      - id: public_comment
        name: Public comment
        definition: ...
        include: [...]         # optional examples
        exclude: [...]
    attributes:                # optional fields the coder also fills in
      - {id: position, choices: [support, oppose, mixed]}
      - {id: summary, help: one or two sentences}
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .hashing import sha256_text

DIR = Path(__file__).parent / "codebooks"
ID = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
RESERVED = {"none"}   # what a classifying coder answers for a unit that takes no code


class CodebookError(ValueError):
    pass


def parse(text: str) -> dict:
    """The codebook in `text`, checked: a name, at least one code, unique ids, a definition for every code."""
    try:
        cb = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise CodebookError(f"the codebook is not valid YAML: {e}") from None
    if not isinstance(cb, dict) or not str(cb.get("name") or "").strip():
        raise CodebookError("a codebook needs a name")
    codes = cb.get("codes")
    if not isinstance(codes, list) or not codes:
        raise CodebookError(f"codebook {cb['name']}: list at least one code under codes:")
    seen: set[str] = set()
    out_codes = []
    for i, c in enumerate(codes):
        if not isinstance(c, dict):
            raise CodebookError(f"codebook {cb['name']}: code {i + 1} must be id, name, definition...")
        cid = str(c.get("id") or "")
        if not ID.match(cid) or cid in RESERVED:
            raise CodebookError(f"codebook {cb['name']}: code id {cid!r} must be lowercase letters, digits and _ "
                                f"(not {', '.join(sorted(RESERVED))})")
        if cid in seen:
            raise CodebookError(f"codebook {cb['name']}: the code id {cid!r} is used twice")
        seen.add(cid)
        if not str(c.get("definition") or "").strip():
            raise CodebookError(f"codebook {cb['name']}: code {cid} has no definition")
        out_codes.append({"id": cid, "name": str(c.get("name") or cid), "definition": " ".join(str(c["definition"]).split()),
                          "include": [str(x) for x in c.get("include") or []],
                          "exclude": [str(x) for x in c.get("exclude") or []]})
    attrs, aseen = [], set()
    for a in cb.get("attributes") or []:
        aid = str((a or {}).get("id") or "")
        if not ID.match(aid) or aid in aseen or aid in ("units", "unit", "code"):
            raise CodebookError(f"codebook {cb['name']}: attribute id {aid!r} is missing, repeated or reserved")
        aseen.add(aid)
        attrs.append({"id": aid, "help": str(a.get("help") or ""),
                      **({"choices": [str(x) for x in a["choices"]]} if a.get("choices") else {})})
    return {"name": str(cb["name"]).strip(), "question": str(cb.get("question") or "").strip(),
            "multiple": bool(cb.get("multiple", False)), "codes": out_codes, "attributes": attrs,
            "sha256": sha256_text(text)}


def load(path: Path) -> dict:
    return parse(Path(path).read_text(encoding="utf-8"))


def shipped() -> list[str]:
    return sorted(p.name for p in DIR.glob("*.yaml"))


def describe(cb: dict) -> str:
    """The codebook as a coder reads it: the question, each code with its definition and examples, the fields."""
    lines = [f"Codebook: {cb['name']}"]
    if cb["question"]:
        lines.append(f"Question: {cb['question']}")
    lines.append("Codes:" if len(cb["codes"]) > 1 else "Code:")
    for c in cb["codes"]:
        lines.append(f"- {c['id']} ({c['name']}): {c['definition']}")
        if c["include"]:
            lines.append("  Includes: " + "; ".join(c["include"]))
        if c["exclude"]:
            lines.append("  Not this code: " + "; ".join(c["exclude"]))
    if cb["attributes"]:
        lines.append("For each coded stretch, also give:")
        for a in cb["attributes"]:
            how = f"one of {', '.join(a['choices'])}" if a.get("choices") else a["help"]
            lines.append(f"- {a['id']}: {how}")
    return "\n".join(lines)
