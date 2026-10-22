"""The stress test stages, with no model: a two-item local set, and a fake OpenAI-compatible server as the reader."""
from __future__ import annotations

import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ontherecord.recipe import item_for_url, load_items
from ontherecord.stages.base import Context, Item
from ontherecord.stages.stress_read import StressRead, capitals, parse, quoted
from ontherecord.stages.stress_set import StressSet, read_set
from ontherecord.stages.whistress import align, score


def write_wav(path: Path, frames: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * frames)


def make_set(tmp_path: Path) -> Path:
    clips = tmp_path / "clips"
    clips.mkdir()
    write_wav(clips / "a.wav", 1600)
    items = [
        {"id": "w1", "path": "clips/a.wav", "transcription": "i didn't say he stole it",
         "answers": ["Someone else said it.", "I never said that."], "label": 0,
         "stress_binary": [1, 0, 0, 0, 0, 0], "intonation": "*i* didn't say he stole it", "blind": True},
        # the same recording asked again, against its other meaning
        {"id": "w2", "path": "clips/a.wav", "transcription": "i didn't say he stole it",
         "answers": ["He took it, not someone else.", "Someone else said it."], "label": 1,
         "stress_binary": [1, 0, 0, 0, 0, 0], "intonation": "*i* didn't say he stole it", "blind": False},
    ]
    sub = tmp_path / "wild" / "set"
    sub.mkdir(parents=True)
    (sub / "items.json").write_text(json.dumps(items))
    return sub / "items.json"


def test_local_set_is_one_hashable_file(tmp_path):
    src = make_set(tmp_path)
    stage = StressSet()
    for k in ("one", "two"):
        (tmp_path / k).mkdir()
        res = stage.run(Item(id="WildStressTest", path=str(src)), {}, tmp_path / k,
                        {"revision": "pinned", "split": "test"}, None, Context(config=None, scratch=tmp_path))
    assert (tmp_path / "one/set.zip").read_bytes() == (tmp_path / "two/set.zip").read_bytes()
    manifest, z = read_set(tmp_path / "one/set.zip")
    assert res.metrics["items"] == 2 and res.metrics["recordings"] == 1 and res.metrics["blind_items"] == 1
    assert manifest["items"][0]["audio"] == manifest["items"][1]["audio"]  # stored once
    assert z.read(manifest["items"][0]["audio"])[:4] == b"RIFF"
    assert [c.status for c in res.checks] == ["ok"]


def test_set_sources_name_their_items(tmp_path):
    assert item_for_url("https://huggingface.co/datasets/slprl/StressTest").id == "StressTest"
    src = make_set(tmp_path)
    [it] = load_items({"files": [{"path": str(src), "id": "WildStressTest"}]}, tmp_path)
    assert it.id == "WildStressTest" and it.path == str(src.resolve())


def test_marks_line_up_with_the_true_words():
    # a mishearing keeps its word's label; an inserted word is dropped
    labels, matched = align("i didn't say he stole the money".split(),
                            [("I", 0), ("didn't", 0), ("say", 0), ("HE", 1), ("stole", 0), ("that", 0), ("um", 0),
                             ("money", 0)])
    assert labels == [0, 0, 0, 1, 0, 0, 0] and matched == 6
    items = [{"id": "a", "stress_binary": [0, 1, 0]}]
    s = score([{"id": "a", "given": {"binary": [0, 1, 1]},
                "heard": {"binary": [0, 1, 0], "top": 1, "aligned": 3}}], items)
    assert s["given"] == {"precision": 0.5, "recall": 1.0, "f1": 0.667}
    assert s["heard"]["f1"] == 1.0 and s["top1"] == 1.0


