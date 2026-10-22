"""Tiny model-free stages for exercising the runner."""
from __future__ import annotations

import time
from pathlib import Path

from ontherecord.stages.base import Check, Result, Setting, Stage


class Upper(Stage):
    name = "upper"
    needs = ("media",)
    makes = ("text",)
    settings = {
        "suffix": Setting("", "appended to the text"),
        "case": Setting("upper", "upper or lower; 'keep' copies unchanged", choices=["upper", "lower", "keep"]),
        "fail": Setting(False, "fail this stage's check"),
        "note": Setting("", "not in the key", in_key=False),
    }

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        text = Path(inputs["media"]).read_text(encoding="utf-8")
        text = {"upper": text.upper(), "lower": text.lower(), "keep": text}[settings["case"]] + settings["suffix"]
        (out / "text.txt").write_text(text, encoding="utf-8")
        return Result(files={"text": "text.txt"},
                      checks=[Check("fail_switch", "fail" if settings["fail"] else "ok", settings["fail"], "test")])


class Count(Stage):
    name = "count"
    needs = ("text",)
    makes = ("count",)
    settings = {"prompt": Setting("prompt.txt", "a file setting", kind="file")}

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        n = len(Path(inputs["text"]).read_text(encoding="utf-8"))
        (out / "count.txt").write_text(f"{n} {len(settings['prompt'])}", encoding="utf-8")
        return Result(files={"count": "count.txt"})


class Slow(Stage):
    name = "slow"
    needs = ("text",)
    makes = ("slow",)
    settings = {"seconds": Setting(60, "how long to sleep")}

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        (out / "started").write_text("x")
        time.sleep(settings["seconds"])
        (out / "slow.txt").write_text("done")
        return Result(files={"slow": "slow.txt"})


class Pair(Stage):
    """Needs two inputs, like align (speaker turns + words), to exercise the same_source check."""
    name = "pair"
    needs = ("text", "count")
    makes = ("pair",)
    settings = {}

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        joined = Path(inputs["text"]).read_text(encoding="utf-8") + " | " + Path(inputs["count"]).read_text("utf-8")
        (out / "pair.txt").write_text(joined, encoding="utf-8")
        return Result(files={"pair": "pair.txt"})


class FakeAudio(Stage):
    """Stands in for the audio stage: the 'audio' is the source text."""
    name = "audio"
    needs = ("media",)
    makes = ("audio",)
    settings = {"channel": Setting("left", "recorded so the player can say which channel it plays"),
                "sample_rate": Setting(16000, "")}

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        (out / "audio.txt").write_text(Path(inputs["media"]).read_text(encoding="utf-8"), encoding="utf-8")
        return Result(files={"audio": "audio.txt"})


class FakeTranscribe(Stage):
    """Stands in for Whisper: one segment per line, ten seconds each."""
    name = "transcribe"
    needs = ("audio",)
    makes = ("transcript",)
    settings = {"deaf": Setting(False, "hear nothing, like Whisper on cancelled audio")}

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        import json
        lines = [] if settings["deaf"] else Path(inputs["audio"]).read_text(encoding="utf-8").splitlines()
        segs = [{"start": 10.0 * i, "end": 10.0 * i + 9, "text": t} for i, t in enumerate(lines)]
        (out / "transcript.json").write_text(json.dumps({"segments": segs}), encoding="utf-8")
        return Result(files={"transcript": "transcript.json"},
                      metrics={"words": sum(len(t.split()) for t in lines), "segments": len(segs)})
