"""The library: what can be played, read and searched for each recording, read straight from runs and receipts."""
from __future__ import annotations

from pathlib import Path

import pytest

from ontherecord.config import MachineConfig
from ontherecord.library import Library
from ontherecord.recipe import recipe_from_text
from ontherecord.runner import Runner
from ontherecord.store import Store

HERE = Path(__file__).parent


@pytest.fixture(autouse=True)
def fake_stages(monkeypatch):
    monkeypatch.setenv("ONTHERECORD_STAGES", "audio=stages_for_tests:FakeAudio;"
                                             "transcribe=stages_for_tests:FakeTranscribe")
    monkeypatch.setenv("PYTHONPATH", str(HERE))
    monkeypatch.syspath_prepend(str(HERE))


def run(ws: Path, transcribe: str = "{}") -> str:
    text = ("name: lib\nsource:\n  files: ['Council Meeting - October 28, 2025 [abc123XYZ].txt']\nstages:\n"
            f"  - download: {{}}\n  - audio: {{}}\n  - transcribe: {transcribe}\n")
    return Runner(recipe_from_text(text, ws), MachineConfig(workspace=ws / "w"), log=lambda *_: None).execute()["run_id"]


def test_recordings_view_and_search(tmp_path):
    (tmp_path / "Council Meeting - October 28, 2025 [abc123XYZ].txt").write_text(
        "call to order\nI am here for public comment on the data center\nadjourned", encoding="utf-8")
    good = run(tmp_path)
    deaf = run(tmp_path, "{deaf: true}")  # newer, but it heard nothing
    lib = Library(Store(tmp_path / "w"))
    [rec] = lib.recordings()
    assert rec["id"] == "abc123XYZ" and rec["date"] == "2025-10-28"
    assert {v["run_id"]: v["words"] for v in rec["views"]} == {good: 14, deaf: 0}
    assert rec["default_run"] == good  # the view with words in it, not merely the newest

    view = lib.recording(good, "abc123XYZ")
    assert view["text_from"] == "transcribe" and len(view["segments"]) == 3
    assert view["audio"]["settings"]["channel"] == "left"
    assert view["segments"][1]["start"] == 10.0

    hits = lib.search("PUBLIC comment")
    assert [(h["id"], h["start"]) for h in hits] == [("abc123XYZ", 10.0)]
    assert lib.search("nothing like this") == []
    with pytest.raises(KeyError):
        lib.recording(good, "no-such-item")
