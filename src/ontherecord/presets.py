"""Ready-made recipes for the common job: speaker-labelled transcripts of some links or files, set for this machine.

The assistant's transcribe tool, `ontherecord transcribe` and the GUI's "Transcribe a recording" all queue this one
recipe. The settings that depend on the machine (which Whisper, which compute type, which extraction model and how
much of it goes on the GPU) are written into the recipe, so the run's record shows them and the same recipe file
gives the same run anywhere.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import bootstrap


def is_link(source: str) -> bool:
    return bool(re.match(r"https?://", source.strip(), re.I))


def transcript_recipe(sources: list[str], with_comments: bool = False, language: str | None = None,
                      name: str | None = None, choice: dict | None = None) -> str:
    """Recipe text for these links and files (files absolute, or globs). `choice` is bootstrap.fit(); by default,
    this machine's."""
    sources = [s.strip() for s in sources if s and s.strip()]
    if not sources:
        raise ValueError("give at least one link or file")
    choice = choice or bootstrap.fit(bootstrap.hardware())
    urls = [s for s in sources if is_link(s)]
    files = [s for s in sources if not is_link(s)]
    if not name:
        what = "transcript and comments" if with_comments else "transcript"
        name = f"{what} of {len(sources)}" if len(sources) > 1 else f"{what} {Path(files[0]).stem}" if files else what
    transcribe = {"model": choice["whisper"], "compute_type": choice["whisper_compute"]}
    if language is not None:
        transcribe["language"] = language
    # With comments, turns are cut as the council study cut its speech blocks, and the code stage reads the whole
    # meeting with the paper's prompt (as recipes/eleven-left.yaml does).
    turns = {"max_gap_s": 30, "max_turn_s": None} if with_comments else {}
    stages = [("download", {}), ("audio", {}), ("diarize", {}), ("transcribe", transcribe), ("align", {}),
              ("turns", turns)]
    if with_comments:
        stages.append(("code", {"codebook": "public-comment.yaml", "instructions": "extract_public_comments_v1.txt",
                                "mode": "find", "context": "whole",
                                "model": bootstrap.MODELS[choice["extraction"]]["file"],
                                "offload": choice["extraction_offload"]}))
    src = ("  urls:\n" + "".join(f"    - {json.dumps(u)}\n" for u in urls) if urls else "") + (
        "  files:\n" + "".join(f"    - {json.dumps(f)}\n" for f in files) if files else "")
    return (f"name: {json.dumps(name)}\n"
            f"description: {json.dumps('Speakers and a transcript' + (', then the public comments' if with_comments else '') + ', set for this computer')}\n"
            f"source:\n{src}stages:\n"
            + "".join(f"  - {stage}: {json.dumps(s, separators=(', ', ': ')) if s else '{}'}\n" for stage, s in stages))


def missing_models(cfg, with_comments: bool = False, choice: dict | None = None) -> list[str]:
    """Catalog models this preset needs that are not downloaded yet (runs are offline, so they must be here)."""
    choice = choice or bootstrap.fit(bootstrap.hardware())
    need = [f"whisper-{choice['whisper']}", "pyannote-community-1"] + ([choice["extraction"]] if with_comments else [])
    status = bootstrap.model_status(cfg.models_root or bootstrap.home_dir() / "models")
    return [n for n in need if status.get(n) is not True]
