"""The job queue behind the GUI: submit a recipe, run jobs one at a time, cancel them.

Jobs live in SQLite in the workspace (`jobs.db`), so the queue survives the GUI being closed or restarted. Each job
runs as its own process (`python -m ontherecord.cli run-job RUN_ID`), started in a new process group, so it keeps
going when the browser tab closes and can be cancelled as a unit.

One job runs at a time per machine: on a single GPU, two models rarely fit at once, and timings from overlapping
jobs would not mean much. Cancel asks the job to stop (SIGTERM on Linux and macOS, CTRL_BREAK on Windows), so the
runner kills its worker, removes partial outputs and records "cancelled"; if the job has not stopped after a grace
period, the whole process tree is killed and the record is corrected here.
"""
from __future__ import annotations

import json
import os
import signal
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
        self._procs: dict[str, subprocess.Popen] = {}
        with self._db() as db:
            db.execute(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    # ------------------------------------------------------------------ submit / list
    def submit(self, recipe_text: str, recipe_dir: Path, backend: str = "local") -> str:
        recipe = recipe_from_text(recipe_text, recipe_dir)  # raises RecipeError before anything is queued
        run_id = new_run_id(recipe.name)
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
    def cancel(self, run_id: str, grace_s: float = 60) -> dict:
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
            threading.Thread(target=self._hard_stop_later, args=(run_id, pid, grace_s), daemon=True).start()
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
        """Start the oldest queued job if nothing is running. Returns its run id."""
        with self._lock:
            self.reconcile()
            with self._db() as db:
                if db.execute("select 1 from jobs where status = 'running' and backend = 'local'").fetchone():
                    return None
                row = db.execute("select * from jobs where status = 'queued' and backend = 'local' "
                                 "order by created_utc limit 1").fetchone()
            if not row:
                return None
            run_id = row["run_id"]
            run_dir = self.store.run_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "submitted_recipe.yaml").write_text(row["recipe_text"], encoding="utf-8")
            cmd = [sys.executable, "-m", "ontherecord.cli",
                   "--config", str(self.cfg.source) if self.cfg.source else "none",
                   "--workspace", str(self.cfg.workspace), "run-job", run_id]
            kw: dict = {}
            if os.name == "nt":
                kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kw["start_new_session"] = True
            env = {**os.environ, "PYTHONUNBUFFERED": "1",
                   "PYTHONPATH": os.pathsep.join([str(SRC_DIR), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}
            out = open(run_dir / "job_stdout.txt", "a", encoding="utf-8")
            proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, env=env, **kw)
            out.close()
            self._procs[run_id] = proc
            self._set(run_id, status="running", started_utc=utc(), pid=proc.pid)
            return run_id

    def reconcile(self) -> None:
        """Bring 'running' jobs whose process has ended up to date from their run record."""
        with self._db() as db:
            rows = db.execute("select run_id, pid from jobs where status = 'running' and backend = 'local'").fetchall()
        for r in rows:
            proc = self._procs.get(r["run_id"])
            if proc is not None:
                if proc.poll() is None:
                    continue
                self._procs.pop(r["run_id"], None)
            elif pid_alive(r["pid"]):
                continue
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

    def dispatch_forever(self, every_s: float = 2.0, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.start_next()
            except Exception as e:  # a bad job must not stop the queue
                print(f"dispatcher: {type(e).__name__}: {e}", file=sys.stderr)
            stop.wait(every_s)


def run_job(cfg: MachineConfig, run_id: str) -> int:
    """Entry point of a job process: run the recipe the queue holds under this run id."""
    from .runner import Runner
    q = JobQueue(cfg)
    job = q.get(run_id)
    if not job:
        raise SystemExit(f"no job {run_id}")
    recipe = recipe_from_text(job["recipe_text"], Path(job["recipe_dir"]))
    rec = Runner(recipe, cfg, run_id=run_id).execute()
    return 0 if rec["status"] == "done" else 1
