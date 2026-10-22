"""Command line. The GUI calls the same functions.

    ontherecord doctor                     what this machine has: config, Python environments, tools, GPU
    ontherecord stages                     every stage, its settings and their defaults
    ontherecord plan  RECIPE               what would be reused and what would run
    ontherecord run   RECIPE               run it (Ctrl+C cancels cleanly)
    ontherecord show  [RUN_ID]             a run's grid of items x stages, with check results
    ontherecord receipt RUN_ID ITEM STAGE  the full receipt behind one cell
    ontherecord serve                      the GUI, at http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
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


def cmd_plan(args) -> int:
    recipe = load_recipe(args.recipe)
    runner = Runner(recipe, config_from(args))
    print(f"{recipe.name}: {len(recipe.items)} item(s)\n")
    print(grid([s.name for s in recipe.stages], runner.plan()))
    print("\n= reused   > runs   ? runs unless its inputs come out byte-identical   x blocked by a failed check")
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


def cmd_show(args) -> int:
    store = Store(config_from(args).workspace)
    rec = _run_record(store, args.run_id)
    print(f"{rec['run_id']}  {rec['status']}  on {rec['host']}  ({rec['created_utc']} -> {rec['finished_utc']})\n")
    print(grid([s["name"] for s in rec["stages"]], rec["cells"]))
    for item, row in rec["cells"].items():
        for stage, cell in row.items():
            if cell.get("receipt") and cell.get("checks") in ("warn", "fail"):
                r = json.loads((store.root / cell["receipt"]).read_text(encoding="utf-8"))
                for c in r["checks"]:
                    if c["status"] != "ok":
                        print(f"  {item} {stage}: {c['status'].upper()} {c['name']}: {c['message']}")
            if cell.get("status") in ("failed", "blocked"):
                print(f"  {item} {stage}: {cell['status']}: {(cell.get('error') or cell.get('reason') or '').strip()[-400:]}")
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
    serve(config_from(args), port=args.port)
    return 0


def cmd_run_job(args) -> int:
    from .jobs import run_job
    return run_job(config_from(args), args.run_id)


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
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("run")
    p.add_argument("recipe")
    p.add_argument("--run-id")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("show")
    p.add_argument("run_id", nargs="?")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("receipt")
    p.add_argument("run_id")
    p.add_argument("item")
    p.add_argument("stage")
    p.add_argument("--full", action="store_true", help="include whole prompt texts")
    p.set_defaults(fn=cmd_receipt)
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(fn=cmd_serve)
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
