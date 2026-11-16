"""The stage registry. Order here is the order the GUI offers them in.

A lab can add its own stages without editing this package: set ONTHERECORD_STAGES to
`name=package.module:Class` entries separated by ';', with that module importable (on PYTHONPATH) in every Python
environment that runs it. The tests use the same hook.
"""
from __future__ import annotations

import importlib
import os

from .base import Stage

_BUILTIN = {
    "download": "download:Download",
    "audio": "audio:Audio",
    "diarize": "diarize:Diarize",
    "transcribe": "transcribe:Transcribe",
    "nemotron_transcribe": "nemotron:NemotronTranscribe",
    "align": "align:Align",
    "word_choices": "word_choices:WordChoices",
    "turns": "turns:Turns",
    "code": "code:Code",
    "stress_set": "stress_set:StressSet",
    "whistress": "whistress:WhiStressMarks",
    "stress_read": "stress_read:StressRead",
}


def _registry() -> dict[str, str]:
    reg = {k: f"{__name__}.{v}" for k, v in _BUILTIN.items()}
    for entry in filter(None, (e.strip() for e in os.environ.get("ONTHERECORD_STAGES", "").split(";"))):
        name, target = entry.split("=", 1)
        reg[name.strip()] = target.strip()
    return reg


def names() -> list[str]:
    return list(_registry())


def get(name: str) -> type[Stage]:
    reg = _registry()
    if name not in reg:
        raise KeyError(f"no stage named {name!r}; stages: {', '.join(reg)}")
    module, cls = reg[name].split(":")
    return getattr(importlib.import_module(module), cls)
