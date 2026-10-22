"""How well a model drives the assistant's tools: run every case in assistant_cases.yaml and score it.

    python evals/assistant_eval.py                          # through the running GUI (its assistant model)
    python evals/assistant_eval.py --model gemma-4-E2B.gguf # a model on its own llama-server (machine.yaml settings)

Each case is one fresh conversation. Tools that start or stop work never run here: they only become buttons.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).parent


def check(case: dict, out: dict) -> tuple[bool, str]:
    calls = [(s["tool"], s["args"]) for s in out["steps"]]
    if case.get("no_tool"):
        return (not calls, "answered without tools" if not calls else f"called {calls}")
    for tool, args in calls:
        if tool != case["call"]:
            continue
        bad = []
        for k, want in (case.get("args") or {}).items():
            got = args.get(k)
            if isinstance(want, bool):
                ok = got is want or str(got).lower() == str(want).lower()
            elif isinstance(want, (int, float)):
                ok = isinstance(got, (int, float)) and abs(got - want) <= 2
            else:
                ok = want.lower() in json.dumps(got).lower()
            if not ok:
                bad.append(f"{k}={got!r}, wanted {want!r}")
        if not bad:
            return True, f"{tool} {json.dumps(args)}"
        return False, f"{tool} with " + "; ".join(bad)
    return False, f"never called {case['call']}; called {[c[0] for c in calls] or 'nothing'}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8765", help="a running GUI to ask (default)")
    ap.add_argument("--model", help="instead, run this model (relative to models_root) on its own server")
    ap.add_argument("--gpu-layers", type=int, default=0)
    ap.add_argument("--out", help="write the full results here (JSON)")
    ap.add_argument("--only", help="run only the cases whose request contains this text")
    args = ap.parse_args()
    cases = yaml.safe_load((HERE / "assistant_cases.yaml").read_text(encoding="utf-8"))
    if args.only:
        cases = [c for c in cases if args.only.lower() in c["ask"].lower()]
    if args.model:
        sys.path.insert(0, str(HERE.parent / "src"))
        from ontherecord.assistant import Assistant
        from ontherecord.config import load_config
        cfg = load_config()
        cfg.assistant = {**cfg.assistant, "model": args.model, "gpu_layers": args.gpu_layers}
        bot = Assistant(cfg)
        ask = lambda q: bot.chat([{"role": "user", "content": q}])  # noqa: E731
    else:
        def ask(q):
            req = urllib.request.Request(args.api + "/api/assistant", headers={"Content-Type": "application/json"},
                                         data=json.dumps({"messages": [{"role": "user", "content": q}]}).encode())
            return json.loads(urllib.request.urlopen(req, timeout=900).read())
    results, passed, t0 = [], 0, time.time()
    for c in cases:
        out = ask(c["ask"])
        ok, why = check(c, out)
        passed += ok
        results.append({**c, "ok": ok, "why": why, "reply": out["reply"], "seconds": out["seconds"]})
        print(f"{'PASS' if ok else 'FAIL'}  {out['seconds']:5.1f}s  {c['ask'][:60]:60}  {why[:90]}", flush=True)
    print(f"\n{passed}/{len(cases)} passed in {time.time() - t0:.0f} s")
    if args.model:
        bot.stop()
    if args.out:
        Path(args.out).write_text(json.dumps({"model": args.model or "gui", "passed": passed, "cases": results},
                                             indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
