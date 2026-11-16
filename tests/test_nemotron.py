"""nemotron_transcribe without a model: the server's answer turned into the transcript and RTTMs later stages read."""
from __future__ import annotations

import json

import pytest

from ontherecord.stages import nemotron as nm
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


def test_the_transcript_is_segments_with_speakers_and_word_times():
    from ontherecord.artifacts import check_segments, segments
    words, turns = nm.piece_words_and_turns(ANSWER, 0.0, prefix="P2_")
    doc = check_segments(segments(nm.segments_from_words(words)))
    assert [(u["id"], u["speaker"]) for u in doc["units"]] == [("s0000", "P2_SPEAKER_01"), ("s0001", "P2_SPEAKER_00")]
    assert all(u["words"] for u in doc["units"])
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


def test_either_checkpoint_resolves_to_its_pinned_revision_and_nvfp4_needs_blackwell(tmp_path):
    from ontherecord.stages.base import UserError
    s = lambda m: {"model": m, "revision": ""}  # noqa: E731
    blackwell = {"cuda": [{"name": "RTX 5070 Ti", "compute_cap": "12.0"}]}
    ada = {"cuda": [{"name": "RTX 4090", "compute_cap": "8.9"}]}
    assert nm.NemotronTranscribe.resolve(s(nm.REPO), ada)["revision"] == nm.PINNED[nm.REPO]
    assert nm.NemotronTranscribe.resolve(s(nm.NVFP4), blackwell)["revision"] == nm.PINNED[nm.NVFP4]
    with pytest.raises(UserError, match="Blackwell"):
        nm.NemotronTranscribe.resolve(s(nm.NVFP4), ada)
    (tmp_path / "hf_quant_config.json").write_text(   # as NVIDIA's NVFP4 export writes it
        '{"quantization": {"quant_algo": "MIXED_PRECISION", "quantized_layers": {"a": {"quant_algo": "FP8"}, '
        '"b": {"quant_algo": "NVFP4"}}}}')
    assert nm.quant_algo(tmp_path) == "NVFP4" and nm.OFFLOAD["NVFP4"].startswith("10")
    args = nm.server_args(tmp_path, 1234, {"gpu_memory_utilization": 0.85, "max_num_batched_tokens": 4096,
                                           "speculative_tokens": 0, "offload_backend": "auto"}, 6)
    assert args[args.index("--kv-cache-dtype") + 1] == "fp8" and args[args.index("--cpu-offload-gb") + 1] == "6"
