"""Tiny model-free stages for exercising the runner."""
from __future__ import annotations

import time
from pathlib import Path

from ontherecord.stages.base import Check, Result, Setting, Stage


class Upper(Stage):
    name = "upper"
    needs = {"media": "media"}
    makes = {"text": "other"}
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
    needs = {"text": "other"}
    makes = {"count": "other"}
    settings = {"prompt": Setting("prompt.txt", "a file setting", kind="file")}

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        n = len(Path(inputs["text"]).read_text(encoding="utf-8"))
        (out / "count.txt").write_text(f"{n} {len(settings['prompt'])}", encoding="utf-8")
        return Result(files={"count": "count.txt"})


class Slow(Stage):
    name = "slow"
    needs = {"text": "other"}
    makes = {"slow": "other"}
    settings = {"seconds": Setting(60, "how long to sleep")}

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        (out / "started").write_text("x")
        time.sleep(settings["seconds"])
        (out / "slow.txt").write_text("done")
        return Result(files={"slow": "slow.txt"})


class Pair(Stage):
    """Needs two inputs, like align (speaker turns + words), to exercise the same_source check."""
    name = "pair"
    needs = {"text": "other", "count": "other"}
    makes = {"pair": "other"}
    settings = {}

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        joined = Path(inputs["text"]).read_text(encoding="utf-8") + " | " + Path(inputs["count"]).read_text("utf-8")
        (out / "pair.txt").write_text(joined, encoding="utf-8")
        return Result(files={"pair": "pair.txt"})


class FakeAudio(Stage):
    """Stands in for the audio stage: the 'audio' is the source text."""
    name = "audio"
    needs = {"media": "media"}
    makes = {"audio": "audio"}
    settings = {"channel": Setting("left", "recorded so the player can say which channel it plays"),
                "sample_rate": Setting(16000, "")}

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        (out / "audio.txt").write_text(Path(inputs["media"]).read_text(encoding="utf-8"), encoding="utf-8")
        return Result(files={"audio": "audio.txt"})


class FakeTranscribe(Stage):
    """Stands in for Whisper: one segment per line, ten seconds each."""
    name = "transcribe"
    needs = {"audio": "audio"}
    makes = {"transcript": "other"}
    settings = {"deaf": Setting(False, "hear nothing, like Whisper on cancelled audio")}

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        import json
        lines = [] if settings["deaf"] else Path(inputs["audio"]).read_text(encoding="utf-8").splitlines()
        segs = [{"start": 10.0 * i, "end": 10.0 * i + 9, "text": t} for i, t in enumerate(lines)]
        (out / "transcript.json").write_text(json.dumps({"segments": segs}), encoding="utf-8")
        return Result(files={"transcript": "transcript.json"},
                      metrics={"words": sum(len(t.split()) for t in lines), "segments": len(segs)})


# ---------------------------------------------------------------- typed toys: the shared formats (artifacts.py)
class ToySegments(Stage):
    """One segment per line of the source, ten seconds each; speakers A and B alternate in pairs of lines."""
    name = "toy_segments"
    needs = {"media": "media"}
    makes = {"lines": "segments", "notes": "other"}
    settings = {"speakers": Setting(True, "label speakers"), "broken": Setting("", "write a malformed file: "
                                                                                  "'dup', 'noid' or 'backwards'")}

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        from ontherecord.artifacts import segments, write_json
        lines = Path(inputs["media"]).read_text(encoding="utf-8").split()
        units = [{"id": f"s{i:04d}", "start": 10.0 * i, "end": 10.0 * i + 9, "text": t,
                  **({"speaker": "AB"[(i // 2) % 2]} if settings["speakers"] else {})} for i, t in enumerate(lines)]
        if settings["broken"] == "dup":
            units[-1]["id"] = units[0]["id"]
        elif settings["broken"] == "noid":
            del units[0]["id"]
        elif settings["broken"] == "backwards":
            units[0]["end"] = -1.0 + units[0]["start"]
        write_json(out / "lines.json", segments(units))
        (out / "notes.txt").write_text("toy", encoding="utf-8")
        return Result(files={"lines": "lines.json", "notes": "notes.txt"}, metrics={"words": len(lines)})


class ToyMerge(Stage):
    """Any segments in, the same as one unit per speaker run out (a stand-in for turns)."""
    name = "toy_merge"
    needs = {"units": "segments"}
    makes = {"merged": "segments"}
    settings = {}

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        from ontherecord.artifacts import load_segments, segments, write_json
        units = []
        for u in load_segments(inputs["units"])["units"]:
            if units and units[-1].get("speaker") == u.get("speaker"):
                units[-1].update(end=u["end"], text=units[-1]["text"] + " " + u["text"])
                units[-1]["from"].append(u["id"])
            else:
                units.append({"id": f"t{len(units):04d}", "start": u["start"], "end": u["end"], "text": u["text"],
                              "speaker": u.get("speaker", ""), "from": [u["id"]]})
        write_json(out / "merged.json", segments(units, "turn"))
        return Result(files={"merged": "merged.json"})


class ToyCoder(Stage):
    """Any segments in; codes a unit 'long' when its text has more than one word."""
    name = "toy_coder"
    needs = {"units": "segments"}
    makes = {"codes": "annotations"}
    settings = {}

    def run(self, item, inputs, out: Path, settings, state, ctx) -> Result:
        from ontherecord.artifacts import load_segments, write_json
        units = load_segments(inputs["units"])["units"]
        anns = [{"id": f"a{i:04d}", "target": {"units": [u["id"]]}, "code": "long",
                 "attributes": {"words": len(u["text"].split())}}
                for i, u in enumerate(units) if len(u.get("text", "").split()) > 1]
        write_json(out / "codes.json", {"format": "annotations/1", "codebook": None,
                                        "by": {"kind": "model", "id": "toy"}, "annotations": anns})
        return Result(files={"codes": "codes.json"})
