"""Command line. The GUI calls the same functions.

    ontherecord doctor                     what this machine has: config, Python environments, tools, GPU
    ontherecord stages                     every stage, its settings and their defaults
    ontherecord plan  RECIPE               what would be reused and what would run
    ontherecord run   RECIPE               run it here and now, in the foreground (Ctrl+C cancels cleanly)
    ontherecord show  [RUN_ID]             a run's grid of items x stages, with check results
    ontherecord receipt RUN_ID ITEM STAGE  the full receipt behind one cell
    ontherecord transcribe FILE|LINK...    queue speaker-labelled transcripts, set for this machine (no recipe needed)

  The job queue (the same one the GUI uses; one job at a time per workspace):
    ontherecord submit RECIPE [--wait]     queue a run; prints its run id
    ontherecord jobs                       queued, running and finished jobs
    ontherecord wait  RUN_ID               block until a job ends; exit 0 done, 1 failed, 3 cancelled
    ontherecord cancel RUN_ID              cancel a queued or running job, and wait until it has stopped
    ontherecord serve                      the GUI at http://127.0.0.1:8765, which also runs the queue
    ontherecord dispatch                   run the queue without the GUI
    ontherecord exclusive -- CMD ARGS      run a command as a queued job, alone on the machine

  Imports from cameras and drives (queued jobs, like everything else):
    ontherecord devices                    removable drives and camera cards plugged in
    ontherecord ingest SOURCE --project P  copy, verify, name and file everything into the archive
    ontherecord clear IMPORT_RUN_ID        then delete those files from the device, each re-checked first

  plan, show, jobs, submit and wait take --json for output a script or an agent can parse.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import stages as registry
from .config import MachineConfig, load_config
from .recipe import RecipeError, load_recipe
from .runner import Runner
from .store import Store

MARK = {"cached": "=", "ran": "+", "run": ">", "run-if-changed": "?", "blocked": "x", "failed": "!",
        "cancelled": "-", "pending": ".", "queued": ".", "running": "*"}
SHORT = {"run-if-changed": "maybe", "cached": "reused"}


def config_from(args) -> MachineConfig:
    """--config FILE, or --config none for the built-in defaults; --workspace overrides either."""
    cfg = MachineConfig() if args.config == "none" else load_config(args.config)
    if args.workspace:
        cfg.workspace = Path(args.workspace).expanduser()
    return cfg


def grid(stage_names: list[str], rows: dict[str, dict]) -> str:
    w = max([len(i) for i in rows] + [4])
    cols = [max(len(s), 15) for s in stage_names]
    lines = [" " * w + "  " + "  ".join(s.ljust(c) for s, c in zip(stage_names, cols))]
    for item, row in rows.items():
        cells = []
        for s, c in zip(stage_names, cols):
            cell = row.get(s, {})
            st = cell.get("status", "")
            txt = f"{MARK.get(st, ' ')} {SHORT.get(st, st)}"
            if cell.get("checks") in ("warn", "fail"):
                txt += f" [{cell['checks']}]"
            cells.append(txt.ljust(c))
        lines.append(item.ljust(w) + "  " + "  ".join(cells))
    return "\n".join(lines)


def cmd_doctor(args) -> int:
    from .envinfo import gpu, tool_version
    cfg = config_from(args)
    print(f"machine config : {cfg.source or '(none: defaults)'}")
    print(f"workspace      : {cfg.workspace}")
    print(f"gpu            : {gpu()}")
    for t in ("ffmpeg", "ffprobe", "llama_server"):
        path = cfg.tool(t)
        flag = "-version" if t.startswith("ff") else "--version"
        print(f"{t:15}: {path}  ->  {tool_version([path, flag]) or 'NOT FOUND'}")
    groups = sorted({registry.get(n).group for n in registry.names()})
    for g in groups:
        py = cfg.python_for(g)
        mods = sorted({m for n in registry.names() if registry.get(n).group == g
                       for m in registry.get(n).packages})
        probe = ("import importlib.metadata as m, json\nout = {}\nfor p in %r:\n    try: out[p] = m.version(p)\n"
                 "    except Exception: out[p] = None\nprint(json.dumps(out))" % mods)
        try:
            r = subprocess.run([py, "-c", probe], capture_output=True, text=True, timeout=60)
            found = json.loads(r.stdout) if r.returncode == 0 else {"error": r.stderr.strip()[-200:]}
        except OSError as e:
            found = {"error": str(e)}
        print(f"env {g:11}: {py}")
        for k, v in found.items():
            print(f"    {k:18} {v or 'MISSING'}")
    return 0


def cmd_stages(args) -> int:
    for n in registry.names():
        d = registry.get(n).describe()
        print(f"\n{n}  ({d['label']}, v{d['version']}, env {d['group']}{', GPU' if d['gpu'] else ''})")
        print(f"  needs {d['needs'] or '-'}  makes {d['makes']}")
        for k, s in d["settings"].items():
            extra = f"  one of {s['choices']}" if s["choices"] else ""
            print(f"  {k:28} = {s['default']!r:<12} {s['help']}{extra}")
    return 0


LEGEND = "= reused   > runs   ? runs unless its inputs come out byte-identical   x blocked by a failed check"


def cmd_plan(args) -> int:
    recipe = load_recipe(args.recipe)
    cells = Runner(recipe, config_from(args)).plan()
    if args.json:
        print(json.dumps({"name": recipe.name, "stages": [s.info() for s in recipe.stages], "cells": cells},
                         indent=1))
        return 0
    print(f"{recipe.name}: {len(recipe.items)} item(s)\n")
    print(grid([s.name for s in recipe.stages], cells))
    print("\n" + LEGEND)
    for s in recipe.stages:
        if s.mixed:
            print(f"\n! {s.name} takes inputs that trace back to different stages making the same artifact: "
                  + "; ".join(f"{a} from {', '.join(steps)}" for a, steps in s.mixed.items())
                  + ". If those files differ, its same_source check fails"
                  + (" and it runs anyway (on_fail: continue)." if s.on_fail == "continue" else " and it is blocked."))
    return 0


def cmd_run(args) -> int:
    recipe = load_recipe(args.recipe)
    runner = Runner(recipe, config_from(args), run_id=args.run_id)
    rec = runner.execute()
    print()
    print(grid([s["name"] for s in rec["stages"]], rec["cells"]))
    print(f"\nrun {rec['run_id']}: {rec['status']}   ({runner.dir})")
    return 0 if rec["status"] == "done" else 1


def _run_record(store: Store, run_id: str | None) -> dict:
    runs = store.list_runs()
    if not runs:
        raise SystemExit("no runs in this workspace yet")
    path = store.run_dir(run_id) / "run.json" if run_id else runs[0]
    return json.loads(path.read_text(encoding="utf-8"))


def problems(store: Store, rec: dict) -> list[dict]:
    """Every warn or fail check and every failed or blocked cell in a run, flattened."""
    out = []
    for item, row in rec["cells"].items():
        for stage, cell in row.items():
            checks = list(cell.get("run_checks", []))
            if cell.get("receipt") and cell.get("checks") in ("warn", "fail"):
                checks += json.loads((store.root / cell["receipt"]).read_text(encoding="utf-8"))["checks"]
            for c in checks:
                if c["status"] != "ok":
                    out.append({"item": item, "stage": stage, "kind": "check", "status": c["status"],
                                "name": c["name"], "message": c["message"]})
            if cell.get("status") in ("failed", "blocked"):
                out.append({"item": item, "stage": stage, "kind": cell["status"], "status": cell["status"],
                            "message": (cell.get("error") or cell.get("reason") or "").strip()[-400:]})
    return out


def cmd_show(args) -> int:
    store = Store(config_from(args).workspace)
    rec = _run_record(store, args.run_id)
    found = problems(store, rec)
    if args.json:
        print(json.dumps({**rec, "problems": found}, indent=1, ensure_ascii=False))
        return 0
    print(f"{rec['run_id']}  {rec['status']}  on {rec['host']}  ({rec['created_utc']} -> {rec['finished_utc']})\n")
    print(grid([s["name"] for s in rec["stages"]], rec["cells"]))
    for p in found:
        label = f"{p['status'].upper()} {p['name']}" if p["kind"] == "check" else p["status"]
        print(f"  {p['item']} {p['stage']}: {label}: {p['message']}")
    return 0


def cmd_receipt(args) -> int:
    store = Store(config_from(args).workspace)
    rec = _run_record(store, args.run_id)
    cell = rec["cells"][args.item][args.stage]
    if not cell.get("receipt"):
        raise SystemExit(f"no receipt: {cell}")
    r = json.loads((store.root / cell["receipt"]).read_text(encoding="utf-8"))
    if not args.full:
        for v in r["settings"].values():
            if isinstance(v, dict) and "text" in v:
                v["text"] = v["text"][:120] + ("..." if len(v["text"]) > 120 else "")
    print(json.dumps(r, indent=1, ensure_ascii=False))
    return 0


def cmd_serve(args) -> int:
    from .server import serve
    serve(config_from(args), host=args.host, port=args.port, open_browser=args.open)
    return 0


def cmd_run_job(args) -> int:
    from .jobs import run_job
    return run_job(config_from(args), args.run_id)


def cmd_dispatch(args) -> int:
    from .jobs import JobQueue
    q = JobQueue(config_from(args))
    print(f"running the job queue in {q.cfg.workspace} (Ctrl+C stops the dispatcher; a running job carries on)",
          file=sys.stderr)
    try:
        q.dispatch_forever(kind="dispatch")
    except KeyboardInterrupt:
        pass
    return 0


def cmd_submit(args) -> int:
    from .jobs import JobQueue
    q = JobQueue(config_from(args))
    path = Path(args.recipe).resolve()
    run_id = q.submit(path.read_text(encoding="utf-8"), path.parent)
    runner = q.dispatcher()
    if args.json and not args.wait:
        print(json.dumps({"run_id": run_id, "dispatcher": runner}))
    elif not args.json:
        print(run_id)
    if not runner:
        print("note: nothing is running this workspace's queue, so the job will wait. Start `ontherecord serve` "
              "(the GUI) or `ontherecord dispatch`.", file=sys.stderr)
    return _wait(q, run_id, args.json) if args.wait else 0


def cmd_transcribe(args) -> int:
    """Queue the ready-made transcript recipe (presets.py) for links and files, with this machine's settings."""
    from . import presets
    from .jobs import JobQueue
    cfg = config_from(args)
    missing = presets.missing_models(cfg, args.comments)
    if missing:
        print(f"first download {', '.join(missing)}: `ontherecord models get {' '.join(missing)}` (the speaker model "
              "needs a Hugging Face token; see INSTALL.md), or use the Setup tab", file=sys.stderr)
        return 2
    sources = [s if presets.is_link(s) else str(Path(s).expanduser().resolve()) for s in args.sources]
    text = presets.transcript_recipe(sources, args.comments, "" if args.detect_language else args.language)
    if args.show:
        print(text, end="")
        return 0
    q = JobQueue(cfg)
    run_id = q.submit(text, cfg.workspace / "recipes")
    _announce(run_id, args)
    if not q.dispatcher():
        print("note: nothing is running this workspace's queue, so the job will wait. Start `ontherecord serve` "
              "(the GUI) or `ontherecord dispatch`.", file=sys.stderr)
    return _wait(q, run_id, args.json) if args.wait else 0


