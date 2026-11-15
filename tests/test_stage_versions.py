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
    "audio": ("1", "c461f7a932214bc1e2169d8b314bcafa7f140af92c7a25f0fbad227967ef7be5"),  # typed needs/makes declared; no output changes
    "diarize": ("1", "f592837e668dbd6e683aae1d24e5cde1e3f40d4edb40d40bfc69abb5270776df"),  # typed needs/makes declared; no output changes
    "transcribe": ("1", "3a488ac452336c91e61556c1437ba4786d1ca2c9adfceb51393100c39e3bb3e5"),  # typed needs/makes declared; no output changes
    "nemotron_transcribe": ("1", "541fdb64d37ea6bd1ddfb0e3cd7c4353e1f5aad9d871e22d4c36d07c223037a9"),  # typed needs/makes declared; no output changes
    "align": ("1", "c0b09e8ebed5d156436c8c6b71814887e869ef63bd8585806022ad4c11bc91bd"),  # typed needs/makes declared; no output changes
    "blocks": ("1", "0cec25332abd5f3d6322f65fba4c76c4b7f00a04f9c4e62f5ad57b2f9a4c1245"),  # typed needs/makes declared; no output changes
    "extract_comments": ("1", "000d2d09936a84a9489119157f5c449bb88e25fb833149de09d815a4f1474862"),  # typed needs/makes declared; no output changes
    "stress_set": ("1", "40d809709d443fa9ac676a9e73e3a5723fdb8be392c1517b1c8b6ef2b3bc255d"),  # typed needs/makes declared; no output changes
    "whistress": ("1", "25fe9ee8c9d5a5130b56fb31430abdac32cee5a8f860e0577c5752ee1d103835"),  # typed needs/makes declared; no output changes
    "stress_read": ("1", "2307a41caf891c8fdef64a76ccde9aca65f9bf3c1217094be902442bddcbe5d3"),  # typed needs/makes declared; no output changes
    "word_choices": ("2", "8d8247123bc1746f4b7ef11e1b284d75f97d3cb00205945947ec717ba5fc9b93"),  # typed needs/makes declared; no output changes
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
