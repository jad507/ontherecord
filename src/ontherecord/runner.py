"""Plan and execute a recipe.

Execution goes stage by stage across all items, so each model loads once per stage. For every item at every stage
the runner computes the cache key from the stage, its settings and the hashes of the item's inputs. A key already in
the store is reused, with its receipt; anything else goes to a worker process. A failed check stops that item's later
stages unless the recipe says `on_fail: continue`.

Whether a later stage can be reused is only known once the earlier one has run: if re-extracting the audio gives
byte-identical audio, diarization is reused; if one byte differs, it runs again. The plan therefore says "runs"
for the first changed stage and "runs unless its inputs come out identical" for the stages after it.

The runner also traces every input back through the stages that made it. If two of a stage's inputs come from
different bytes of the same artifact (speaker turns from one audio file, words from another), the stage gets a
failed `same_source` check: it is blocked, or, with `on_fail: continue`, it runs and carries the check.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import MachineConfig
from .envinfo import gpu
from .hashing import cache_key
from .recipe import Recipe, StageSpec, source_identity
from .store import Store, write_json_atomic

SRC_DIR = Path(__file__).resolve().parents[1]


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id(name: str) -> str:
    from .recipe import slug
    return f"{datetime.now():%Y%m%d-%H%M%S}-{slug(name, 40)}"


def worst_check(receipt: dict) -> str:
    order = ["ok", "warn", "fail"]
    return max((c["status"] for c in receipt.get("checks", [])), key=order.index, default="ok")


def failed_checks(receipt: dict, run_checks: list[dict] = ()) -> list[str]:
    return [f"{c['name']}: {c['message']}" for c in [*receipt.get("checks", []), *run_checks] if c["status"] == "fail"]


def same_source_check(conflicts: dict) -> list[dict]:
    """The failed check for inputs that trace back to different files of the same artifact, or nothing."""
    if not conflicts:
        return []
    parts = [f"{len(shas)} different {art} files ("
             + ", ".join(f"{sha[:8]} from {step}" for sha, step in sorted(shas.items(), key=lambda kv: kv[1])) + ")"
             for art, shas in conflicts.items()]
    return [{"name": "same_source", "status": "fail", "value": conflicts,
             "message": "its inputs were made from " + "; ".join(parts)}]


class Cancelled(Exception):
    pass


class Runner:
    def __init__(self, recipe: Recipe, cfg: MachineConfig, run_id: str | None = None, log=print):
        self.recipe, self.cfg, self.log = recipe, cfg, log
        self.store = Store(cfg.workspace)
        self.run_id = run_id or new_run_id(recipe.name)
        self.dir = self.store.run_dir(self.run_id)
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.record: dict = {}
        self._source_ids: dict[str, str] = {}
        self._by_name = {s.name: s for s in recipe.stages}
        self._traces: dict[tuple[str, str], tuple[dict, list]] = {}

    # ------------------------------------------------------------------ keys
    # `arts` is one item's outputs so far: step name -> artifact -> {path, sha256, step, lineage}, where lineage maps
    # every artifact upstream of that output to {sha256: step that made it}.
    def key_inputs(self, spec: StageSpec, item_id: str, arts: dict) -> dict[str, str]:
        if not spec.cls.needs:
            if item_id not in self._source_ids:
                item = next(i for i in self.recipe.items if i.id == item_id)
                self._source_ids[item_id] = source_identity(item)
            return {"source": self._source_ids[item_id]}
        return {n: arts[src][n]["sha256"] for n, src in spec.inputs_from.items()}

    def key_for(self, spec: StageSpec, item_id: str, arts: dict) -> str:
        return cache_key(spec.stage, spec.cls.version, item_id, spec.key_settings,
                         self.key_inputs(spec, item_id, arts))

    @staticmethod
    def trace(spec: StageSpec, arts: dict) -> tuple[dict, dict]:
        """Everything upstream of this step's inputs, and the artifacts that reach it as two or more different files
        (counted only where that first happens, not again in every stage after it)."""
        lineage: dict[str, dict] = {}
        inherited = set()
        for n, src in spec.inputs_from.items():
            a = arts[src][n]
            for art, shas in [*a["lineage"].items(), (n, {a["sha256"]: src})]:
                lineage.setdefault(art, {}).update(shas)
                if len(shas) > 1:
                    inherited.add(art)
        return lineage, {art: shas for art, shas in lineage.items() if len(shas) > 1 and art not in inherited}

    def artifacts_from(self, spec: StageSpec, item_id: str, receipt: dict, lineage: dict) -> dict:
        return {name: {"path": str(self.store.output_path(spec.stage, item_id, receipt["key"], out)),
                       "sha256": out["sha256"], "step": spec.name, "lineage": lineage}
                for name, out in receipt["outputs"].items()}

    # ------------------------------------------------------------------ plan
    def plan(self) -> dict:
        """For every item and stage: 'cached', 'run', 'run-if-changed' (after a stage that runs), or 'blocked'.
        A stage wired to inputs that may come from different files of one artifact also gets a 'mixed' note."""
        out: dict[str, dict[str, dict]] = {}
        for item in self.recipe.items:
            arts: dict = {}
            row: dict[str, dict] = {}
            unknown: set[str] = set()   # steps that will run, and every step downstream of one
            blocked = False
            for spec in self.recipe.stages:
                note = {"mixed": spec.mixed} if spec.mixed else {}
                if blocked:
                    row[spec.name] = {"status": "blocked", **note}
                    continue
                if unknown & set(spec.inputs_from.values()):
                    row[spec.name] = {"status": "run-if-changed", **note}
                    unknown.add(spec.name)
                    continue
                lineage, conflicts = self.trace(spec, arts)
                run_checks = same_source_check(conflicts)
                if run_checks and spec.on_fail == "stop":
                    row[spec.name] = {"status": "blocked", "checks": "fail", **note}
                    blocked = True
                    continue
                key = self.key_for(spec, item.id, arts)
                receipt = self.store.lookup(spec.stage, item.id, key)
                if receipt:
                    row[spec.name] = {"status": "cached", "key": key, "checks": worst_check(
                        {"checks": [*receipt.get("checks", []), *run_checks]}),
                        "elapsed_s": receipt["timing"]["elapsed_s"], **note}
                    arts[spec.name] = self.artifacts_from(spec, item.id, receipt, lineage)
                    blocked = bool(failed_checks(receipt)) and spec.on_fail == "stop"
                else:
                    row[spec.name] = {"status": "run", "key": key, **note}
                    unknown.add(spec.name)
            out[item.id] = row
        return out

    # ------------------------------------------------------------------ record
    def _init_record(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "recipe.yaml").write_text(self.recipe.text, encoding="utf-8")
        self.record = {
            "run_id": self.run_id, "name": self.recipe.name, "description": self.recipe.description,
            "recipe_path": str(self.recipe.path) if self.recipe.path else None,
            "status": "running", "created_utc": utc(), "finished_utc": None, "pid": os.getpid(),
            "host": socket.gethostname(), "gpu": gpu(),
            "machine_config": self.cfg.describe(),
            "stages": [{"name": s.name, "stage": s.stage, "label": s.cls.label, "version": s.cls.version,
                        "group": s.cls.group, "settings": s.record_settings, "on_fail": s.on_fail,
                        "from": s.inputs_from, "mixed": s.mixed} for s in self.recipe.stages],
            "items": [i.as_dict() for i in self.recipe.items],
            "cells": {i.id: {s.name: {"status": "pending"} for s in self.recipe.stages} for i in self.recipe.items},
        }
        self._save()

    def _save(self) -> None:
        write_json_atomic(self.dir / "run.json", self.record)

    def _cell(self, item_id: str, stage: str, **kw) -> None:
        self.record["cells"][item_id][stage] = kw
        self._save()

    # ------------------------------------------------------------------ execute
    def execute(self) -> dict:
        self._init_record()
        self._install_signal_handlers()
        self.log(f"run {self.run_id}: {len(self.recipe.items)} item(s), {len(self.recipe.stages)} stage(s)")
        arts = {i.id: {} for i in self.recipe.items}
        blocked: dict[str, str] = {}
        status = "done"
        try:
            for spec in self.recipe.stages:
                pending = []
                for item in self.recipe.items:
                    if item.id in blocked:
                        self._cell(item.id, spec.name, status="blocked", reason=blocked[item.id])
                        continue
                    lineage, conflicts = self.trace(spec, arts[item.id])
                    run_checks = same_source_check(conflicts)
                    if run_checks and spec.on_fail == "stop":
                        reason = f"{spec.name} check failed: same_source: {run_checks[0]['message']}"
                        self._cell(item.id, spec.name, status="blocked", reason=reason, run_checks=run_checks,
                                   checks="fail")
                        blocked[item.id] = reason
                        self.log(f"{spec.name} {item.id}: blocked: {run_checks[0]['message']}")
                        continue
                    self._traces[(spec.name, item.id)] = (lineage, run_checks)
                    key = self.key_for(spec, item.id, arts[item.id])
                    receipt = self.store.lookup(spec.stage, item.id, key)
                    if receipt:
                        self._reuse(spec, item.id, key, receipt, arts, blocked, ran=False)
                    else:
                        pending.append((item, key))
                if pending:
                    self._run_batch(spec, pending, arts, blocked)
                if self.cancelled:
                    raise Cancelled
            if any(c.get("status") == "failed" for row in self.record["cells"].values() for c in row.values()):
                status = "failed"
        except Cancelled:
            status = "cancelled"
            for row in self.record["cells"].values():
                for name, c in row.items():
                    if c.get("status") in ("pending", "running"):
                        row[name] = {"status": "cancelled"}
        except Exception as e:
            status = "failed"
            self.record["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            self.record.update(status=status, finished_utc=utc())
            self._save()
            self.log(f"run {self.run_id}: {status}")
        return self.record

    def _reuse(self, spec, item_id, key, receipt, arts, blocked, ran: bool) -> None:
        lineage, run_checks = self._traces.get((spec.name, item_id), ({}, []))
        arts[item_id][spec.name] = self.artifacts_from(spec, item_id, receipt, lineage)
        fails = failed_checks(receipt, run_checks)
        worst = worst_check({"checks": [*receipt.get("checks", []), *run_checks]})
        entry = self.store.entry(spec.stage, item_id, key)
        extra = {"run_checks": run_checks} if run_checks else {}
        self._cell(item_id, spec.name, status="ran" if ran else "cached", key=key,
                   receipt=str(entry.relative_to(self.store.root) / "receipt.json"), checks=worst,
                   elapsed_s=receipt["timing"]["elapsed_s"], produced_by_run=receipt.get("run_id"), **extra)
        if fails and spec.on_fail == "stop":
            blocked[item_id] = f"{spec.name} check failed: " + "; ".join(fails)
        if run_checks:
            self.log(f"{spec.name} {item_id}: FAIL same_source: {run_checks[0]['message']} (on_fail: continue)")
        if not ran:
            self.log(f"{spec.name} {item_id}: reused ({worst})")

    def _run_batch(self, spec: StageSpec, pending, arts, blocked) -> None:
        sdir = self.dir / "stages" / spec.name
        status_dir = sdir / "status"
        status_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for item, key in pending:
            inputs = {}
            for n, src in spec.inputs_from.items():
                a = arts[item.id][src][n]
                inputs[n] = {"path": a["path"], "sha256": a["sha256"],
                             "from_stage": self._by_name[src].stage, "from_step": src}
            items.append({"item": item.as_dict(), "key": key, "inputs": inputs,
                          "entry": str(self.store.entry(spec.stage, item.id, key))})
            self._cell(item.id, spec.name, status="queued", key=key)
        spec_file = sdir / "spec.json"
        write_json_atomic(spec_file, {
            "stage": spec.stage, "step": spec.name, "run_id": self.run_id, "run_settings": spec.run_settings,
            "record_settings": spec.record_settings, "items": items, "status_dir": str(status_dir),
            "scratch": str(sdir / "scratch"),
            "config_file": str(self.cfg.source) if self.cfg.source else None,
        })
        python = self.cfg.python_for(spec.cls.group)
        self.log(f"{spec.name}: running {len(items)} item(s) with {python}")
        env = {**os.environ, **self.cfg.stage_env, **self.cfg.secret_env(), "PYTHONUNBUFFERED": "1",
               "PYTHONPATH": os.pathsep.join([str(SRC_DIR), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}
        kw: dict = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        with open(self.dir / "log.txt", "a", encoding="utf-8") as logf:
            self.proc = subprocess.Popen([python, "-m", "ontherecord.worker", str(spec_file)], env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", **kw)
            for line in self.proc.stdout:
                logf.write(line)
                logf.flush()
                self.log(line.rstrip("\n"))
                if " ..." in line or " done in " in line or " FAILED" in line:  # an item started or ended
                    self._sync_batch(spec, pending, status_dir, arts, blocked)
            self.proc.wait()
        self.proc = None
        if self.cancelled:  # a killed worker cannot tidy up after itself
            for item, key in pending:
                entry = self.store.entry(spec.stage, item.id, key)
                for partial in entry.parent.glob(entry.name + ".partial-*"):
                    shutil.rmtree(partial, ignore_errors=True)
        for item, key in pending:
            if self.record["cells"][item.id][spec.name].get("status") in ("ran", "failed"):
                continue  # already settled while the batch ran
            st_file = status_dir / f"{item.id}.json"
            st = json.loads(st_file.read_text(encoding="utf-8")) if st_file.is_file() else {}
            receipt = self.store.lookup(spec.stage, item.id, key)
            if receipt:
                self._reuse(spec, item.id, key, receipt, arts, blocked, ran=True)
            elif self.cancelled:
                self._cell(item.id, spec.name, status="cancelled", key=key)
            else:
                err = st.get("error") or "the worker exited without finishing this item"
                self._cell(item.id, spec.name, status="failed", key=key, error=err)
                blocked[item.id] = f"{spec.name} failed"

    def _sync_batch(self, spec: StageSpec, pending, status_dir: Path, arts, blocked) -> None:
        """Bring the run record up to date from the worker's status files while the batch runs, so an item shows as
        finished (its receipt is already in the store) as soon as it is, not when the whole batch ends."""
        for item, key in pending:
            cell = self.record["cells"][item.id][spec.name]
            if cell.get("status") not in ("queued", "running"):
                continue
            try:
                st = json.loads((status_dir / f"{item.id}.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            receipt = self.store.lookup(spec.stage, item.id, key) if st.get("status") == "done" else None
            if receipt:
                self._reuse(spec, item.id, key, receipt, arts, blocked, ran=True)
            elif st.get("status") == "failed":
                self._cell(item.id, spec.name, status="failed", key=key, error=st.get("error"))
                blocked[item.id] = f"{spec.name} failed"
            elif st.get("status") == "running" and cell.get("status") == "queued":
                cell.update(status="running", started_utc=st.get("started_utc"))
                self._save()

    # ------------------------------------------------------------------ cancel
    def cancel(self) -> None:
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            kill_tree(self.proc.pid)

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if sig is not None:
                signal.signal(sig, lambda *_: self.cancel())


def kill_tree(pid: int, timeout: float = 15) -> None:
    """Terminate a process and everything it started (a worker and its llama-server), then kill what is left."""
    import psutil
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = root.children(recursive=True) + [root]
    for p in procs:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass

