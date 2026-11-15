"""The shared formats (artifacts.py) and wiring by type (recipe.parse_stages)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ontherecord import artifacts
from ontherecord.artifacts import ArtifactError
from ontherecord.config import MachineConfig
from ontherecord.recipe import RecipeError, recipe_from_text
from ontherecord.runner import Runner

HERE = Path(__file__).parent
TOYS = ("toy_segments=stages_for_tests:ToySegments;toy_merge=stages_for_tests:ToyMerge;"
        "toy_coder=stages_for_tests:ToyCoder;upper=stages_for_tests:Upper")


@pytest.fixture(autouse=True)
def toys(monkeypatch):
    monkeypatch.setenv("ONTHERECORD_STAGES", TOYS)
    monkeypatch.setenv("PYTHONPATH", str(HERE))
    monkeypatch.syspath_prepend(str(HERE))


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "src.txt").write_text("one two three four five", encoding="utf-8")
    return tmp_path


def recipe(ws, stages):
    return recipe_from_text(f"name: t\nsource:\n  files: [src.txt]\nstages:\n{stages}", ws)


def run(ws, stages):
    return Runner(recipe(ws, stages), MachineConfig(workspace=ws / "workspace"), log=lambda *_: None).execute()


SEG = {"format": "segments/1", "unit": "segment",
       "units": [{"id": "s1", "start": 0, "end": 1.5, "text": "hi", "speaker": "A",
                  "words": [{"start": 0, "end": 0.4, "word": "hi", "p": 0.9}]},
                 {"id": "s2", "start": 2, "end": 3}]}


def test_valid_segments_and_annotations_pass():
    artifacts.check_segments(json.loads(json.dumps(SEG)))
    artifacts.check_annotations({"format": "annotations/1", "codebook": {"name": "x", "sha256": "ab"},
                                 "by": {"kind": "person", "id": "ana"},
                                 "annotations": [{"id": "a1", "target": {"units": ["s1"]}, "code": "c"},
                                                 {"id": "a2", "target": {"start": 1, "end": 2}, "code": "c"},
                                                 {"id": "a3", "target": {"unit": "s1", "word_from": 0, "word_to": 0},
                                                  "code": "c", "attributes": {}, "note": ""}]})


@pytest.mark.parametrize("change, msg", [
    (lambda d: d["units"][0].pop("id"), "id is missing"),
    (lambda d: d["units"][1].update(id="s1"), "used twice"),
    (lambda d: d["units"][0].update(end=-1), "before its start"),
    (lambda d: d["units"][0]["words"][0].update(end=-0.1), "before its start"),
    (lambda d: d.update(unit="paragraph"), "unit must be"),
    (lambda d: d.update(format="segments/0"), "format"),
])
def test_malformed_segments_are_rejected_with_the_field_named(change, msg):
    d = json.loads(json.dumps(SEG))
    change(d)
    with pytest.raises(ArtifactError, match=msg):
        artifacts.check_segments(d)


def test_malformed_annotations_are_rejected():
    base = {"format": "annotations/1", "codebook": None, "by": {"kind": "model", "id": "m"}, "annotations": []}
    for ann, msg in [({"id": "a", "code": "c", "target": {}}, "target must"),
                     ({"id": "a", "code": "", "target": {"units": ["s"]}}, "code is missing"),
                     ({"id": "a", "code": "c", "target": {"unit": "s", "word_from": 3, "word_to": 1}}, "word_from")]:
        with pytest.raises(ArtifactError, match=msg):
            artifacts.check_annotations({**base, "annotations": [ann]})
    with pytest.raises(ArtifactError, match="by must"):
        artifacts.check_annotations({**base, "by": {"kind": "robot", "id": "m"}})


def test_inputs_are_wired_by_type_and_opaque_ones_by_name(ws):
    r = recipe(ws, "  - download: {}\n  - toy_segments: {}\n  - toy_merge: {}\n  - toy_coder: {}\n")
    merge, coder = r.stages[2], r.stages[3]
    assert (merge.inputs_from, merge.input_artifacts) == ({"units": "toy_segments"}, {"units": "lines"})
    assert (coder.inputs_from, coder.input_artifacts) == ({"units": "toy_merge"}, {"units": "merged"})  # the latest
    r = recipe(ws, "  - download: {}\n  - toy_segments: {}\n  - toy_merge: {}\n  - toy_coder: {}\n"
                   "    from: {units: toy_segments.lines}\n")
    assert r.stages[3].inputs_from == {"units": "toy_segments"}
    with pytest.raises(RecipeError, match="makes notes as segments"):
        recipe(ws, "  - download: {}\n  - toy_segments: {}\n  - toy_coder: {}\n    from: {units: toy_segments.notes}\n")
    with pytest.raises(RecipeError, match=r"needs units \(segments\)"):
        recipe(ws, "  - download: {}\n  - toy_coder: {}\n")


def test_a_chain_of_one_type_is_not_a_mix_but_two_sources_are(ws):
    r = recipe(ws, "  - download: {}\n  - toy_segments: {}\n  - toy_merge: {}\n  - toy_coder: {}\n")
    assert all(not s.mixed for s in r.stages)
    rec = run(ws, "  - download: {}\n  - toy_segments: {}\n  - toy_merge: {}\n  - toy_coder: {}\n")
    assert {s: c["status"] for s, c in rec["cells"]["src"].items()} == {
        "download": "ran", "toy_segments": "ran", "toy_merge": "ran", "toy_coder": "ran"}
    assert rec["stages"][3]["makes"] == {"codes": "annotations"}          # the run record carries the types
    assert rec["stages"][3]["from_artifact"] == {"units": "merged"}
    receipt = json.loads((ws / "workspace" / rec["cells"]["src"]["toy_coder"]["receipt"]).read_text())
    assert receipt["inputs"]["units"]["type"] == "segments"


def test_the_worker_refuses_a_malformed_output(ws):
    for broken, msg in [("dup", "used twice"), ("noid", "id is missing"), ("backwards", "before its start")]:
        rec = run(ws, f"  - download: {{}}\n  - toy_segments: {{broken: {broken}}}\n")
        cell = rec["cells"]["src"]["toy_segments"]
        assert cell["status"] == "failed", broken
        assert "lines (segments) is malformed" in cell["error"] and msg in cell["error"]


def test_every_builtin_stage_declares_typed_needs_and_makes():
    from ontherecord.recipe import check_declaration
    from ontherecord.stages import _BUILTIN, get
    for name in _BUILTIN:
        check_declaration(get(name))
