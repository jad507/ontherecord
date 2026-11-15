"""Reviews: blind answers, what the page may see, scoring, sets made from a run, and the endpoints."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ontherecord import reviews

CHOICE = {"kind": "choice", "options": [{"value": "yes", "label": "Yes", "key": "y"},
                                        {"value": "no", "label": "No", "key": "n"},
                                        {"value": "unsure", "label": "Unsure", "key": "u", "score": False}],
          "flags": [{"name": "fused", "label": "fuses two speakers", "key": "f"}]}


def choice_set(ws: Path, **kw) -> str:
    items = [{"id": f"b{i}", "text": f"block {i}", "machine": "yes" if i % 2 else "no",
              "hidden": {"stratum": "secret"}, "clip": {"path": "/x.flac", "start": 1, "end": 2}} for i in range(6)]
    return reviews.create(ws, {"name": "Gold test", "question": "Public comment?", "answer": CHOICE, "items": items,
                               "machine_label": "the extractor", **kw})


def test_blind_answers_and_what_counts_as_seen(tmp_path):
    sid = choice_set(tmp_path)
    s = reviews.load(tmp_path, sid)
    assert sorted(s["order"]) == [f"b{i}" for i in range(6)] and s["order"] != [f"b{i}" for i in range(6)]
    page = reviews.public_item(s, "b1")
    assert "machine" not in page and "hidden" not in page and "clip" not in page and page["has_machine"]
    r = reviews.answer(tmp_path, sid, "b1", "no", flags={"fused": True, "bogus": True}, seconds=9.6)
    assert r["machine"] == "yes" and r["machine_seen"] == 0 and r["flags"] == {"fused": True} and r["seconds"] == 10
    assert reviews.answer(tmp_path, sid, "b1", "yes")["machine_seen"] == 1  # a change after the reveal
    assert reviews.reveal(tmp_path, sid, "b2") == "no"
    assert reviews.answer(tmp_path, sid, "b2", "no")["machine_seen"] == 1  # asked to see it first
    reviews.answer(tmp_path, sid, "b3", "yes")
    reviews.answer(tmp_path, sid, "b4", "unsure")
    sc = reviews.score(s, reviews.rows(tmp_path, sid))
    # scored: b1 on its first (blind) answer "no" vs yes; b3 yes vs yes. b2 was seen first; b4 unsure is not scored
    assert sc["scored"] == 2 and sc["agreement"] == 0.5 and sc["confusion"] == {"yes": {"no": 1, "yes": 1}}
    assert reviews.latest(reviews.rows(tmp_path, sid))[("b1", "me")]["value"] == "yes"  # the latest answer wins
    p = reviews.progress(tmp_path, sid)
    assert (p["answered"], p["left"], p["seconds_per_item"]) == (4, 2, 10)
    with pytest.raises(ValueError):
        reviews.answer(tmp_path, sid, "b5", "maybe")
    with pytest.raises(KeyError):
        reviews.answer(tmp_path, sid, "nope", "yes")


def test_two_annotators_never_overwrite_each_other(tmp_path):
    """D1: answers are kept per (item, annotator), in progress, scoring and export; seen is per annotator."""
    sid = choice_set(tmp_path)
    s = reviews.load(tmp_path, sid)
    a = reviews.answer(tmp_path, sid, "b1", "yes", by="ana")
    assert a["by"] == "ana" and a["machine_seen"] == 0
    b = reviews.answer(tmp_path, sid, "b1", "no", by="ben")       # ana's reveal-after does not count as ben seeing it
    assert b["machine_seen"] == 0
    reviews.reveal(tmp_path, sid, "b3", by="ana")
    assert reviews.answer(tmp_path, sid, "b3", "yes", by="ben")["machine_seen"] == 0
    assert reviews.answer(tmp_path, sid, "b3", "yes", by="ana")["machine_seen"] == 1
    log = reviews.rows(tmp_path, sid)
    assert {k: r["value"] for k, r in reviews.answers_of(log, "ana").items()} == {"b1": "yes", "b3": "yes"}
    assert {k: r["value"] for k, r in reviews.answers_of(log, "ben").items()} == {"b1": "no", "b3": "yes"}
    assert reviews.progress(tmp_path, sid, "ana")["answered"] == 2 and reviews.progress(tmp_path, sid, "carl")["left"] == 6
    assert reviews.progress(tmp_path, sid)["annotators"] == {"ana": 2, "ben": 2}
    sc = reviews.score(s, log)       # machine: b1 yes, b3 yes. ana b1 yes (b3 seen: not scored); ben b1 no, b3 yes
    assert sc["scored"] == 3 and sc["agreement"] == round(2 / 3, 3)
    assert sc["by_annotator"]["ana"]["agreement"] == 1.0 and sc["by_annotator"]["ben"]["agreement"] == 0.5
    rows = reviews.export_rows(tmp_path, sid)
    assert sorted((r["item"], r["by"], r["answer"]) for r in rows) == [
        ("b1", "ana", "yes"), ("b1", "ben", "no"), ("b3", "ana", "yes"), ("b3", "ben", "yes")]


def test_old_rows_without_names_are_the_annotator_me(tmp_path):
    sid = choice_set(tmp_path)
    with open(tmp_path / "reviews" / sid / "answers.jsonl", "w") as fh:   # as written before annotators had names
        fh.write(json.dumps({"item": "b1", "value": "yes", "reason": "", "flags": {}, "note": "", "seconds": 3,
                             "machine_seen": 0, "utc": "x"}) + "\n")
        fh.write(json.dumps({"item": "b3", "value": "no", "reason": "", "flags": {}, "note": "", "seconds": 3,
                             "machine_seen": 0, "by": "me", "utc": "x"}) + "\n")
    log = reviews.rows(tmp_path, sid)
    assert set(reviews.answers_of(log, "me")) == {"b1", "b3"}
    assert reviews.score(reviews.load(tmp_path, sid), log)["agreement"] == 0.5
    assert {r["by"] for r in reviews.export_rows(tmp_path, sid)} == {"me"}


STRESS_SET = Path.home() / "ontherecord-workspace"


@pytest.mark.skipif(not (STRESS_SET / "reviews" / "stress-clips-which-word-is-stressed-20261023").is_dir(),
                    reason="Jeff's stress review set is only on shiro")
def test_the_stress_review_still_scores_47_of_49():
    """R1: the answers people gave still load and score as they did (WhiStress, blind: 47 of 49)."""
    sid = "stress-clips-which-word-is-stressed-20261023"
    s, log = reviews.load(STRESS_SET, sid), reviews.rows(STRESS_SET, sid)
    sc = reviews.score(s, log)
    assert (sc["scored"], sc["answered"], sc["exact"], sc["agreement"]) == (49, 170, 0.959, 0.959)
    assert round(sc["agreement"] * sc["scored"]) == 47


def test_a_set_that_never_reveals(tmp_path):
    sid = reviews.create(tmp_path, {"name": "pre-registered", "answer": CHOICE, "reveal": "never",
                                    "items": [{"id": "1", "text": "x"}]})
    assert reviews.load(tmp_path, sid)["reveal"] == "never"
    assert "machine" not in reviews.answer(tmp_path, sid, "1", "yes")
    with pytest.raises(KeyError):
        reviews.reveal(tmp_path, sid, "1")


def test_words_and_text_sets(tmp_path):
    words = {"kind": "words", "options": [{"value": "none", "label": "No word", "key": "x", "score": False},
                                          {"value": "bad", "label": "Bad", "score": False, "reasons": [
                                              {"value": "cut_off", "label": "cut off", "key": "c"}]}]}
    sid = reviews.create(tmp_path, {"name": "stress", "answer": words, "items": [
        {"id": "a", "words": ["i", "never", "said", "that"], "machine": 1},
        {"id": "b", "words": ["she", "stole", "it"], "machine": 0}]})
    with pytest.raises(ValueError):
        reviews.answer(tmp_path, sid, "a", [7])
    with pytest.raises(ValueError):
        reviews.answer(tmp_path, sid, "a", "bad")  # bad needs its reason
    reviews.answer(tmp_path, sid, "a", [1])
    reviews.answer(tmp_path, sid, "b", [1, 2])
    sc = reviews.score(reviews.load(tmp_path, sid), reviews.rows(tmp_path, sid))
    assert sc["exact"] == 0.5 and sc["agreement"] == 0.5
    text = {"kind": "text", "options": [{"value": "unsure", "label": "Can't tell", "score": False}]}
    tid = reviews.create(tmp_path, {"name": "lines", "answer": text, "prefill": True,
                                    "items": [{"id": "1", "text": "the cat sat", "machine": "the cat sat"}]})
    assert reviews.public_item(reviews.load(tmp_path, tid), "1")["prefill"] == "the cat sat"
    r = reviews.answer(tmp_path, tid, "1", "the black cat sat")
    assert r["machine_seen"] == 1  # it was corrected from the machine's own text
    assert reviews.score(reviews.load(tmp_path, tid), reviews.rows(tmp_path, tid))["wer"] == 0.25


def test_bad_specs_are_refused(tmp_path):
    for spec in ({"answer": {"kind": "vote"}, "items": [{"id": 1}]}, {"answer": CHOICE, "items": []},
                 {"answer": CHOICE, "items": [{"id": "1"}, {"id": "1"}]},
                 {"answer": {"kind": "words"}, "items": [{"id": "1"}]}):
        with pytest.raises(ValueError):
            reviews.create(tmp_path, spec)
    with pytest.raises(KeyError):
        reviews.load(tmp_path, "../../etc")


def fake_run(ws: Path) -> str:
    """A finished run with blocks, comments and aligned speakers for one recording."""
    run = "20261023-000000-test"

    def out(stage, name, data):
        d = ws / "store" / stage / "rec1" / "k"
        d.mkdir(parents=True)
        (d / f"{name}.json").write_text(json.dumps(data))
        (d / "receipt.json").write_text(json.dumps({"outputs": {name: {"file": f"{name}.json"}}}))
        return {"status": "ran", "receipt": f"store/{stage}/rec1/k/receipt.json"}
    blocks = [{"block_id": i, "speaker": f"SPEAKER_0{i % 3}", "start": 10.0 * i, "end": 10.0 * i + 8,
               "text": f"words of block {i}"} for i in range(20)]
    cells = {"blocks": out("blocks", "blocks", {"title": "Council, Jan 29", "blocks": blocks}),
             "extract_comments": out("extract_comments", "comments", {"comments": [{"block_ids": [3, 4]},
                                                                                    {"block_ids": [9]}]}),
             "align": out("align", "aligned", {"segments": [{"start": b["start"], "end": b["end"], "text": b["text"],
                                                             "speaker": b["speaker"]} for b in blocks]})}
    (ws / "runs" / run).mkdir(parents=True)
    (ws / "runs" / run / "run.json").write_text(json.dumps({
        "name": "test", "items": [{"id": "rec1", "url": "https://www.youtube.com/watch?v=rec1"}],
        "stages": [{"name": n} for n in cells], "cells": {"rec1": cells}}))
    return run


def test_sets_from_a_run(tmp_path):
    run = fake_run(tmp_path)
    spec = reviews.from_run(tmp_path, run, "comments", sample=8)
    strata = [i["hidden"]["stratum"] for i in spec["items"]]
    assert len(spec["items"]) == 8 and strata.count("flagged") == 3  # all three flagged blocks, then the rest
    it = next(i for i in spec["items"] if i["id"] == "rec1:4") if any(i["id"] == "rec1:4" for i in spec["items"]) else spec["items"][0]
    assert it["link"].endswith(f"t={int(it['start'])}s") and it["title"] == "Council, Jan 29"
    assert {c["offset"] for c in it["context"]} >= {0}
    sp = reviews.from_run(tmp_path, run, "speakers")
    assert [i["speaker"] for i in sp["items"]] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]
    tr = reviews.from_run(tmp_path, run, "transcript", sample=5, blind=True)
    assert len(tr["items"]) == 5 and all(i["text"] == "" and i["machine"] for i in tr["items"]) and not tr["prefill"]
    with pytest.raises(ValueError):
        reviews.from_run(tmp_path, run, "votes")


def test_endpoints_keep_machine_answers_on_the_server(tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from ontherecord.config import MachineConfig
    from ontherecord.server import create_app
    c = TestClient(create_app(MachineConfig(workspace=tmp_path), dispatch=False), base_url="http://127.0.0.1:8765")
    run = fake_run(tmp_path)
    p = c.post("/api/reviews", json={"run": run, "kind": "comments", "sample": 6}).json()
    sid = p["id"]
    st = c.get(f"/api/reviews/{sid}").json()
    item = st["order"][0]
    page = c.get(f"/api/reviews/{sid}/items/{item}").json()
    assert '"machine":' not in json.dumps(st) + json.dumps(page) and "stratum" not in json.dumps(st) + json.dumps(page)
    r = c.post(f"/api/reviews/{sid}/answer", json={"item": item, "value": "maybe"})
    assert r.status_code == 422
    r = c.post(f"/api/reviews/{sid}/answer", json={"item": item, "value": "yes", "seconds": 5}).json()
    assert r["machine"] in ("yes", "no") and r["machine_seen"] == 0
    assert c.get(f"/api/reviews/{sid}").json()["answers"][item]["value"] == "yes"
    c.post(f"/api/reviews/{sid}/answer", json={"item": item, "value": "no", "by": "ana"})
    assert c.get(f"/api/reviews/{sid}?by=ana").json()["answers"][item]["value"] == "no"   # each coder sees their own
    assert c.get(f"/api/reviews/{sid}").json()["answers"][item]["value"] == "yes"
    assert c.get(f"/api/reviews/{sid}?by=ben").json()["answered"] == 0
    assert c.get(f"/api/reviews/{sid}/score").json()["scored"] == 2
    assert c.get(f"/api/reviews/{sid}/score?by=ana").json()["scored"] == 1
    assert "stratum" in c.get(f"/api/reviews/{sid}/export.csv").text  # the export is for analysis, not the page
    assert c.get("/api/reviews/..%2F..%2Fetc").status_code == 404
    assert c.get(f"/api/reviews/{sid}/clip/{item}").status_code == 404  # this fake run has no audio


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="needs ffmpeg")
def test_neighbors_audio_is_cut_from_the_same_recording_and_only_theirs(tmp_path):
    import subprocess
    rec = tmp_path / "meeting.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
                    str(rec)], check=True)
    sid = reviews.create(tmp_path, {"name": "ctx", "question": "q", "answer": {"kind": "choice", "options": [{"value": "yes"}]},
                                    "items": [{"id": "1", "text": "t", "clip": {"path": str(rec), "start": 10, "end": 12},
                                               "context": [{"offset": -1, "speaker": "A", "start": 2, "end": 9, "text": "before"}]}]})
    own, before = reviews.clip(tmp_path, sid, "1"), reviews.clip(tmp_path, sid, "1", ctx=-1)
    assert own and before and own != before
    seconds = lambda p: float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                              str(p)], capture_output=True, text=True).stdout)
    assert abs(seconds(before) - 9) < 0.2  # 2-9 s, a second either side
    assert reviews.clip(tmp_path, sid, "1", ctx=1) is None  # no neighbor there: nothing else of the recording is cut