def cmd_exclusive(args) -> int:
    """Queue a command as a job of its own and wait for it: it starts only when nothing else is running, and nothing
    starts beside it. For agents and scripts doing heavy work (an evaluation, a conversion) on a machine that the
    queue may be using."""
    from .jobs import JobQueue
    argv = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not argv:
        raise SystemExit("usage: ontherecord exclusive -- COMMAND [ARGS...]")
    q = JobQueue(config_from(args))
    run_id = q.submit_task("command", {"argv": argv, "cwd": os.getcwd()}, f"command {Path(argv[0]).name}")
    print(run_id, file=sys.stderr)
    if not q.dispatcher():
        print("note: nothing is running this workspace's queue, so the command will wait. Start `ontherecord serve` "
              "(the GUI) or `ontherecord dispatch`.", file=sys.stderr)
    _wait(q, run_id, as_json=False, show=False)
    log = q.store.run_dir(run_id) / "log.txt"
    if log.is_file():
        sys.stdout.write(log.read_text(encoding="utf-8", errors="replace"))
    rec_path = q.store.run_dir(run_id) / "run.json"
    rec = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.is_file() else {}
    return 3 if rec.get("status") == "cancelled" else int(rec.get("exit_code", 1) or 0) if rec else 1


def cmd_wait(args) -> int:
    from .jobs import JobQueue
    return _wait(JobQueue(config_from(args)), args.run_id, args.json)


