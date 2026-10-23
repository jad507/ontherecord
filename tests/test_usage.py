"""The usage ledger and the time estimates, from receipts written by hand."""
from __future__ import annotations

import json
from pathlib import Path

from ontherecord import usage


def receipt(ws: Path, stage: str, item: str, elapsed: float, metrics: dict, *, run="r1", setup=0.0, device="cuda",
            settings=None, host="box", started="2026-10-22T10:00:00+00:00", tools=None):
    d = ws / "store" / stage / item / f"{stage}-{item}-{run}"
    d.mkdir(parents=True)
    (d / "receipt.json").write_text(json.dumps({
        "stage": stage, "item": {"id": item}, "settings": settings or {}, "metrics": metrics, "device": device,
        "tools": tools or {}, "run_id": run, "environment": {"hostname": host, "gpu": {"name": "RTX"},
                                                            "packages": {"faster-whisper": "1.2.1"}},
        "timing": {"started_utc": started, "elapsed_s": elapsed, "batch_setup_s": setup}}))


def test_ledger_counts_each_piece_of_work_once_and_prices_the_tokens(tmp_path):
    receipt(tmp_path, "transcribe", "a", 480, {"duration_s": 3600}, settings={"model": "large-v3"}, setup=20)
    receipt(tmp_path, "transcribe", "b", 240, {"duration_s": 1800}, settings={"model": "large-v3"}, setup=20)
    receipt(tmp_path, "extract_comments", "a", 120, {"input_tokens": 1_000_000, "output_tokens": 100_000,
                                                     "gen_tokens_per_s": 40.0, "prompt_tokens_per_s": 900.0},
            device="server", tools={"model": {"file": "g.gguf"}, "llama_server": "version: x (build 11249)"})
    receipt(tmp_path, "stress_read", "s", 600, {"items": 200}, device="server")  # from before token counting
    usage.record_assistant(tmp_path, "e4b.gguf", {"usage": {"prompt_tokens": 2000, "completion_tokens": 100},
                                                 "timings": {"predicted_n": 100, "predicted_ms": 2000}}, 3.0)
    d = usage.ledger(tmp_path, [{"backend": "local", "started_utc": "2026-10-22T10:00:00+00:00",
                                 "finished_utc": "2026-10-22T10:30:00+00:00"}])
    assert d["receipts"] == 4
    assert d["compute"]["setup_hours"] == round(20 / 3600, 2)  # one batch, one model load, however many receipts
    assert d["compute"]["busy_hours"] == {"recipes": 0.5}
    assert d["tokens"] == {"input": 1_002_000, "output": 100_100}
    assert d["api_equivalent"]["usd"]["claude-sonnet-5-5"] == round((1_002_000 * 2 + 100_100 * 10) / 1e6, 2)
    assert d["api_equivalent"]["usd"]["gpt-5.6-terra"] == round((1_002_000 * 2 + 100_100 * 12) / 1e6, 2)
    assert d["api_equivalent"]["uncounted_receipts"] == 1
    assert d["speeds"]["audio"][0]["times_real_time"] == 7.5
    assert d["speeds"]["llm"][0]["gen_tokens_per_s"] == 40.0 and d["speeds"]["llm"][0]["model"] == "g.gguf"


def test_llm_metrics_sum_calls_and_take_the_servers_speeds():
    m = usage.llm_metrics([{"input_tokens": 100, "output_tokens": 10, "prompt_n": 100, "prompt_ms": 100,
                            "gen_n": 10, "gen_ms": 500},
                           {"input_tokens": 300, "output_tokens": 30, "prompt_n": 300, "prompt_ms": 300,
                            "gen_n": 30, "gen_ms": 1500}])
    assert m == {"input_tokens": 400, "output_tokens": 40, "llm_calls": 2, "prompt_tokens_per_s": 1000.0,
                 "gen_tokens_per_s": 20.0}
    assert usage.llm_metrics([{"input_tokens": 5}]) == {"input_tokens": 5, "output_tokens": 0, "llm_calls": 1}


def test_estimates_use_this_machines_pace_and_jeffs_yardsticks(tmp_path):
    receipt(tmp_path, "transcribe", "a", 480, {"duration_s": 3600}, settings={"model": "large-v3"}, setup=30)
    receipt(tmp_path, "transcribe", "t", 60, {"duration_s": 3600}, settings={"model": "large-v3-turbo"}, run="r2")
    receipt(tmp_path, "diarize", "a", 60, {"duration_s": 3600})
    receipt(tmp_path, "stress_read", "s", 900, {})
    receipt(tmp_path, "transcribe", "x", 9999, {"duration_s": 60}, host="elsewhere")  # another machine's pace
    rates = usage.Rates(tmp_path, host="box")
    steps = [{"name": "diarize", "stage": "diarize"}, {"name": "transcribe", "stage": "transcribe",
                                                       "settings": {"model": "large-v3"}}]
    est = usage.estimate(rates, steps, [{"id": "a"}, {"id": "new"}], {"a": {"transcribe"}, "new": {"diarize", "transcribe"}})
    # a: 60 min known -> 480 s; new: unknown length -> an hour: 60 + 480; one setup of 30 s for the transcribe batch
    assert est["seconds"] == 480 + 540 + 30 and est["unknown_length"] == ["new"] and est["fits"] == "a lunch break"
    assert "counting a recording of unknown length as an hour" in est["text"]
    turbo = usage.estimate(rates, [{"name": "t", "stage": "transcribe", "settings": {"model": "large-v3-turbo"}}],
                           [{"id": "a"}], {"a": {"t"}})
    assert turbo["seconds"] == 60  # turbo's own pace, not large-v3's
    never = usage.estimate(rates, [{"name": "w", "stage": "whistress"}], [{"id": "a"}], {"a": {"w"}})
    assert never["never_done"] == ["whistress"] and never["seconds"] == 0
    assert [usage.fits(s) for s in (600, 3000, 7200, 30000, 90000)] == [
        "a coffee break", "a lunch break", "a long meeting", "overnight", "more than a night"]
