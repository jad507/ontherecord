"""nemotron_transcribe without a model: the server's answer turned into the transcript and RTTMs later stages read."""
from __future__ import annotations

import json

import pytest

from ontherecord.stages import nemotron as nm
from ontherecord.stages.align import assign_speaker, load_rttm
from ontherecord.stages.base import Context, Item

# The shape NeMo Speech's ctc_timestamp_middleware returns (ctc_timestamps.py, _public_result, commit e5d79d5)
ANSWER = {
    "words": [{"word": "Good", "start": 0.40, "end": 0.62, "speaker": "0"},
              {"word": "evening,", "start": 0.62, "end": 1.10, "speaker": "0"},
              {"word": "everyone.", "start": 1.10, "end": 1.70, "speaker": "0"},
              {"word": "Thank", "start": 2.30, "end": 2.50, "speaker": "1"},
              {"word": "you.", "start": 2.50, "end": 2.80, "speaker": "1"}],
    "diarization": [{"speaker": 1, "start": 0.30, "end": 1.90}, {"speaker": 0, "start": 1.60, "end": 3.00}],
    "speaker_tag_to_diarization_speaker": {"0": 1, "1": 0},
    "error": None,
}


def test_words_take_the_diarization_speaker_their_tag_maps_to():
    words, turns = nm.piece_words_and_turns(ANSWER, offset=60.0)
    assert [w["speaker"] for w in words] == ["SPEAKER_01"] * 3 + ["SPEAKER_00"] * 2
    assert words[0]["start"] == 60.4 and turns[0] == {"start": 60.3, "end": 61.9, "speaker": "SPEAKER_01"}
    segs = nm.segments_from_words(words)
    assert [(s["speaker"], s["text"]) for s in segs] == [("SPEAKER_01", "Good evening, everyone."),
                                                        ("SPEAKER_00", "Thank you.")]
    assert segs[0]["words"][1]["word"] == " evening,"  # faster-whisper's form


def test_rttms_parse_and_align_like_pyannotes(tmp_path):
    words, turns = nm.piece_words_and_turns(ANSWER, 0.0, prefix="P2_")
    excl = nm.exclusive_turns(turns)
    assert excl == [{"start": 0.3, "end": 1.6, "speaker": "P2_SPEAKER_01"},
                    {"start": 1.6, "end": 3.0, "speaker": "P2_SPEAKER_00"}]
    nm.write_rttm(turns, "a meeting", tmp_path / "s.rttm")
    rows = load_rttm(tmp_path / "s.rttm")
    assert [r["speaker"] for r in rows] == ["P2_SPEAKER_01", "P2_SPEAKER_00"]
    seg = nm.segments_from_words(words)[1]
    assert assign_speaker(seg["start"], seg["end"], rows) == "P2_SPEAKER_00"
    st = nm.turn_stats(turns)
    assert st["speaker_count"] == 2 and st["speech_s"] == 2.7 and st["overlap_s"] == 0.3


def test_loop_guardrail_keeps_one_copy():
    w = lambda t: {"word": t, "start": 0, "end": 0, "speaker": "S"}  # noqa: E731
    words = [w("so")] + [w("thank"), w("you")] * 25 + [w("bye")]
    out, dropped = nm.collapse_loops(words)
    assert [x["word"] for x in out] == ["so", "thank", "you", "bye"] and dropped == 48
    short = [w("no")] * 19 + [w("yes")]
    assert nm.collapse_loops(short) == (short, 0)


def test_long_recordings_are_cut_into_pieces_under_the_limit():
    assert nm.plan_pieces(1800, 3600, lambda t: t) == [(0.0, 1800)]
    quiet = lambda t: t + nm.CUT_SEARCH_S  # the worst case: every cut moves as far as it may  # noqa: E731
    pieces = nm.plan_pieces(3 * 3600 + 5, 3600, quiet)
    assert pieces[0][0] == 0 and pieces[-1][1] == 3 * 3600 + 5
    assert all(b - a <= 3600 for a, b in pieces) and len(pieces) == 4


def test_server_args_follow_the_card_and_offload_experts():
    s = {k: v.default for k, v in nm.NemotronTranscribe.settings.items()}
    args = nm.server_args(nm.Path("/m"), 9000, s, 24)
    assert args[:2] == ["serve", "/m"]
    for flag in ("--use-replayssm", "--trust-request-mm-kwargs", "--no-enable-prefix-caching"):
        assert flag in args
    assert args[args.index("--cpu-offload-gb") + 1] == "24"
    assert args[args.index("--cpu-offload-params") + 1] == "experts"
    assert "--offload-backend" not in args  # auto is vLLM's default
    assert json.loads(args[args.index("--speculative-config") + 1]) == {"method": "mtp", "num_speculative_tokens": 8}
    assert "--cpu-offload-gb" not in nm.server_args(nm.Path("/m"), 9000, s, 0)


def test_run_against_a_server_answer(tmp_path, monkeypatch):
    sf = pytest.importorskip("soundfile")
    np = pytest.importorskip("numpy")
    audio = tmp_path / "a.flac"
    sf.write(audio, np.zeros(16000 * 4, dtype="float32"), 16000)
    sent = []

    def fake_ask(url, data, mime, settings, timeout=0):
        sent.append((url, mime, data == audio.read_bytes()))
        return {"choices": [{"finish_reason": "stop", "message": {"content": "<spk:0> Good evening ..."}}],
                "usage": {"prompt_tokens": 60, "completion_tokens": 12}, "ctc_timestamps": ANSWER}
    monkeypatch.setattr(nm, "ask", fake_ask)
    stage = nm.NemotronTranscribe()
    settings = {k: v.default for k, v in stage.settings.items()}
    out = tmp_path / "out"
    out.mkdir()
    res = stage.run(Item(id="m"), {"audio": audio}, out, settings, {"url": "http://x/v1", "tools": {}},
                    Context(config=None, scratch=tmp_path))
    assert sent == [("http://x/v1", "audio/flac", True)]  # one piece: the file as it is
    assert set(res.files) == set(stage.makes)
    assert json.loads((out / "transcript.json").read_text())["segments"][0]["text"] == "Good evening, everyone."
    assert {c.name: c.status for c in res.checks}["answers"] == "ok"
    assert res.metrics["input_tokens"] == 60 and res.metrics["speaker_count"] == 2