EXIT = {"done": 0, "failed": 1, "cancelled": 3}


def _wait(q, run_id: str, as_json: bool, show: bool = True) -> int:
    """Follow a job until it ends, with one progress line on stderr whenever its cells change."""
    from .jobs import FINAL
    last, warned = None, False
    while True:
        job = q.get(run_id)
        if not job:
            raise SystemExit(f"no job {run_id}")
        rec_path = q.store.run_dir(run_id) / "run.json"
        rec = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.is_file() else None
        if rec:
            counts: dict[str, int] = {}
            for row in rec["cells"].values():
                for c in row.values():
                    counts[c["status"]] = counts.get(c["status"], 0) + 1
            now = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
            if now != last:
                print(f"[{time.strftime('%H:%M:%S')}] {run_id}: {job['status']}: {now}", file=sys.stderr)
                last = now
        if job["status"] in FINAL:
            break
        if job["status"] == "queued" and not q.dispatcher() and not warned:
            print("note: nothing is running the queue; start `ontherecord serve` or `ontherecord dispatch`",
                  file=sys.stderr)
            warned = True
        time.sleep(5)
        q.reconcile()
    if rec and as_json:
        print(json.dumps({**rec, "problems": problems(q.store, rec)}, indent=1, ensure_ascii=False))
    elif rec and show:
        print(grid([s["name"] for s in rec["stages"]], rec["cells"]))
        for p in problems(q.store, rec):
            label = f"{p['status'].upper()} {p['name']}" if p["kind"] == "check" else p["status"]
            print(f"  {p['item']} {p['stage']}: {label}: {p['message']}")
        print(f"\nrun {run_id}: {job['status']}")
    return EXIT.get(job["status"], 1)


