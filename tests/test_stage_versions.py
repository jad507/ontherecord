"""A stage's code cannot change without its version (audit D6, K2).

A stage's version goes into every cache key it makes. If its code changes and its version does not, the store
hands outputs made by the old code to anyone who asks, and nothing records it. This test holds, for every built-in
stage, its version and the sha256 of its source: the stage's module plus every module of this package it imports,
followed transitively (function-level imports included), minus `NOT_OUTPUT` below.

When this test fails for a stage you changed, do one of two things:

- raise the stage's `version`, then update its row; or
- if no output of the stage can change (a comment, a help string, a metric, a refactor), update only the hash and
  write why on the row's comment.

`python tests/test_stage_versions.py` prints the current table to paste.
"""
from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "src" / "ontherecord"

# Modules a stage may import that cannot change what it writes: the stage declarations themselves, environment
# reports, hashing, file lookups for the cache, and token/time metrics. Everything else a stage imports counts.
NOT_OUTPUT = {"stages/base.py", "envinfo.py", "hashing.py", "bootstrap.py", "config.py", "usage.py"}

# stage: (version, sha256 of its sources). Keep a comment on a row whose hash moved without a version bump.
EXPECTED = {
    "download": ("1", "80ba96de29df746f7df731450256ffb0dab96cce79dd291b929d89e269fd9464"),
    "audio": ("1", "fb3557681a3224e9dbd59a0b0e27db83d648e87ee520aaa84b27b5136200040f"),
    "diarize": ("1", "17aa3d32ae86888523134c4bd16d37a2bdc61264f9d8199f28b5129285aa6b63"),
    "transcribe": ("1", "1b6f25db94f61385d96a57edd7e2cdd101ccb0938c6ac3bd182721f538cafab4"),
    "nemotron_transcribe": ("1", "57ebf1c4214b0798d21142a84fe6cbffa8996cc3a62c01cbb941b4b22b241fe8"),
    "align": ("1", "89a5c12261940aa67485317c3e65889c60e50f4226779c72eaad96799b0cb224"),
    "blocks": ("1", "f04e293756c6f8c71a02030fa17909b16f7c907ded99e9fc644fe9cde69403f3"),
    "extract_comments": ("1", "a3b5cfcc2a14e85eda7024730898c3404dad06fbe1e5d1a8cb9b3008cf3e435c"),
    "stress_set": ("1", "574bb01f6265860a357c4c25461dc8b4902ab60b4f4823ccd420e1354a78b022"),
    "whistress": ("1", "5174fb585c1d87952a4f6cabbf03ef8961b9635b0830bc9cb884a0d1974235d4"),
    "stress_read": ("1", "f9a757b04f261418f70d235c8db05815da8d2b21292af2a66c32a1c101f866e5"),
    "word_choices": ("2", "68c732c8354af766175ce3ff6ee5916d0c01cebf8757cf89c4d775db1029f233"),
}


def _module_file(name: str) -> Path | None:
    p = PKG.joinpath(*name.split("."))
    if p.with_suffix(".py").exists():
        return p.with_suffix(".py")
    if (p / "__init__.py").exists():
        return p / "__init__.py"
    return None


def _imports(path: Path) -> set[Path]:
    """Files of this package that `path` imports, at any depth in the file (relative and absolute imports)."""
    here = path.relative_to(PKG).parent.parts
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        targets = []
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(here[: len(here) - (node.level - 1)]) if node.level > 1 else list(here)
            elif node.module and node.module.split(".")[0] == "ontherecord":
                base = []
            else:
                continue
            mod = (node.module or "").split(".") if node.module else []
            if not node.level:
                mod = mod[1:]
            targets.append(".".join(base + mod))
            targets += [".".join(base + mod + [a.name]) for a in node.names]   # `from . import _whistress`
        elif isinstance(node, ast.Import):
            targets += [a.name.split(".", 1)[1] for a in node.names if a.name.startswith("ontherecord.")]
        for t in targets:
            f = _module_file(t) if t else None
            if f:
                found.add(f)
    return found


def sources(module_file: Path) -> list[Path]:
    seen, todo = set(), [module_file]
    while todo:
        f = todo.pop()
        rel = f.relative_to(PKG).as_posix()
        if f in seen or (rel in NOT_OUTPUT and f != module_file) or rel.endswith("__init__.py"):
            continue
        seen.add(f)
        todo += _imports(f)
    return sorted(seen)


def source_hash(stage_cls) -> str:
    module_file = Path(sys.modules[stage_cls.__module__].__file__).resolve()
    h = hashlib.sha256()
    for f in sources(module_file):
        h.update(f.relative_to(PKG).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def current() -> dict[str, tuple[str, str]]:
    from ontherecord.stages import _BUILTIN, get
    return {name: (str(get(name).version), source_hash(get(name))) for name in _BUILTIN}


def test_every_builtin_stage_has_a_row():
    from ontherecord.stages import _BUILTIN
    assert sorted(EXPECTED) == sorted(_BUILTIN), "add or remove rows in EXPECTED (run this file to print them)"


def test_stage_code_does_not_change_without_its_version():
    now = current()
    wrong = []
    for name, (version, digest) in EXPECTED.items():
        if name not in now:
            continue
        v, d = now[name]
        if v != version:
            wrong.append(f"{name}: version is {v}, the table says {version}: update the row")
        elif d != digest:
            wrong.append(f"{name}: its code changed but its version ({v}) did not. Raise the version, or, if no "
                         f"output can change, update the hash with a comment saying why. New hash: {d}")
    assert not wrong, "\n".join(wrong)


def test_sources_follow_imports():
    """The hash covers what a stage imports: llm.py for the model stages, transcribe.py for word_choices."""
    from ontherecord.stages import get
    def rel(name):
        return {f.relative_to(PKG).as_posix() for f in sources(Path(sys.modules[get(name).__module__].__file__))}
    assert "llm.py" in rel("stress_read")
    assert "stages/transcribe.py" in rel("word_choices")
    assert "stages/diarize.py" in rel("word_choices")      # through transcribe's hf_snapshot
    assert "stages/base.py" not in rel("align")


if __name__ == "__main__":
    sys.path.insert(0, str(PKG.parent))
    for name, (v, d) in current().items():
        print(f'    "{name}": ("{v}", "{d}"),')
