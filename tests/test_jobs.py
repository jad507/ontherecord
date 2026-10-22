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


def test_only_one_dispatcher_runs_a_workspace(queue):
    import os
    import threading
    q, base = queue
    from ontherecord.jobs import write_json_atomic
    # another live process on this host holds the heartbeat: this one must wait, not start jobs
    write_json_atomic(q.cfg.workspace / "dispatcher.json",
                      {"pid": os.getppid(), "host": __import__("socket").gethostname(), "kind": "serve",
                       "t": time.time()})
    rid = q.submit(recipe(0.1), base)
    stop = threading.Event()
    t = threading.Thread(target=q.dispatch_forever, kwargs={"every_s": 0.2, "stop": stop, "log": lambda m: None})
    t.start()
    time.sleep(1.5)
    assert q.get(rid)["status"] == "queued"
    (q.cfg.workspace / "dispatcher.json").unlink()  # the other dispatcher is gone: this one takes over
    job = wait_for(q, rid, ("done", "failed"))
    stop.set()
    t.join()
    assert job["status"] == "done"
    assert q.dispatcher()["pid"] == os.getpid()


def test_cli_submit_wait_and_cancel(queue, capsys):
    import threading
    from ontherecord import cli
    q, base = queue
    (base / "r.yaml").write_text(recipe(0.1), encoding="utf-8")
    (base / "slow.yaml").write_text(recipe(120), encoding="utf-8")
    ws = ["--config", "none", "--workspace", str(q.cfg.workspace)]
    stop = threading.Event()
    t = threading.Thread(target=q.dispatch_forever, kwargs={"every_s": 0.2, "stop": stop, "log": lambda m: None})
    t.start()
    try:
        assert cli.main(ws + ["submit", str(base / "r.yaml"), "--wait", "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "done" and out["problems"] == []
        assert cli.main(ws + ["submit", str(base / "slow.yaml"), "--json"]) == 0
        rid = json.loads(capsys.readouterr().out)["run_id"]
        wait_for(q, rid, ("running",))
        assert cli.main(ws + ["cancel", rid, "--grace", "20"]) == 0
        assert capsys.readouterr().out.strip() == f"{rid}: cancelled"
        assert cli.main(ws + ["wait", rid]) == 3
        assert "cancelled" in capsys.readouterr().out
        assert cli.main(ws + ["jobs", "--json"]) == 0
        jobs = json.loads(capsys.readouterr().out)["jobs"]
        assert [j["status"] for j in jobs] == ["cancelled", "done"]
    finally:
        stop.set()
        t.join()


def test_job_outlives_a_tree_kill_of_its_dispatcher(queue):
    import subprocess
    import sys
    from ontherecord.runner import kill_tree
    q, base = queue
    rid = q.submit(recipe(120), base)
    code = ("import sys, time; from pathlib import Path; from ontherecord.config import MachineConfig; "
            "from ontherecord.jobs import JobQueue; JobQueue(MachineConfig(workspace=Path(sys.argv[1]))).start_next(); "
            "time.sleep(120)")
    dispatcher = subprocess.Popen([sys.executable, "-c", code, str(q.cfg.workspace)])
    deadline = time.time() + 30
    while time.time() < deadline and not q.get(rid)["pid"]:
        time.sleep(0.2)
    kill_tree(dispatcher.pid)  # what stopping a GUI server by its process tree does
    pid = q.get(rid)["pid"]
    assert pid and pid_alive(pid)
    assert q.cancel(rid, grace_s=20, wait=True)["status"] == "cancelled"
    assert not pid_alive(pid)


def test_the_same_recipe_twice_in_one_second_gets_two_run_ids(queue):
    q, base = queue
    ids = {q.submit(recipe(0.1), base) for _ in range(3)}
    assert len(ids) == 3