def cmd_jobs(args) -> int:
    from .jobs import JobQueue
    q = JobQueue(config_from(args))
    jobs = q.list(limit=args.limit)
    for j in jobs:
        j.pop("recipe_text", None)
        rec_path = q.store.run_dir(j["run_id"]) / "run.json"
        cells = json.loads(rec_path.read_text(encoding="utf-8"))["cells"] if rec_path.is_file() else {}
        flat = [c["status"] for row in cells.values() for c in row.values()]
        j["cells_total"], j["cells_finished"] = len(flat), sum(s in ("ran", "cached") for s in flat)
    if args.json:
        print(json.dumps({"dispatcher": q.dispatcher(), "jobs": jobs}, indent=1))
        return 0
    d = q.dispatcher()
    print(f"queue run by {d['kind']} (pid {d['pid']} on {d['host']})" if d else "nothing is running the queue")
    for j in jobs:
        prog = f"{j['cells_finished']}/{j['cells_total']}" if j["cells_total"] else ""
        print(f"  {j['run_id']:44} {j['status']:10} {prog:>8}  {j['created_utc']}")
    return 0


def _announce(run_id: str, args) -> None:
    """The new run id; with --wait --json, only the final record is printed, as one JSON document."""
    if not args.json:
        print(run_id)
    elif not args.wait:
        print(json.dumps({"run_id": run_id}))


def cmd_devices(args) -> int:
    from .ingest import volumes
    vols = volumes()
    if args.json:
        print(json.dumps(vols, indent=1))
        return 0
    for v in vols:
        print(f"{v['mount']:40} {v['label']:20} {v['used'] / 1e9:7.1f} GB used{'  (camera card: has DCIM)' if v['camera'] else ''}")
    if not vols:
        print("no removable drives or cards are mounted")
    return 0


