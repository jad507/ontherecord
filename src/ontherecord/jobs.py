"""The job queue behind the GUI and the command line: submit a recipe, run jobs one at a time, cancel them.

Jobs live in SQLite in the workspace (`jobs.db`), so the queue survives the GUI being closed or restarted. Each job
runs as its own process (`python -m ontherecord.cli run-job RUN_ID`), detached from whatever started it and in its
own process group, so it keeps going when the browser tab or the GUI server closes and can be cancelled as a unit.

Jobs run in two lanes, one job at a time in each. The "local" lane runs recipes: on a single GPU, two models rarely
fit at once, and timings from overlapping jobs would not mean much. The "io" lane copies files (imports from a
camera or drive, and clearing them afterwards), which needs the disks rather than the GPU, so a card can be
imported while a long transcription runs. One process runs the queue (the GUI's `serve`, or the headless `dispatch`); it writes a
heartbeat to `dispatcher.json`, and any other would-be dispatcher waits while that heartbeat is fresh. Jobs are also
claimed inside a write transaction, so even two dispatchers could not start two jobs.

Cancel asks the job to stop (SIGTERM on Linux and macOS, CTRL_BREAK on Windows), so the runner kills its worker,
removes partial outputs and records "cancelled"; if the job has not stopped after a grace period, the whole process
tree is killed and the record is corrected here.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import MachineConfig
from .recipe import recipe_from_text
from .runner import SRC_DIR, kill_tree, new_run_id
from .store import Store, write_json_atomic

SCHEMA = """
create table if not exists jobs (
    run_id text primary key,
    name text not null,
    recipe_text text not null,
    recipe_dir text not null,
    backend text not null default 'local',
    status text not null,              -- queued | running | done | failed | cancelled
    created_utc text not null,
    started_utc text,
    finished_utc text,
    pid integer,
    external_id text,                  -- e.g. a Slurm job id
    cancel_requested integer not null default 0
)
"""

FINAL = ("done", "failed", "cancelled")
LANES = ("local", "io")
HEARTBEAT_S = 30   # a dispatcher silent this long is taken as gone


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    import psutil
    try:
        p = psutil.Process(pid)
        return p.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


class JobQueue:
    def __init__(self, cfg: MachineConfig):
        self.cfg = cfg
        self.store = Store(cfg.workspace)
        cfg.workspace.mkdir(parents=True, exist_ok=True)
        self.db_path = cfg.workspace / "jobs.db"
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    # ------------------------------------------------------------------ submit / list
    def submit_task(self, kind: str, spec: dict, name: str) -> str:
        """Queue an import or a clear (see ingest.py) in the io lane. The spec is stored as YAML like a recipe."""
        import yaml
        text = yaml.safe_dump({"kind": kind, "name": name, **spec}, sort_keys=False, allow_unicode=True)
        run_id = new_run_id(name, lambda r: self.get(r) is not None or self.store.run_dir(r).exists())
        with self._db() as db:
            db.execute("insert into jobs (run_id, name, recipe_text, recipe_dir, backend, status, created_utc) "
                       "values (?, ?, ?, ?, 'io', 'queued', ?)", (run_id, name, text, "", utc()))
        return run_id

    def submit(self, recipe_text: str, recipe_dir: Path, backend: str = "local") -> str:
        recipe = recipe_from_text(recipe_text, recipe_dir)  # raises RecipeError before anything is queued
        run_id = new_run_id(recipe.name, lambda r: self.get(r) is not None or self.store.run_dir(r).exists())
        with self._db() as db:
            db.execute("insert into jobs (run_id, name, recipe_text, recipe_dir, backend, status, created_utc) "
                       "values (?, ?, ?, ?, ?, 'queued', ?)",
                       (run_id, recipe.name, recipe_text, str(recipe_dir), backend, utc()))
        return run_id

    def list(self, limit: int = 200) -> list[dict]:
        self.reconcile()
        with self._db() as db:
            rows = db.execute("select * from jobs order by created_utc desc limit ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get(self, run_id: str) -> dict | None:
        with self._db() as db:
            r = db.execute("select * from jobs where run_id = ?", (run_id,)).fetchone()
        return dict(r) if r else None

    def _set(self, run_id: str, **kw) -> None:
        cols = ", ".join(f"{k} = ?" for k in kw)
        with self._db() as db:
            db.execute(f"update jobs set {cols} where run_id = ?", (*kw.values(), run_id))

    # ------------------------------------------------------------------ cancel
    def cancel(self, run_id: str, grace_s: float = 60, wait: bool = False) -> dict:
        """Cancel a job. A running one is asked to stop and killed after `grace_s`; with wait=True this call
        blocks until it has stopped (for the command line), otherwise a thread sees to it (for the GUI)."""
        job = self.get(run_id)
        if not job:
            raise KeyError(run_id)
        if job["status"] == "queued":
            self._set(run_id, status="cancelled", finished_utc=utc(), cancel_requested=1)
            return self.get(run_id)
        if job["status"] != "running":
            return job
        self._set(run_id, cancel_requested=1)
        pid = job["pid"]
        if pid_alive(pid):
            try:
                if os.name == "nt":
                    os.kill(pid, signal.CTRL_BREAK_EVENT)
                else:
                    os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            if wait:
                self._hard_stop_later(run_id, pid, grace_s)
            else:
                threading.Thread(target=self._hard_stop_later, args=(run_id, pid, grace_s), daemon=True).start()
        elif wait:
            self._finish(run_id)
        return self.get(run_id)

    def _hard_stop_later(self, run_id: str, pid: int, grace_s: float) -> None:
        deadline = time.time() + grace_s
        while time.time() < deadline and pid_alive(pid):
            time.sleep(1)
        if pid_alive(pid):
            kill_tree(pid)
        self._finish(run_id)

    # ------------------------------------------------------------------ dispatch
    def start_next(self) -> str | None:
        """In each lane with nothing running, start its oldest queued job. Returns the first run id started."""
        started = [r for r in (self._start_in(lane) for lane in LANES) if r]
        return started[0] if started else None

    def _start_in(self, lane: str) -> str | None:
        with self._lock:
            self.reconcile()
            db = self._db()
            try:
                db.execute("begin immediate")  # check and claim in one write transaction
                row = None
                if not db.execute("select 1 from jobs where status = 'running' and backend = ?", (lane,)).fetchone():
                    row = db.execute("select * from jobs where status = 'queued' and backend = ? "
                                     "order by created_utc limit 1", (lane,)).fetchone()
                if row:
                    db.execute("update jobs set status = 'running', started_utc = ?, pid = null where run_id = ?",
                               (utc(), row["run_id"]))
                db.execute("commit")
            finally:
                db.close()
            if not row:
                return None
            run_id = row["run_id"]
            run_dir = self.store.run_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "submitted_recipe.yaml").write_text(row["recipe_text"], encoding="utf-8")
            cmd = [sys.executable, "-m", "ontherecord.cli",
                   "--config", str(self.cfg.source) if self.cfg.source else "none",
                   "--workspace", str(self.cfg.workspace), "run-job", run_id]
            env = {**os.environ, "PYTHONUNBUFFERED": "1",
                   "PYTHONPATH": os.pathsep.join([str(SRC_DIR), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}
            self._set(run_id, pid=spawn_detached(cmd, run_dir / "job_stdout.txt", env))
            return run_id

    def reconcile(self) -> None:
        """Bring 'running' jobs whose process has ended up to date from their run record."""
        with self._db() as db:
            rows = db.execute("select run_id, pid, started_utc from jobs where status = 'running'").fetchall()
        for r in rows:
            if pid_alive(r["pid"]):
                continue
            if r["pid"] is None and _age_s(r["started_utc"]) < 60:
                continue  # claimed a moment ago; its process is still being started
            self._finish(r["run_id"])

    def _finish(self, run_id: str) -> None:
        job = self.get(run_id)
        if not job or job["status"] in FINAL:
            return
        rec_path = self.store.run_dir(run_id) / "run.json"
        status = "failed"
        if rec_path.is_file():
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
            status = rec.get("status", "failed")
            if status == "running":  # killed before it could write its own ending
                status = "cancelled" if job["cancel_requested"] else "failed"
                for row in rec["cells"].values():
                    for name, c in row.items():
                        if c.get("status") in ("pending", "queued", "running"):
                            row[name] = {"status": status if status == "cancelled" else "failed",
                                         "error": "the job process ended unexpectedly" if status == "failed" else None}
                rec.update(status=status, finished_utc=utc())
                write_json_atomic(rec_path, rec)
        elif job["cancel_requested"]:
            status = "cancelled"
        self._set(run_id, status=status, finished_utc=utc())

    # ------------------------------------------------------------------ the dispatcher and its heartbeat
    def dispatcher(self) -> dict | None:
        """The process running this workspace's queue, if one has written its heartbeat recently."""
        try:
            d = json.loads((self.cfg.workspace / "dispatcher.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - d.get("t", 0) > HEARTBEAT_S:
            return None
        if d.get("host") == socket.gethostname() and not pid_alive(d.get("pid")):
            return None
        return d

    def _beat(self, kind: str) -> None:
        write_json_atomic(self.cfg.workspace / "dispatcher.json",
                          {"pid": os.getpid(), "host": socket.gethostname(), "kind": kind, "t": time.time(),
                           "utc": utc()})

    def dispatch_forever(self, every_s: float = 2.0, stop: threading.Event | None = None, kind: str = "serve",
                         log=lambda m: print(m, file=sys.stderr)) -> None:
        stop = stop or threading.Event()
        me = (socket.gethostname(), os.getpid())
        waiting = False
        while not stop.is_set():
            other = self.dispatcher()
            if other and (other.get("host"), other.get("pid")) != me:
                if not waiting:
                    log(f"the queue is run by {other.get('kind')} (pid {other.get('pid')} on {other.get('host')}); "
                        "this process will take over if that one stops")
                    waiting = True
            else:
                waiting = False
                self._beat(kind)
                try:
                    self.start_next()
                except Exception as e:  # a bad job must not stop the queue
                    log(f"dispatcher: {type(e).__name__}: {e}")
            stop.wait(every_s)


# Started through a middle process that exits at once, a job is no longer a descendant of whatever started it, so
# stopping the GUI server, even by killing its whole process tree, leaves the job running. Its own session (its own
# process group on Windows, which CTRL_BREAK can reach) keeps it clear of Ctrl+C and a closed terminal too.
_LAUNCH = """
import os, subprocess, sys
kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
with open(sys.argv[1], "a", encoding="utf-8") as out:
    p = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **kw)
print(p.pid)
"""


def spawn_detached(cmd: list[str], log_path: Path, env: dict) -> int:
    r = subprocess.run([sys.executable, "-c", _LAUNCH, str(log_path), *cmd], env=env, capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"could not start the job: {r.stderr.strip()[-500:]}")
    return int(r.stdout.strip())


def _age_s(iso: str | None) -> float:
    if not iso:
        return float("inf")
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()


def run_job(cfg: MachineConfig, run_id: str) -> int:
    """Entry point of a job process: run the recipe (or the import or clear) the queue holds under this run id."""
    from .runner import Runner
    q = JobQueue(cfg)
    job = q.get(run_id)
    if not job:
        raise SystemExit(f"no job {run_id}")
    if job["backend"] == "io":
        return run_task(cfg, q, run_id, job)
    recipe = recipe_from_text(job["recipe_text"], Path(job["recipe_dir"]))
    rec = Runner(recipe, cfg, run_id=run_id).execute()
    return 0 if rec["status"] == "done" else 1


def run_task(cfg: MachineConfig, q: JobQueue, run_id: str, job: dict) -> int:
    import shutil as sh
    import yaml
    from . import ingest
    spec = yaml.safe_load(job["recipe_text"])
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            signal.signal(sig, lambda *_: stop.set())
    run_dir = q.store.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(run_dir / "log.txt", "a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()

    try:
        if spec["kind"] == "ingest":
            exiftool = cfg.tool("exiftool")
            rec = ingest.run_ingest(spec, run_dir, run_id, cancelled=stop.is_set, log=log,
                                    ffprobe=cfg.tool("ffprobe"), exiftool=exiftool if sh.which(exiftool) else None)
        elif spec["kind"] == "clear":
            rec = ingest.run_clear(spec, run_dir, run_id, cancelled=stop.is_set, log=log)
        elif spec["kind"] == "download":
            rec = run_downloads(cfg, spec, run_dir, run_id, log)
        else:
            raise ValueError(f"unknown task kind {spec['kind']!r}")
    except Exception as e:
        log(f"FAILED: {type(e).__name__}: {e}")
        rec_path = run_dir / "run.json"
        rec = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.is_file() else {
            "run_id": run_id, "name": job["name"], "status": "failed", "stages": [], "items": [], "cells": {},
            "created_utc": utc(), "host": socket.gethostname()}
        rec.update(status="failed", error=f"{type(e).__name__}: {e}", finished_utc=utc())
        write_json_atomic(rec_path, rec)
        return 1
    finally:
        log_file.close()
    return 0 if rec["status"] == "done" else 1


def run_downloads(cfg: MachineConfig, spec: dict, run_dir: Path, run_id: str, log) -> dict:
    """Download catalog models (the Setup tab's buttons), one row per model, each checked against its pinned hash."""
    import os
    from types import SimpleNamespace
    from . import bootstrap as b
    from .ingest import _Progress
    rows = [SimpleNamespace(rel=n) for n in spec["models"]]
    prog = _Progress(run_dir, run_id, spec.get("name") or "download models", "download", "Download and check", rows,
                     {"description": "models: " + ", ".join(spec["models"])})
    token = os.environ.get("HF_TOKEN") or cfg.secret_env().get("HF_TOKEN")
    root = cfg.models_root or b.home_dir() / "models"
    status = "done"
    for r in rows:
        prog.cell(r, "running")
        try:
            path = b.get_model(r.rel, root, token, cfg.python_for("audio"), log=log)
            prog.cell(r, "ran", checks="ok", note=path)
        except Exception as e:
            status = "failed"
            prog.cell(r, "failed", error=f"{type(e).__name__}: {e}")
            log(f"FAILED {r.rel}: {e}")
    prog.finish(status)
    return prog.rec