def test_prompts_and_answer_parsing():
    assert capitals("I didn't say he stole it.", [1, 0, 0, 1]) == "I didn't say HE stole it."
    assert capitals("I didn't say he stole it.", [0, 0, 0, 1]) == "i didn't say HE stole it."
    assert quoted("I didn't say he stole it.", [0, 0, 0, 0, 0, 1]) == '"it"'
    assert quoted("x y", []) == "none"
    answers = ["Someone else said it.", "I never said that."]
    assert parse("The correct answer is **2. I never said that.**", answers) == (2, "rule_bold_option")
    assert parse("1", answers) == (1, "rule_leading_digit")
    assert parse("Probably: someone else said it.", answers) == (1, "rule_answer_text")
    assert parse("I cannot hear the audio.", answers) == (None, "")


class FakeReader(BaseHTTPRequestHandler):
    """Answers 1 when stress is marked; otherwise refuses, and the judge then says 2."""
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
        if self.path.endswith("/models"):
            self._send({"data": [{"id": "fake"}]})
        else:
            self._send({"default_generation_settings": {"n_ctx": 8192}})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeReader.calls.append(req)
        prompt = req["messages"][0]["content"]
        if prompt.startswith("You are a Speech-LM evaluator"):
            text = '{"answer": 2}'
        elif "CAPITAL LETTERS" in prompt or "stressed the words" in prompt:
            text = "**1. " + prompt.split("1. ")[1].split("\n")[0] + "**"
        else:
            text = "There is no way to tell from the text."
        self._send({"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def test_reader_scores_each_marking(tmp_path):
    src = make_set(tmp_path)
    (tmp_path / "set").mkdir()
    StressSet().run(Item(id="W", path=str(src)), {}, tmp_path / "set", {"revision": "pinned", "split": "test"},
                    None, Context(config=None, scratch=tmp_path))
    marks = tmp_path / "marks.jsonl"
    marks.write_text("".join(json.dumps({"id": i, "given": {"binary": [1, 0, 0, 0, 0, 0]},
                                         "heard": {"binary": [0, 0, 0, 1, 0, 0], "top": None}}) + "\n"
                             for i in ("w1", "w2")))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeReader)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stage = StressRead()
    try:
        results = {}
        for marking, prompt in (("labels", "capitals"), ("none", "capitals"), ("whistress_top", "capitals"),
                                ("whistress_given", "paper")):
            settings = {k: s.default for k, s in stage.settings.items()} | {
                "endpoint": f"http://127.0.0.1:{srv.server_port}/v1", "marks": marking, "prompt": prompt}
            out = tmp_path / f"{marking}-{prompt}"
            out.mkdir()
            state = stage.setup(settings, Context(config=None, scratch=tmp_path))
            res = stage.run(Item(id="W"), {"stress_set": tmp_path / "set/set.zip", "stress_marks": marks}, out,
                            settings, state, Context(config=None, scratch=tmp_path, log=lambda m: None))
            stage.teardown(state)
            rows = [json.loads(x) for x in (out / "answers.jsonl").read_text().splitlines()]
            results[marking] = (res.metrics, rows)
    finally:
        srv.shutdown()
    m, rows = results["labels"]
    assert m["correct"] == 1 and m["parse"] == {"rule_bold_option": 2} and m["blind_accuracy"] == 1.0
    assert rows[0]["prompt"].startswith('The speaker said: "I didn\'t say he stole it"\nWords in CAPITAL')
    m, rows = results["none"]
    assert m["parse"] == {"judge": 2} and m["correct"] == 1 and "CAPITAL" not in rows[0]["prompt"]
    _, rows = results["whistress_top"]  # WhiStress named no word: nothing is marked
    assert rows[0]["marked"] == [] and "CAPITAL LETTERS" in rows[0]["prompt"]
    _, rows = results["whistress_given"]
    assert 'and stressed the words: "i".' in rows[0]["prompt"]
    # every question carries its own item's seed and Gemma's sampling settings
    asked = [c for c in FakeReader.calls if not c["messages"][0]["content"].startswith("You are")]
    assert {c["temperature"] for c in asked} == {1.0} and {c["top_k"] for c in asked} == {64}
    assert len({c["seed"] for c in asked}) == 2
