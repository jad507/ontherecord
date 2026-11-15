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
    "download": ("1", "2bcaefac55809c4876839ca5e79966b93daa018501f878e290a26a0e8faceb15"),  # typed needs/makes declared; no output changes
    "audio": ("1", "11298036ad1d6a8a103ea03b6470a9542ad6c47c0f57082474798d7b30b99a7a"),  # headline declared for Compare; no output changes
    "diarize": ("2", "62be5302d1ef4a1c71a592e9656da406ef93cc9ead0932587c720d00e79b7a88"),  # formats branch: writes the shared formats (version raised)
    "transcribe": ("2", "1af689eb6f53a0462f06f95cb8c80f92a81739273ca1f8010888de70ad2ec0a1"),  # formats branch: writes the shared formats (version raised)
    "nemotron_transcribe": ("2", "bbe590f9fa0089f40470c058560379154d7365e25f78fcc4276f7a13e5927445"),  # formats branch (version raised); D5 leaves_machine check only adds to the receipt
    "align": ("2", "3e3d7bfe8a60e524628558e07536031784ca2c3dcc7aabda88797d3c8e012ff2"),  # formats branch: writes the shared formats (version raised)
    "stress_set": ("1", "74f57266f21033e0f37e275f911286be8a4a3d1e449284d1e4ede3eb12271961"),  # headline declared for Compare; no output changes
    "whistress": ("1", "05e53964a2bb0fddb2575cd71e4d7d7b3efe1a2f1a66ec74c6d8c55229995db5"),  # headline declared for Compare; no output changes
    "stress_read": ("1", "58890068ca9470b89c3e04c71329ba81d441cde483dbeaa89265741bfbb91cd3"),  # headline; open_model's context argument; D5 leaves_machine check (receipt only): no output changes
    "word_choices": ("3", "42cf5b6101508fb15d23478f4ae7be4a551dfb56f6b9040def24358590f3feb1"),  # formats branch: writes the shared formats (version raised)
    "turns": ("1", "102334b9f90b3a6eabb25b465580fc5694e1d28f06b8862db559048bbc4626ba"),  # formats branch: writes the shared formats (version raised)
    "code": ("1", "3627880ed9f178e417b1908b8ef28387ef80bb0f2d6e08625295769828590f2c"),  # formats branch (version raised); D5 leaves_machine check only adds to the receipt
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
