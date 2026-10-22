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
