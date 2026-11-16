"""The library: what can be played, read and searched for each recording, read straight from runs and receipts."""
from __future__ import annotations

from pathlib import Path

import pytest

from ontherecord.config import MachineConfig
from ontherecord.library import Library
from ontherecord.recipe import recipe_from_text
from ontherecord.runner import Runner
from ontherecord.store import Store

HERE = Path(__file__).parent


@pytest.fixture(autouse=True)
def fake_stages(monkeypatch):
    monkeypatch.setenv("ONTHERECORD_STAGES", "audio=stages_for_tests:FakeAudio;"
                                             "transcribe=stages_for_tests:FakeTranscribe")
    monkeypatch.setenv("PYTHONPATH", str(HERE))
    monkeypatch.syspath_prepend(str(HERE))


def run(ws: Path, transcribe: str = "{}") -> str:
    text = ("name: lib\nsource:\n  files: ['Council Meeting - October 28, 2025 [abc123XYZ].txt']\nstages:\n"
            f"  - download: {{}}\n  - audio: {{}}\n  - transcribe: {transcribe}\n")
    return Runner(recipe_from_text(text, ws), MachineConfig(workspace=ws / "w"), log=lambda *_: None).execute()["run_id"]


def test_recordings_view_and_search(tmp_path):
    (tmp_path / "Council Meeting - October 28, 2025 [abc123XYZ].txt").write_text(
        "call to order\nI am here for public comment on the data center\nadjourned", encoding="utf-8")
    good = run(tmp_path)
    deaf = run(tmp_path, "{deaf: true}")  # newer, but it heard nothing
    lib = Library(Store(tmp_path / "w"))
    [rec] = lib.recordings()
    assert rec["id"] == "abc123XYZ" and rec["date"] is None   # never guessed from a title (D8)
    assert {v["run_id"]: v["words"] for v in rec["views"]} == {good: 14, deaf: 0}
    assert rec["default_run"] == good  # the view with words in it, not merely the newest

    view = lib.recording(good, "abc123XYZ")
    assert view["text_from"] == "transcribe" and view["segments"][0]["id"] == "s0000" and len(view["segments"]) == 3
    assert view["audio"]["settings"]["channel"] == "left"
    assert view["segments"][1]["start"] == 10.0

    hits = lib.search("PUBLIC comment")
    assert [(h["id"], h["start"]) for h in hits] == [("abc123XYZ", 10.0)]
    assert lib.search("nothing like this") == []
    with pytest.raises(KeyError):
        lib.recording(good, "no-such-item")


def test_compare_two_runs(tmp_path):
    from ontherecord.library import compare
    (tmp_path / "Council Meeting - October 28, 2025 [abc123XYZ].txt").write_text("one two three\nfour five", "utf-8")
    deaf = run(tmp_path, "{deaf: true}")
    good = run(tmp_path)
    c = compare(Store(tmp_path / "w"), deaf, good)
    assert [col["name"] for col in c["columns"]] == ["download", "audio", "transcribe"]
    row = c["rows"]["abc123XYZ"]
    assert row["download"]["same"] and row["audio"]["same"]
    t = row["transcribe"]
    assert t["same"] is False and t["settings_differ"] == {"deaf": [True, False]} and t["inputs_differ"] == []
    assert (t["a"]["value"], t["b"]["value"], t["unit"]) == (0, 5, "words")


def test_any_stage_making_segments_and_annotations_shows_in_the_library(tmp_path, monkeypatch):
    """Audit A2: a new transcriber or coder appears in the Library with no edit outside its own file."""
    monkeypatch.setenv("ONTHERECORD_STAGES", "toy_segments=stages_for_tests:ToySegments;"
                                             "toy_merge=stages_for_tests:ToyMerge;toy_coder=stages_for_tests:ToyCoder")
    (tmp_path / "talk.txt").write_text("hello there general kenobi", encoding="utf-8")
    text = ("name: toys\nsource:\n  files: [talk.txt]\nstages:\n  - download: {}\n  - toy_segments: {}\n"
            "  - toy_merge: {}\n  - toy_coder: {}\n")
    run_id = Runner(recipe_from_text(text, tmp_path), MachineConfig(workspace=tmp_path / "w"),
                    log=lambda *_: None).execute()["run_id"]
    lib = Library(Store(tmp_path / "w"))
    [rec] = lib.recordings()
    assert rec["views"][0]["has"] == ["transcript", "speakers", "codes"] and rec["views"][0]["words"] == 4
    view = lib.recording(run_id, "talk")
    assert view["text_from"] == "toy_segments"          # segment-sized units over the merged ones
    assert [s["speaker"] for s in view["segments"]] == ["A", "A", "B", "B"]
    [codes] = view["codes"]
    assert codes["step"] == "toy_coder" and codes["by"] == {"kind": "model", "id": "toy"}
    assert [(a["code"], a["units"], a["text"], a["start"]) for a in codes["annotations"]] == [
        ("long", ["t0000"], "hello there", 0.0), ("long", ["t0001"], "general kenobi", 20.0)]


def test_no_reader_names_a_stage():
    """Audit A2: outside stages/, presets.py and bootstrap.py, no source file holds a stage name as a string.
    Names that are also artifact types ("audio") or plain words the program uses for other things are allowed
    where listed, with the reason."""
    import ast
    from ontherecord.stages import _BUILTIN
    from ontherecord.stages.base import TYPES
    pkg = Path(__file__).resolve().parents[1] / "src" / "ontherecord"
    everywhere = {"code"}   # also the annotations format's field name
    allowed = {
        "transcribe": {"tools.py", "assistant.py", "mcp_server.py", "server.py", "cli.py"},  # the tool and command
        "turns": {"reviews.py", "cli.py"},           # how many turns a check set's answers covered
        "download": {"jobs.py", "server.py"},        # the model-download task's kind
        "whistress": {"model_hashes.py"},            # the model's catalog name
    }
    found = []
    for f in sorted(pkg.rglob("*.py")):
        rel = f.relative_to(pkg).as_posix()
        if rel.startswith("stages/") or rel in ("presets.py", "bootstrap.py"):
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in _BUILTIN \
                    and node.value not in TYPES and node.value not in everywhere \
                    and f.name not in allowed.get(node.value, set()):
                found.append(f"{rel}:{node.lineno} {node.value!r}")
    js = (pkg / "static" / "app.js").read_text(encoding="utf-8") + (pkg / "static" / "check.js").read_text("utf-8")
    import re
    for name in _BUILTIN:
        if name in TYPES or name in ("transcribe", "code"):
            continue
        found += [f"static: {m.group(0)!r}" for m in re.finditer(rf"[\"'`]{name}[\"'`]", js)]
    assert not found, "stage names in readers:\n" + "\n".join(found)
