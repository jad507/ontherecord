"""Codebooks, turns and the code stage, end to end through the runner, against a fake model server."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ontherecord import codebooks
from ontherecord.config import MachineConfig
from ontherecord.recipe import recipe_from_text
from ontherecord.runner import Runner
from ontherecord.stages.turns import merge

HERE = Path(__file__).parent
TOY_BOOK = """name: toy
question: Is this a greeting?
codes:
  - {id: greeting, name: Greeting, definition: Someone says hello., include: [hi, hello], exclude: [goodbye]}
  - {id: other_talk, name: Other talk, definition: Anything else said.}
attributes:
  - {id: tone, choices: [warm, flat]}
  - {id: summary, help: a few words}
"""


class FakeCoder(BaseHTTPRequestHandler):
    """Codes a unit 'greeting' when its text holds 'hello'. Answers for the ids it was shown (find: every unit in
    brackets; classify: the marked one, or all in a whole recording), plus one made-up id to exercise the check."""
    calls: list = []

    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"data": [{"id": "fake"}]} if self.path.endswith("/models")
                   else {"default_generation_settings": {"n_ctx": 8192}})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeCoder.calls.append(req)
        user = req["messages"][1]["content"]
        lines = re.findall(r"^(>> |   )?\[(\w+)\][^\n]*?-\d+:\d\d:\d\d: (.*)$", user, re.M)
        marked = [i for m, i, _ in lines if m == ">> "] or [i for _, i, _ in lines]
        text = {i: t for _, i, t in lines}
        props = req["response_format"]["json_schema"]["schema"]["properties"]
        if "found" in props:
            out = {"found": [{"units": [i], "code": "greeting", "tone": "warm", "summary": "hi"}
                             for i in marked if "hello" in text[i]] + [{"units": ["zz99"], "code": "greeting",
                                                                        "tone": "flat", "summary": ""}]}
        elif props["coded"]["items"]["properties"]["code"]["type"] == "array":   # a codebook with multiple: true
            out = {"coded": [{"unit": i, "code": ["greeting", "other_talk"] if "again" in text[i] else ["greeting"],
                              "tone": "flat", "summary": ""} for i in marked]}
        else:
            out = {"coded": [{"unit": i, "code": "greeting" if "hello" in text[i] else "none", "tone": "flat",
                              "summary": ""} for i in marked]}
        self._send({"choices": [{"message": {"content": json.dumps(out)}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoder)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeCoder.calls = []
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("ONTHERECORD_STAGES", "toy_segments=stages_for_tests:ToySegments")
    monkeypatch.setenv("PYTHONPATH", str(HERE))
    monkeypatch.syspath_prepend(str(HERE))
    # speakers A A B B A A: turns t0000 (hello there), t0001 (fine thanks), t0002 (hello again)
    (tmp_path / "src.txt").write_text("hello there fine thanks hello again", encoding="utf-8")
    (tmp_path / "toy.yaml").write_text(TOY_BOOK, encoding="utf-8")
    return tmp_path


def run(ws, code_settings: str):
    text = ("name: t\nsource:\n  files: [src.txt]\nstages:\n  - download: {}\n  - toy_segments: {}\n"
            "  - turns: {}\n  - code: " + code_settings + "\n")
    r = Runner(recipe_from_text(text, ws), MachineConfig(workspace=ws / "workspace"), log=lambda *_: None)
    return r, r.execute()


def output(ws, rec, step, name):
    rel = rec["cells"]["src"][step]["receipt"]
    receipt = json.loads((ws / "workspace" / rel).read_text())
    return (ws / "workspace" / rel).parent / receipt["outputs"][name]["file"]


def test_codebooks_are_checked_on_load():
    cb = codebooks.parse(TOY_BOOK)
    assert [c["id"] for c in cb["codes"]] == ["greeting", "other_talk"] and cb["sha256"]
    for bad, msg in [("name: x\ncodes: []\n", "at least one code"),
                     ("name: x\ncodes:\n  - {id: a, definition: d}\n  - {id: a, definition: e}\n", "used twice"),
                     ("name: x\ncodes:\n  - {id: a}\n", "no definition"),
                     ("codes:\n  - {id: a, definition: d}\n", "needs a name"),
                     ("name: x\ncodes:\n  - {id: none, definition: d}\n", "code id")]:
        with pytest.raises(codebooks.CodebookError, match=msg):
            codebooks.parse(bad)
    assert "public-comment.yaml" in codebooks.shipped()
    pc = codebooks.load(codebooks.DIR / "public-comment.yaml")
    assert [a["id"] for a in pc["attributes"]] == ["speaker_name", "residence_or_affiliation", "agenda_item",
                                                   "position", "summary", "boundary_note"]


def test_turns_merge_and_cut_for_length():
    u = lambda i, a, b, spk: {"id": f"s{i}", "start": a, "end": b, "text": f"w{i}", "speaker": spk}  # noqa: E731
    units = [u(0, 0, 10, "A"), u(1, 12, 20, "A"), u(2, 40, 50, "A"), u(3, 51, 55, "B"), u(4, 56, 70, "B"),
             u(5, 71, 90, "B")]
    t = merge(units, 5.0, None)
    assert [x["from"] for x in t] == [["s0", "s1"], ["s2"], ["s3", "s4", "s5"]]
    assert t[0]["text"] == "w0 w1" and "part" not in t[2]
    t = merge(units, 5.0, 20.0)   # B's turn would be 39 s: cut at a segment boundary into numbered parts
    assert [x["from"] for x in t] == [["s0", "s1"], ["s2"], ["s3", "s4"], ["s5"]]
    assert t[2]["part"] == [1, 2] and t[3]["part"] == [2, 2]
    assert len(merge(units, 30.0, None)) == 2


def test_turns_then_code_find_mode(ws, server):
    r, rec = run(ws, f"{{codebook: toy.yaml, max_tokens: 2000, endpoint: {json.dumps(server)}}}")
    assert {s: c["status"] for s, c in rec["cells"]["src"].items()} == {
        "download": "ran", "toy_segments": "ran", "turns": "ran", "code": "ran"}
    turns = json.loads(output(ws, rec, "turns", "turns").read_text())
    assert [(u["id"], u["text"], u["from"]) for u in turns["units"]] == [
        ("t0000", "hello there", ["s0000", "s0001"]), ("t0001", "fine thanks", ["s0002", "s0003"]),
        ("t0002", "hello again", ["s0004", "s0005"])]
    ann = json.loads(output(ws, rec, "code", "annotations").read_text())
    assert ann["codebook"]["name"] == "toy" and ann["by"] == {"kind": "model", "id": "fake"}
    assert [(a["target"]["units"], a["code"], a["attributes"]["tone"]) for a in ann["annotations"]] == [
        (["t0000"], "greeting", "warm"), (["t0002"], "greeting", "warm")]
    receipt = json.loads((ws / "workspace" / rec["cells"]["src"]["code"]["receipt"]).read_text())
    checks = {c["name"]: c["status"] for c in receipt["checks"]}
    assert checks == {"parsed": "ok", "unknown_codes": "ok", "unknown_ids": "warn", "windows": "ok"}
    assert receipt["settings"]["codebook"]["text"] == TOY_BOOK and receipt["settings"]["lens"] is None
    system = FakeCoder.calls[0]["messages"][0]["content"]
    assert "greeting (Greeting): Someone says hello." in system and "Not this code: goodbye" in system
    csv_text = output(ws, rec, "code", "annotations_csv").read_text()
    assert csv_text.splitlines()[0] == "id,code,units,start,end,speakers,tone,summary,text"
    # a second submit plans every step cached
    again = Runner(recipe_from_text(r.recipe.text, ws), MachineConfig(workspace=ws / "workspace"), log=lambda *_: None)
    assert {s: c["status"] for s, c in again.plan()["src"].items()} == {
        "download": "cached", "toy_segments": "cached", "turns": "cached", "code": "cached"}


def test_code_classify_one_unit_at_a_time_with_a_lens(ws, server):
    (ws / "lens.txt").write_text("Attend to warmth.", encoding="utf-8")
    _, rec = run(ws, f"{{codebook: toy.yaml, lens: lens.txt, mode: classify, context: window, window_size: 1, "
                     f"max_tokens: 2000, endpoint: {json.dumps(server)}}}")
    ann = json.loads(output(ws, rec, "code", "annotations").read_text())
    assert [a["target"]["units"] for a in ann["annotations"]] == [["t0000"], ["t0002"]]
    assert len(FakeCoder.calls) == 3   # one call per unit
    first = FakeCoder.calls[0]["messages"]
    assert first[0]["content"].startswith("Read the material through this lens:\nAttend to warmth.")
    assert ">> [t0000]" in first[1]["content"] and "   [t0001]" in first[1]["content"]
    receipt = json.loads((ws / "workspace" / rec["cells"]["src"]["code"]["receipt"]).read_text())
    assert {c["name"]: c["status"] for c in receipt["checks"]}["coverage"] == "ok"


def test_turns_refuse_segments_without_speakers(ws):
    text = ("name: t\nsource:\n  files: [src.txt]\nstages:\n  - download: {}\n  - toy_segments: {speakers: false}\n"
            "  - turns: {}\n")
    rec = Runner(recipe_from_text(text, ws), MachineConfig(workspace=ws / "workspace"), log=lambda *_: None).execute()
    cell = rec["cells"]["src"]["turns"]
    assert cell["status"] == "failed" and "have no speaker" in cell["error"] and "Put align" in cell["error"]


def test_an_endpoint_off_this_machine_is_refused_unless_allowed(ws):
    """D5: a recipe naming a remote server is refused at plan time; allowed, it is flagged in the plan."""
    from ontherecord.recipe import RecipeError
    text = ("name: t\nsource:\n  files: [src.txt]\nstages:\n  - download: {}\n  - toy_segments: {}\n  - turns: {}\n"
            "  - code: {codebook: toy.yaml, endpoint: 'https://llm.example.org/v1'}\n")
    with pytest.raises(RecipeError, match=r"llm.example.org, off this machine.*allow_remote_endpoints"):
        Runner(recipe_from_text(text, ws), MachineConfig(workspace=ws / "workspace"), log=lambda *_: None)
    cfg = MachineConfig(workspace=ws / "workspace", allow_remote_endpoints=["llm.example.org"])
    plan = Runner(recipe_from_text(text, ws), cfg, log=lambda *_: None).plan()
    assert plan["src"]["code"]["leaves_machine"] == "llm.example.org"
    from ontherecord.llm import leaves_machine_check, remote_host
    assert remote_host("http://127.0.0.1:8081/v1") is None and remote_host("managed") is None
    assert leaves_machine_check("llm.example.org")[0].status == "warn"


def test_an_annotation_names_a_local_model_by_its_file_not_its_path():
    from ontherecord.stages.code import model_id
    assert model_id("/home/someone/models/gemma.gguf") == "gemma.gguf"
    assert model_id("google/gemma-4") == "google/gemma-4" and model_id("fake") == "fake"


def two_pass(ws, server, codes="greeting"):
    (ws / "multi.yaml").write_text(TOY_BOOK.replace("name: toy", "name: multi\nmultiple: true"), encoding="utf-8")
    end = json.dumps(server)
    text = ("name: t\nsource:\n  files: [src.txt]\nstages:\n  - download: {}\n  - toy_segments: {}\n  - turns: {}\n"
            f"  - code: {{codebook: toy.yaml, max_tokens: 2000, endpoint: {end}}}\n"
            f"  - excerpts: {{codes: '{codes}'}}\n"
            f"  - code: {{codebook: multi.yaml, mode: classify, context: unit, max_tokens: 2000, endpoint: {end}}}\n"
            "    as: second\n")
    r = Runner(recipe_from_text(text, ws), MachineConfig(workspace=ws / "workspace"), log=lambda *_: None)
    return r, r.execute()


def test_a_second_pass_codes_only_what_the_first_found(ws, server):
    """Find greetings among the turns, cut them out, and classify only those: the two-pass study."""
    r, rec = two_pass(ws, server)
    assert {s: c["status"] for s, c in rec["cells"]["src"].items()} == {
        "download": "ran", "toy_segments": "ran", "turns": "ran", "code": "ran", "excerpts": "ran", "second": "ran"}
    ex = json.loads(output(ws, rec, "excerpts", "excerpts").read_text())
    assert ex["unit"] == "clip"
    assert [(u["id"], u["text"], u["from"], u["speaker"]) for u in ex["units"]] == [
        ("a0001", "hello there", ["t0000"], "A"), ("a0002", "hello again", ["t0002"], "A")]
    second_calls = FakeCoder.calls[1:]
    assert len(second_calls) == 2 and all("fine thanks" not in c["messages"][1]["content"] for c in second_calls)
    # the reply format names the only ids the model may answer with: the ones that call showed
    enum = lambda c, key: c["response_format"]["json_schema"]["schema"]["properties"][key]["items"]  # noqa: E731
    assert [enum(c, "coded")["properties"]["unit"]["enum"] for c in second_calls] == [["a0001"], ["a0002"]]
    assert enum(FakeCoder.calls[0], "found")["properties"]["units"]["items"]["enum"] == ["t0000", "t0001", "t0002"]
    ann = json.loads(output(ws, rec, "second", "annotations").read_text())
    assert [(a["target"]["units"], a["code"]) for a in ann["annotations"]] == [
        (["a0001"], "greeting"), (["a0002"], "greeting"), (["a0002"], "other_talk")]   # several codes on one
    # readers: the transcript is still the whole recording, and the second pass's codes lie over the excerpts
    from ontherecord import library
    root = ws / "workspace"
    run = json.loads((root / "runs" / rec["run_id"] / "run.json").read_text())
    assert library.best_transcript(root, run, "src")[0] != "excerpts"
    rows = library.coded(root, run, "src", "second", "annotations")["annotations"]
    assert [x["text"] for x in rows] == ["hello there", "hello again", "hello again"]
    again = Runner(recipe_from_text(r.recipe.text, ws), MachineConfig(workspace=ws / "workspace"), log=lambda *_: None)
    assert {c["status"] for c in again.plan()["src"].values()} == {"cached"}


def test_excerpts_of_a_code_nobody_used_leave_the_second_pass_nothing(ws, server):
    _, rec = two_pass(ws, server, codes="other_talk")
    cells = rec["cells"]["src"]
    assert cells["excerpts"]["status"] == "ran" and cells["second"]["status"] == "ran"
    assert json.loads(output(ws, rec, "excerpts", "excerpts").read_text())["units"] == []
    assert len(FakeCoder.calls) == 1   # the first pass only
    receipt = json.loads((ws / "workspace" / cells["excerpts"]["receipt"]).read_text())
    assert {c["name"]: c["status"] for c in receipt["checks"]}["codes"] == "warn"


def test_excerpts_refuse_codes_on_units_they_were_not_given():
    from ontherecord.stages.base import UserError
    from ontherecord.stages.excerpts import cut
    units = [{"id": "s0", "start": 0.0, "end": 1.0, "text": "a", "speaker": "A"},
             {"id": "s1", "start": 1.0, "end": 2.0, "text": "b", "speaker": "B"}]
    two = cut(units, [{"id": "a1", "code": "x", "target": {"units": ["s0", "s1"]}}], set())
    assert "speaker" not in two[0] and two[0]["text"] == "a b"   # two speakers: no single speaker named
    with pytest.raises(UserError, match="not in the segments"):
        cut(units, [{"id": "a1", "code": "x", "target": {"units": ["t0009"]}}], set())
    with pytest.raises(UserError, match="span of time"):
        cut(units, [{"id": "a1", "code": "x", "target": {"start": 0.0, "end": 1.0}}], set())


def test_the_shipped_example_recipe_plans_two_passes(tmp_path):
    """recipes/example-interviews.yaml: pass two reads the excerpts, and the excerpts read the turns pass one coded."""
    import shutil
    shutil.copy(HERE.parent / "recipes" / "example-interviews.yaml", tmp_path / "r.yaml")
    (tmp_path / "interviews").mkdir()
    (tmp_path / "interviews" / "one.wav").write_bytes(b"RIFF")
    from ontherecord.recipe import load_recipe
    r = Runner(load_recipe(tmp_path / "r.yaml"), MachineConfig(workspace=tmp_path / "ws"), log=lambda *_: None)
    wiring = {s.name: s.inputs_from for s in r.recipe.stages}
    assert wiring["excerpts"] == {"units": "turns", "annotations": "code"}
    assert wiring["themes"] == {"units": "excerpts"}
    assert set(r.plan()["one"]) >= {"turns", "code", "excerpts", "themes"}