def cmd_ingest(args) -> int:
    """Queue an import: copy, verify and file everything from a card or folder into the archive."""
    from .ingest import DEFAULT_TEMPLATE
    from .jobs import JobQueue
    cfg = config_from(args)
    q = JobQueue(cfg)
    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        raise SystemExit(f"not a folder: {source}")
    spec = {"source": str(source), "archive": str(Path(args.archive).expanduser() if args.archive else cfg.archive),
            "project": args.project, "template": args.template or DEFAULT_TEMPLATE}
    if args.kinds:
        spec["kinds"] = args.kinds.split(",")
    run_id = q.submit_task("ingest", spec, f"import {source.name or source}")
    _announce(run_id, args)
    return _wait(q, run_id, args.json) if args.wait else 0


def cmd_clear(args) -> int:
    """Queue the clearing of a device after a finished import (only if every file was verified)."""
    from .ingest import clearable
    from .jobs import JobQueue
    q = JobQueue(config_from(args))
    rec_path = q.store.run_dir(args.import_run) / "run.json"
    if not rec_path.is_file():
        raise SystemExit(f"no import {args.import_run}")
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    if rec.get("kind") != "ingest" or not rec.get("manifest"):
        raise SystemExit(f"{args.import_run} is not an import")
    ok, why = clearable(json.loads(Path(rec["manifest"]).read_text(encoding="utf-8")))
    if not ok:
        raise SystemExit(f"not clearing: {why}")
    print(why, file=sys.stderr)
    run_id = q.submit_task("clear", {"manifest": rec["manifest"], "ingest_run": args.import_run},
                           f"clear {rec['ingest']['volume']}")
    _announce(run_id, args)
    return _wait(q, run_id, args.json) if args.wait else 0


def cmd_ask(args) -> int:
    """One question to the assistant, from the command line (the Ask tab does the same)."""
    from .assistant import Assistant
    a = Assistant(config_from(args))
    try:
        out = a.chat([{"role": "user", "content": args.question}])
    finally:
        a.stop()
    if args.json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0
    for s in out["steps"]:
        print(f"  [{s['tool']} {json.dumps(s['args'], ensure_ascii=False)}]", file=sys.stderr)
    for p in out["pending"]:
        print(f"  (waiting for confirmation: {p['tool']} {json.dumps(p['args'], ensure_ascii=False)})", file=sys.stderr)
    print(out["reply"])
    print(f"  ({out['seconds']} s)", file=sys.stderr)
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import serve_mcp
    serve_mcp(config_from(args))
    return 0


