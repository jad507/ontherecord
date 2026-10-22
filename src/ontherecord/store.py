"""The workspace on disk.

    <workspace>/store/<stage>/<item>/<key>/      one stage's outputs for one item, plus receipt.json
    <workspace>/runs/<run_id>/run.json           what a run asked for and what happened to every item at every stage
    <workspace>/runs/<run_id>/recipe.yaml        the recipe exactly as submitted
    <workspace>/runs/<run_id>/log.txt
    <workspace>/runs/<run_id>/stages/<stage>/    the worker's spec, per-item status files, scratch (server logs)

A store entry is complete only if receipt.json is in it. Workers build an entry in a sibling `.partial-<pid>`
directory and rename it into place last, so an interrupted stage never leaves something that looks finished.
Entries are never edited after that; a changed setting or input makes a new key, so a new entry beside the old one.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

KEY_CHARS = 20


class Store:
    def __init__(self, workspace: Path):
        self.root = Path(workspace)
        self.store = self.root / "store"
        self.runs = self.root / "runs"

    def entry(self, stage: str, item: str, key: str) -> Path:
        return self.store / stage / item / key[:KEY_CHARS]

    def lookup(self, stage: str, item: str, key: str) -> dict | None:
        """The receipt of a finished entry, or None."""
        p = self.entry(stage, item, key) / "receipt.json"
        if not p.is_file():
            return None
        receipt = json.loads(p.read_text(encoding="utf-8"))
        return receipt if receipt.get("key") == key else None

    def output_path(self, stage: str, item: str, key: str, output: dict) -> Path:
        f = Path(output["file"])
        return f if output.get("external") else self.entry(stage, item, key) / f

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    def list_runs(self) -> list[Path]:
        return sorted((p for p in self.runs.glob("*/run.json")), reverse=True) if self.runs.is_dir() else []


def write_json_atomic(path: Path, data) -> None:
    """Readers (the GUI) may open the file at any moment, so it is replaced whole, never half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, path)
