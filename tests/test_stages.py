"""Stage edge cases that need no model."""
from __future__ import annotations

import json
from types import SimpleNamespace

from ontherecord.stages.base import Context, Item
from ontherecord.stages.extract_comments import ExtractComments


def test_extract_comments_on_a_meeting_with_no_blocks(tmp_path):
    # an averaged phase-inverted meeting: Whisper hears nothing, so there are no blocks to send
    blocks = tmp_path / "blocks.json"
    blocks.write_text(json.dumps({"title": "Empty", "video_id": "x", "upload_date": "20251028", "blocks": []}))
    llm = SimpleNamespace(n_ctx=65536, chat=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call expected")))
    stage = ExtractComments()
    settings = {k: s.default for k, s in stage.settings.items()} | {"system_prompt": "prompt"}
    out = tmp_path / "out"
    out.mkdir()
    res = stage.run(Item(id="x"), {"blocks": blocks}, out, settings, {"llm": llm, "tools": {}},
                    Context(config=None, scratch=tmp_path, log=print))
    assert json.loads((out / "comments.json").read_text())["comments"] == []
    assert {c.name: c.status for c in res.checks} == {"parsed": "ok", "comments": "warn"}


def test_auto_channel_choice():
    from ontherecord.stages.audio import choose_channel
    inverted = {"rms_left_dbfs": -36.5, "rms_right_dbfs": -36.6, "rms_average_dbfs": -68.7, "lr_correlation": -0.999}
    assert choose_channel(inverted)[0] == "left"
    dead_left = {"rms_left_dbfs": -80.0, "rms_right_dbfs": -30.0, "rms_average_dbfs": -36.0, "lr_correlation": 0.0}
    assert choose_channel(dead_left) == ("right", "the left channel is 50 dB quieter (a dead or muted microphone)")
    stereo_mic = {"rms_left_dbfs": -30.0, "rms_right_dbfs": -31.0, "rms_average_dbfs": -32.5, "lr_correlation": 0.4}
    assert choose_channel(stereo_mic)[0] == "average"
    same = {"rms_left_dbfs": -30.0, "rms_right_dbfs": -30.0, "rms_average_dbfs": -30.0, "lr_correlation": 1.0}
    assert choose_channel(same)[0] == "average"