def cmd_setup(args) -> int:
    """What this machine has and can run; fetch ffmpeg and llama-server if missing; write a machine config."""
    from . import bootstrap as b
    from .config import user_config_dir
    hw = b.hardware()
    choice = b.fit(hw)
    acc = b.accel(hw, config_from(args).python_for("audio"))
    gpu = hw["gpu"]
    print(f"machine   : {hw['os']} {hw['arch']}, {hw['cpus']} processor threads, {hw['ram_gb']} GB RAM, "
          f"{hw['disk_free_gb']} GB free disk")
    for g in hw["gpus"] or [None]:
        print("graphics  : " + (f"{g['name']}" + (f", {g['memory_gb']} GB ({g['free_gb']} GB free now), compute "
                                                  f"capability {g['compute_cap']}, driver {g['driver']} (CUDA "
                                                  f"{g['cuda']})" if g["vendor"] == "nvidia" else "")
                                if g else "no graphics card found"))
    if gpu and gpu.get("vendor") == "apple":
        print(f"graphics  : {gpu['name']}")
    print(f"fits      : assistant {choice['assistant']}, comment extraction {choice['extraction']}, "
          f"Whisper {choice['whisper']} ({choice['whisper_compute']})")
    t = acc["torch"]
    print(f"PyTorch   : {t['installed'] or 'not in the audio environment'}"
          + (f" ({t['installed_build']})" if t["installed_build"] else "")
          + f"; this machine suits {t['want']} ({t['why']})")
    if not t["ok"]:
        print(f"            to switch an installer-made environment: `uv sync --extra all --frozen "
              f"--no-default-groups --group {t['want']}` in {Path(__file__).resolve().parents[2]}")
    print(f"llama.cpp : {acc['llama']['want']} ({acc['llama']['why']})")
    if acc["driver"]:
        print(f"driver    : {acc['driver']['level'].upper()}: {acc['driver']['text']}")
    home = b.home_dir()
    cfg_path = Path(args.config_file) if args.config_file else user_config_dir() / "machine.yaml"
    if args.check:
        cfg = config_from(args)
        for t in ("ffmpeg", "ffprobe", "llama_server"):
            p = cfg.tool(t)
            print(f"{t:10}: {p if Path(p).is_file() or shutil.which(p) else 'MISSING'}")
        status = b.model_status(cfg.models_root or home / "models")
        for name, ok in status.items():
            print(f"{name:21}: {ok if isinstance(ok, str) else 'downloaded' if ok else 'not downloaded'}")
        return 0
    bin_dir = home / "bin"
    tools = b.ensure_ffmpeg(bin_dir, hw)
    print(f"ffmpeg    : {tools['ffmpeg']}")
    tools["llama_server"] = str(b.ensure_llama(bin_dir, hw))
    print(f"llama     : {tools['llama_server']}")
    print(b.write_config(cfg_path, hw, tools, choice, home))
    for d in ("workspace", "Archive", "models"):
        (home / d).mkdir(parents=True, exist_ok=True)
    if not args.install:
        print(f"\nnext: `ontherecord models` lists the models; `ontherecord models get NAME` downloads one.")
        return 0
    from .config import load_config
    cfg = load_config(cfg_path)
    b.interactive_models(cfg.models_root or home / "models", choice, cfg.env_file or cfg_path.parent / "secrets.env",
                         cfg.python_for("audio"))
    exe = Path(sys.executable).parent / ("ontherecord.exe" if os.name == "nt" else "ontherecord")
    icons = Path(__file__).resolve().parents[2] / "install"
    print(f"\nShortcut: {b.make_shortcut(exe, icons, hw['os'])}")
    print("Done. Open On the Record from the shortcut on your desktop.")
    return 0


def cmd_models(args) -> int:
    from . import bootstrap as b
    cfg = config_from(args)
    root = cfg.models_root or b.home_dir() / "models"
    if args.action == "get":
        token = args.token or os.environ.get("HF_TOKEN") or cfg.secret_env().get("HF_TOKEN")
        if args.token and args.save_token:
            from .config import user_config_dir
            print(f"saved the token in {b.save_token(cfg.env_file or user_config_dir() / 'secrets.env', args.token)}")
        for name in args.names:
            if name not in b.MODELS:
                raise SystemExit(f"no model {name!r}; the catalog: {', '.join(b.MODELS)}")
            print(b.get_model(name, root, token, cfg.python_for("audio")))
        return 0
    status = b.model_status(root)
    want = b.recommended_models(b.fit(b.hardware()))
    for name, m in b.MODELS.items():
        chosen = name in want
        print(f"{'*' if chosen else ' '} {name:21} {m['bytes'] / 1e9:5.1f} GB  "
              f"{(status[name] if isinstance(status[name], str) else 'downloaded' if status[name] else 'not downloaded'):15} {m['about']}")
    print("\n* = what this machine should use. `ontherecord models get NAME [NAME...]` downloads; gated models need "
          "--token (from https://huggingface.co/settings/tokens) after accepting their terms.")
    return 0


