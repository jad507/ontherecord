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
