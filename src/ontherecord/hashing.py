"""Content hashes and the cache key.

A stage's output is reused only when everything that could change it is the same: the stage's code version, its
settings, the item, and the bytes of every input. That is the cache key. The hands_on_dl scripts skipped work when
an output *file existed*, which is how the May 2026 diarization outlived the audio it was made from: the audio was
re-extracted from the left channel, and diarize.py saw its old .rttm and moved on.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj) -> str:
    """One spelling per value, so equal settings always hash equally."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def cache_key(stage: str, stage_version: str, item_id: str, settings: dict, inputs: dict[str, str]) -> str:
    """`inputs` maps an artifact name to its sha256."""
    return sha256_text(canonical_json({
        "stage": stage, "stage_version": stage_version, "item": item_id,
        "settings": settings, "inputs": inputs,
    }))
