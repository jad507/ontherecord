"""The job queue: jobs run one at a time in their own process, survive in SQLite, and cancel cleanly."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ontherecord.config import MachineConfig
from ontherecord.jobs import JobQueue, pid_alive

HERE = Path(__file__).parent


@pytest.fixture(autouse=True)
def test_stages(monkeypatch):
    monkeypatch.setenv("ONTHERECORD_STAGES", "upper=stages_for_tests:Upper;slow=stages_for_tests:Slow")
    monkeypatch.setenv("PYTHONPATH", str(HERE))
    monkeypatch.syspath_prepend(str(HERE))


@pytest.fixture
def queue(tmp_path):
    (tmp_path / "src.txt").write_text("hello", encoding="utf-8")
    return JobQueue(MachineConfig(workspace=tmp_path / "ws")), tmp_path


def recipe(seconds: float) -> str:
    return (f"name: q\nsource:\n  files: [src.txt]\nstages:\n  - download: {{}}\n  - upper: {{}}\n"
            f"  - slow: {{seconds: {seconds}}}\n")


def wait_for(q: JobQueue, run_id: str, statuses: tuple, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        q.reconcile()
        job = q.get(run_id)
        if job["status"] in statuses:
            return job
        time.sleep(0.3)
    raise AssertionError(f"{run_id} stayed {q.get(run_id)['status']}")


def test_job_runs_to_completion(queue):
    q, base = queue
    rid = q.submit(recipe(0.1), base)
    assert q.get(rid)["status"] == "queued"
    assert q.start_next() == rid
    assert q.start_next() is None  # one at a time
    job = wait_for(q, rid, ("done", "failed"))
    assert job["status"] == "done"
    rec = json.loads((q.store.run_dir(rid) / "run.json").read_text())
    assert {c["status"] for c in rec["cells"]["src"].values()} == {"ran"}


def test_cancel_queued_job_never_starts(queue):
    q, base = queue
    rid = q.submit(recipe(0.1), base)
    assert q.cancel(rid)["status"] == "cancelled"
    assert q.start_next() is None


def test_cancel_running_job(queue):
    q, base = queue
    rid = q.submit(recipe(120), base)
    q.start_next()
    store = q.store.store / "slow"
    deadline = time.time() + 60
    while time.time() < deadline and not list(store.glob("*/*/started")):
        time.sleep(0.3)
    pid = q.get(rid)["pid"]
    q.cancel(rid, grace_s=20)
    job = wait_for(q, rid, ("cancelled", "failed", "done"), timeout=40)
    assert job["status"] == "cancelled"
    assert not pid_alive(pid)
    rec = json.loads((q.store.run_dir(rid) / "run.json").read_text())
    assert rec["status"] == "cancelled"
    assert rec["cells"]["src"]["slow"]["status"] == "cancelled"
    assert not list(store.glob("*/*.partial-*"))


def test_bad_recipe_is_refused_before_queueing(queue):
    q, base = queue
    from ontherecord.recipe import RecipeError
    with pytest.raises(RecipeError):
        q.submit("name: x\nsource:\n  files: [src.txt]\nstages:\n  - upper: {}\n", base)
    assert q.list() == []