def cmd_cancel(args) -> int:
    from .jobs import JobQueue
    q = JobQueue(config_from(args))
    try:
        job = q.cancel(args.run_id, grace_s=args.grace, wait=True)
    except KeyError:
        raise SystemExit(f"no job {args.run_id}")
    print(f"{args.run_id}: {job['status']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ontherecord", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="machine config file, or 'none' for defaults (default: see config.py)")
    ap.add_argument("--workspace", help="override the machine config's workspace")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("stages").set_defaults(fn=cmd_stages)
    p = sub.add_parser("plan")
    p.add_argument("recipe")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("run")
    p.add_argument("recipe")
    p.add_argument("--run-id")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("show")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--json", action="store_true", help="the whole run record, plus a flat list of problems")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("receipt")
    p.add_argument("run_id")
    p.add_argument("item")
    p.add_argument("stage")
    p.add_argument("--full", action="store_true", help="include whole prompt texts")
    p.set_defaults(fn=cmd_receipt)
    p = sub.add_parser("transcribe", help="queue speaker-labelled transcripts of links or files, with the settings "
                                          "that suit this machine (no recipe needed)")
    p.add_argument("sources", nargs="+", help="video or audio links, or files (globs are fine)")
    p.add_argument("--comments", action="store_true", help="also list the public comments (a large language model)")
    p.add_argument("--language", default=None, help="language code (default: the transcribe stage's, en)")
    p.add_argument("--detect-language", action="store_true", help="let Whisper detect the language")
    p.add_argument("--show", action="store_true", help="print the recipe instead of queueing it")
    p.add_argument("--wait", action="store_true", help="then follow it until it ends (exit code as for wait)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_transcribe)
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1", help="only change this inside a container (see docker/)")
    p.add_argument("--open", action="store_true", help="open the GUI in the browser once it is up")
    p.set_defaults(fn=cmd_serve)
    sub.add_parser("dispatch", help="run the job queue without the GUI").set_defaults(fn=cmd_dispatch)
    p = sub.add_parser("submit", help="queue a recipe as a job")
    p.add_argument("recipe")
    p.add_argument("--wait", action="store_true", help="then follow it until it ends (exit code as for wait)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_submit)
    p = sub.add_parser("exclusive", help="run a command as a queued job, alone on the machine; prints its output and "
                                         "exits with its exit code")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...]")
    p.set_defaults(fn=cmd_exclusive)
    p = sub.add_parser("wait", help="block until a job ends; exit 0 done, 1 failed, 3 cancelled")
    p.add_argument("run_id")
    p.add_argument("--json", action="store_true", help="print the final run record, plus a flat list of problems")
    p.set_defaults(fn=cmd_wait)
    p = sub.add_parser("jobs", help="the job queue")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_jobs)
    p = sub.add_parser("devices", help="removable drives and camera cards that are plugged in")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_devices)
    p = sub.add_parser("ingest", help="copy, verify and file a card or folder into the archive (a queued job)")
    p.add_argument("source", help="the card or folder to import, e.g. /media/me/EOS_DIGITAL or E:\\")
    p.add_argument("--project", default="Project", help="the archive's top folder for this material")
    p.add_argument("--archive", help="default: the machine config's archive")
    p.add_argument("--template", help="file naming template; see ontherecord.ingest.render")
    p.add_argument("--kinds", help="comma-separated: photo,video,audio,sidecar,other (default: all but other)")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("clear", help="after an import, delete its verified files from the device (a queued job)")
    p.add_argument("import_run", help="the run id of the finished import")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_clear)
    p = sub.add_parser("ask", help="ask the assistant a question in plain English")
    p.add_argument("question")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ask)
    sub.add_parser("mcp", help="serve the assistant's tools to an MCP client (Claude Desktop, Claude Code) "
                              "over stdio").set_defaults(fn=cmd_mcp)
    p = sub.add_parser("setup", help="check this machine, fetch ffmpeg and llama-server, write a machine config")
    p.add_argument("--check", action="store_true", help="only report what is there")
    p.add_argument("--config-file", help="where to write the machine config (default: the user config folder)")
    p.add_argument("--install", action="store_true",
                   help="also offer the models this machine needs and put a shortcut on the desktop (the installers)")
    p.set_defaults(fn=cmd_setup)
    p = sub.add_parser("models", help="the model catalog; `models get NAME` downloads one")
    p.add_argument("action", nargs="?", choices=["list", "get"], default="list")
    p.add_argument("names", nargs="*")
    p.add_argument("--token", help="a Hugging Face token, for gated models")
    p.add_argument("--save-token", action="store_true", help="keep the token in the machine's secrets file")
    p.set_defaults(fn=cmd_models)
    p = sub.add_parser("cancel", help="cancel a job and wait until it has stopped")
    p.add_argument("run_id")
    p.add_argument("--grace", type=float, default=60, help="seconds to let it stop before killing it")
    p.set_defaults(fn=cmd_cancel)
    p = sub.add_parser("run-job", help="(used by the job queue) run a queued job")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_run_job)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except RecipeError as e:
        print(f"recipe error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
