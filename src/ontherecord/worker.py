"""Runs one stage over a batch of items, inside whichever Python environment that stage needs.

    python -m ontherecord.worker <spec.json>

The runner starts this as a separate process, even when the environment is the runner's own: a cancel can then
kill the whole process tree (a llama-server included), and a GPU model's memory is returned to the system when the
stage ends. The model is loaded once per batch (setup), each item is run, and each finished item gets its store
entry and receipt straight away, so a batch interrupted halfway keeps the items it finished.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .config import MachineConfig, load_config
from .envinfo import environment
from .hashing import sha256_file
from .stages import get
from .stages.base import Context, Item
from .store import write_json_atomic


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def main(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    # The runner's config, exactly: no file means the defaults, never a fresh search from this directory.
    cfg = load_config(spec["config_file"]) if spec.get("config_file") else MachineConfig()
    stage = get(spec["stage"])()
    status_dir = Path(spec["status_dir"])
    scratch = Path(spec["scratch"])
    scratch.mkdir(parents=True, exist_ok=True)
    ctx = Context(config=cfg, scratch=scratch, log=log)
    settings = spec["run_settings"]
    env = environment(list(stage.packages))

    def fail(item_id: str, err: str, started: str) -> None:
        write_json_atomic(status_dir / f"{item_id}.json",
                          {"item": item_id, "status": "failed", "error": err, "started_utc": started,
                           "finished_utc": utc()})

    t0, started = time.perf_counter(), utc()
    try:
        state = stage.setup(settings, ctx)
    except Exception:
        err = traceback.format_exc()
        log(f"setup failed:\n{err}")
        for it in spec["items"]:
            fail(it["item"]["id"], "setup failed:\n" + err, started)
        return 1
    setup_s = round(time.perf_counter() - t0, 2)
    log(f"{stage.name}: setup {setup_s} s; {len(spec['items'])} item(s)")

    failures = 0
    try:
        for it in spec["items"]:
            item = Item(**it["item"])
            final = Path(it["entry"])
            tmp = final.with_name(final.name + f".partial-{os.getpid()}")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            started, t1 = utc(), time.perf_counter()
            log(f"{stage.name} {item.id} ...")
            write_json_atomic(status_dir / f"{item.id}.json", {"item": item.id, "status": "running",
                                                                "started_utc": started})
            try:
                inputs = {k: Path(v["path"]) for k, v in it["inputs"].items()}
                res = stage.run(item, inputs, tmp, settings, state, ctx)
                outputs = {}
                for art, fname in res.files.items():
                    p = Path(fname)
                    external = p.is_absolute()
                    path = p if external else tmp / fname
                    outputs[art] = {"file": fname, "sha256": sha256_file(path), "bytes": path.stat().st_size,
                                    "external": external}
                elapsed = round(time.perf_counter() - t1, 2)
                receipt = {
                    "receipt_version": 1, "stage": stage.name, "stage_label": stage.label,
                    "stage_version": stage.version, "item": item.as_dict(), "key": it["key"],
                    "settings": spec["record_settings"], "inputs": it["inputs"], "outputs": outputs,
                    "checks": [c.as_dict() for c in res.checks], "metrics": res.metrics, "tools": res.tools,
                    "device": res.device, "environment": env, "machine_config": cfg.describe(),
                    "timing": {"started_utc": started, "finished_utc": utc(), "elapsed_s": elapsed,
                               "batch_setup_s": setup_s},
                    "run_id": spec["run_id"],
                }
                write_json_atomic(tmp / "receipt.json", receipt)
                if final.exists():  # another run finished the same key first; keep theirs
                    shutil.rmtree(tmp)
                else:
                    final.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(tmp, final)
                worst = max((c.status for c in res.checks), key=["ok", "warn", "fail"].index, default="ok")
                write_json_atomic(status_dir / f"{item.id}.json",
                                  {"item": item.id, "status": "done", "started_utc": started, "finished_utc": utc(),
                                   "elapsed_s": elapsed, "checks": worst})
                log(f"{stage.name} {item.id} done in {elapsed} s"
                    + "".join(f"\n    {c.status.upper():4} {c.name}: {c.message}" for c in res.checks))
            except Exception:
                failures += 1
                shutil.rmtree(tmp, ignore_errors=True)
                err = traceback.format_exc()
                log(f"{stage.name} {item.id} FAILED:\n{err}")
                fail(item.id, err, started)
    finally:
        stage.teardown(state)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
