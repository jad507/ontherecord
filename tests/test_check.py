"""Checking by hand: word_choices' word logic (no model), check sets made from its output, and their answers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ontherecord import reviews
from ontherecord.stages.word_choices import merge_words, norm, span, windows


def test_words_match_the_transcript_split_at_spaces():
    pieces = [(" Chris", 0), (" Balke", 1), ("-", 2), ("Hild", 3), ("itch", 4), (".", 5), (" “", 6), ("Yes", 7),
              (",”", 8), (" co", 9), ("-", 10), ("worker", 11)]
    words = merge_words([{"text": t, "tokens": [i], "start": i, "end": i + 1} for t, i in pieces])
    assert [w["text"].strip() for w in words] == ["Chris", "Balke-Hilditch.", "“Yes,”", "co-worker"]
    assert words[1]["tokens"] == [1, 2, 3, 4, 5] and (words[1]["start"], words[1]["end"]) == (1, 6)
    assert words[2]["tokens"] == [6, 7, 8] and words[2]["start"] == 6


def test_how_many_words_a_choice_replaces():
    t = ["there", "is", "a", "plan"]
    assert span(["their", "is", "a"], t, False) == (1, 1)            # one for one
    assert span(["gonna", "go"], ["going", "to", "go"], False) == (1, 2)  # one for two
    assert span(["ice", "cream", "is"], ["I", "scream", "is"], False) == (2, 2)
    assert span(["our", "phase", "two"], ["phase", "two"], False) == (2, 1)  # two for one (a word was missed)
    assert span(["is", "a", "plan"], t, False) == (0, 1)              # the word was invented: delete it
    assert span(["thing"], ["think"], True) == (1, 1)                 # the text ended there
    assert span(["something", "else"], t, False) == (1, 1)            # no agreement in reach
    assert norm("Don’t, OK?") == "don't ok"


def test_lines_are_grouped_into_whisper_windows():
    segs = [{"start": s, "end": e} for s, e in ((0, 10), (11, 20), (21, 29), (31, 40), (41, 90), (91, 95))]
    assert windows(segs, 0.2) == [[0, 1, 2], [3], [4], [5]]  # a line longer than a window gets one of its own


def words_json() -> dict:
    w = lambda t, s, alts=None: {"start": s, "end": s + 0.4, "word": t, "p": 0.5 if alts else 0.99,  # noqa: E731
                                 **({"alts": alts} if alts else {})}
    return {"format": "segments/1", "unit": "segment", "units": [
        {"id": "s0000", "start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "there is a plan",
         "words": [w("there", 0.0, [{"word": "their", "p": 0.3, "replaces": 1}]), w("is", 0.5), w("a", 1.0),
                   w("plan", 1.5)]},
        {"id": "s0001", "start": 2.2, "end": 4.0, "speaker": "SPEAKER_00", "text": "going to go",
         "words": [w("going", 2.2, [{"word": "gonna", "p": 0.2, "replaces": 2}]), w("to", 2.7), w("go", 3.2)]},
        {"id": "s0002", "start": 9.0, "end": 10.0, "speaker": "SPEAKER_01", "text": "thanks",
         "words": [w("thanks", 9.0)]},
    ]}


def typed_run(store: Path, run_id: str, steps: list[tuple[str, dict, dict, dict, str, object]],
              items=({"id": "rec1", "url": "https://www.youtube.com/watch?v=x"},)) -> None:
    """A finished typed run: each step is (name, needs, makes, from, file name, content) for item rec1."""
    stages, cells = [], {}
    for name, needs, makes, frm, fname, body in steps:
        art = next(iter(makes))
        out = store / "store" / name / "rec1" / "k"
        out.mkdir(parents=True)
        (out / fname).write_text(body if isinstance(body, str) else json.dumps(body))
        (out / "receipt.json").write_text(json.dumps({"outputs": {art: {"file": fname}}, "checks": []}))
        stages.append({"name": name, "stage": name, "needs": needs, "makes": makes,
                       "from": {n: s for n, (s, _) in frm.items()}, "from_artifact": {n: a for n, (_, a) in frm.items()},
                       "settings": {}})
        cells[name] = {"status": "ran", "receipt": f"store/{name}/rec1/k/receipt.json"}
    (store / "runs" / run_id).mkdir(parents=True)
    (store / "runs" / run_id / "run.json").write_text(json.dumps({
        "run_id": run_id, "name": "test", "items": list(items), "stages": stages, "cells": {"rec1": cells}}))


@pytest.fixture
def check_set(tmp_path):
    from ontherecord.artifacts import segments
    from ontherecord.stages.turns import merge
    words = words_json()
    typed_run(tmp_path, "r1", [
        ("audio", {}, {"audio": "audio"}, {}, "audio.flac", ""),
        ("word_choices", {"audio": "audio", "units": "segments"}, {"words": "segments"},
         {"audio": ("audio", "audio")}, "words.json", words),
        ("turns", {"units": "segments"}, {"turns": "segments"}, {"units": ("word_choices", "words")}, "turns.json",
         segments(merge(words["units"], 5.0, 60.0), "turn"))])
    spec = reviews.from_run(tmp_path, "r1", "check", None, codes=["Public comment", "Question"])
    return tmp_path, reviews.create(tmp_path, spec)


def test_a_check_set_is_every_turn_in_order(check_set):
    ws, sid = check_set
    s = reviews.load(ws, sid)
    assert s["order"] == ["rec1:0000", "rec1:0001"] and s["prefill"] and s["answer"]["kind"] == "check"
    first = s["by_id"]["rec1:0000"]
    assert first["text"] == "there is a plan going to go" and len(first["words"]) == 7 and first["speaker"] == "SPEAKER_00"
    assert first["clip"]["format"] == "wav" and s["answer"]["codes"] == [{"key": "1", "name": "Public comment"},
                                                                         {"key": "2", "name": "Question"}]
    page = reviews.public_item(s, "rec1:0000")
    assert "machine" not in page and page["words"][0]["alts"][0]["t"] == "their" and page["clip_start"] == 0.0


def test_checked_answers_and_their_summary(check_set):
    ws, sid = check_set
    value = {"text": "their is a plan gonna go", "words": [{"t": "their", "i": 0}, {"t": "is", "i": 1}, {"t": "a", "i": 2},
             {"t": "plan", "i": 3}, {"t": "gonna", "i": 4}, {"t": "go", "i": 6, "unclear": True}],
             "edits": [{"i": 0, "n": 1, "from": "there", "to": "their", "how": "choice"},
                       {"i": 4, "n": 2, "from": "going to", "to": "gonna", "how": "typed"}],
             "codes": [{"code": "1", "from": 0, "to": 3}, {"code": "2", "from": 4, "to": 5}], "scheme": "follow",
             "keys": 9, "replays": 2, "junk": "dropped"}
    r = reviews.answer(ws, sid, "rec1:0000", value, seconds=40)
    assert r["machine_seen"] == 1 and "junk" not in r["value"] and r["value"]["words"][5]["unclear"]
    reviews.answer(ws, sid, "rec1:0001", "no speech")
    with pytest.raises(ValueError):  # a code the set does not have
        reviews.answer(ws, sid, "rec1:0000", {**value, "codes": [{"code": "Q", "from": 0, "to": 1}]})
    with pytest.raises(ValueError):  # past the end of the turn
        reviews.answer(ws, sid, "rec1:0000", {**value, "codes": [{"code": "1", "from": 0, "to": 9}]})
    sc = reviews.score(reviews.load(ws, sid), reviews.rows(ws, sid))
    assert sc["scored"] == 1 and sc["wer"] == pytest.approx(3 / 6, abs=0.001)
    assert sc["edits"] == {"choice": 1, "typed": 1} and sc["typed_word_was_a_choice"] == 1
    assert sc["by_scheme"]["follow"]["turns"] == 1 and sc["by_scheme"]["follow"]["seconds_per_audio_minute"] == 600.0
    rows = reviews.export_rows(ws, sid)
    assert {r["answer"] for r in rows} == {"their is a plan gonna go", "no speech"}
