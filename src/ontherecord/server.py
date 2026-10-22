"""The GUI's web server: a JSON API over the job queue, the store and the recipes, plus the page itself.

    ontherecord serve                 # http://127.0.0.1:8765
    ontherecord serve --port 9000

It listens on 127.0.0.1 only. Reach it from another machine through an SSH tunnel or a private network (Tailscale),
never by opening the port: the page can start jobs on this machine and shows research data.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from . import stages as registry
from .config import MachineConfig
from .hashing import sha256_text
from .jobs import JobQueue
from .library import Library
from .recipe import RecipeError, recipe_from_text, slug
from .runner import Runner
from .store import Store

STATIC = Path(__file__).parent / "static"
MEDIA = {".flac": "audio/flac", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
         ".opus": "audio/ogg", ".ogg": "audio/ogg", ".webm": "video/webm", ".mp4": "video/mp4", ".m4v": "video/mp4",
         ".mov": "video/quicktime", ".mkv": "video/x-matroska"}
REPO_RECIPES = Path(__file__).resolve().parents[2] / "recipes"


class RecipeIn(BaseModel):
    text: str
    name: str | None = None


class PromptIn(BaseModel):
    name: str
    text: str


def create_app(cfg: MachineConfig, dispatch: bool = True) -> FastAPI:
    app = FastAPI(title="On the Record")
    store = Store(cfg.workspace)
    queue = JobQueue(cfg)
    library = Library(store)
    recipes_dir = cfg.workspace / "recipes"
    prompts_dir = recipes_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    if dispatch:
        threading.Thread(target=queue.dispatch_forever, kwargs={"stop": stop, "kind": "serve"}, daemon=True).start()

    def recipe_paths() -> list[Path]:
        seen, out = set(), []
        for d in (recipes_dir, REPO_RECIPES):
            for p in sorted(d.glob("*.yaml")) if d.is_dir() else []:
                if p.name not in seen:
                    seen.add(p.name)
                    out.append(p)
        return out

    def in_workspace(rel: str) -> Path:
        p = (store.root / rel).resolve()
        if store.root.resolve() not in p.parents:
            raise HTTPException(400, "path outside the workspace")
        if not p.is_file():
            raise HTTPException(404, rel)
        return p

    def parse(text: str):
        try:
            return recipe_from_text(text, recipes_dir)
        except RecipeError as e:
            raise HTTPException(422, str(e))
        except Exception as e:  # YAML errors and the like, shown to the person editing
            raise HTTPException(422, f"{type(e).__name__}: {e}")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/static/{name}")
    def static(name: str):
        p = (STATIC / name).resolve()
        if p.parent != STATIC.resolve() or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p)

    @app.get("/api/machine")
    def machine():
        from .envinfo import gpu
        return {"config": cfg.describe(), "workspace": str(cfg.workspace), "gpu": gpu()}

    @app.get("/api/stages")
    def stages():
        return [registry.get(n).describe() for n in registry.names()]

    @app.get("/api/recipes")
    def recipes():
        out = []
        for p in recipe_paths():
            try:
                import yaml
                raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                raw = {}
            out.append({"file": p.name, "name": raw.get("name", p.stem), "description": raw.get("description", ""),
                        "builtin": p.parent == REPO_RECIPES})
        return out

    @app.get("/api/recipes/{file}", response_class=PlainTextResponse)
    def recipe(file: str):
        for p in recipe_paths():
            if p.name == file:
                return p.read_text(encoding="utf-8")
        raise HTTPException(404, file)

    @app.post("/api/recipes")
    def save_recipe(body: RecipeIn):
        r = parse(body.text)
        name = slug(body.name or r.name, 60) + ".yaml"
        recipes_dir.mkdir(parents=True, exist_ok=True)
        (recipes_dir / name).write_text(body.text, encoding="utf-8")
        return {"file": name}

    @app.get("/api/prompts")
    def prompts():
        from .recipe import PROMPTS_DIR
        out = [{"file": p.name, "text": p.read_text(encoding="utf-8"), "builtin": True}
               for p in sorted(PROMPTS_DIR.glob("*.txt"))]
        out += [{"file": f"prompts/{p.name}", "text": p.read_text(encoding="utf-8"), "builtin": False}
                for p in sorted(prompts_dir.glob("*.txt"))]
        return out

    @app.post("/api/prompts")
    def save_prompt(body: PromptIn):
        """Prompts are never edited in place: an edited prompt is saved under a new name carrying its hash, so a
        receipt that names a prompt file can always be read back as the text that run used."""
        name = f"{slug(Path(body.name).stem, 40)}-{sha256_text(body.text)[:8]}.txt"
        p = prompts_dir / name
        if not p.exists():
            p.write_text(body.text, encoding="utf-8")
        return {"file": f"prompts/{name}"}

    @app.post("/api/raw")
    def raw(body: RecipeIn):
        """The recipe as written (no defaults filled, globs unexpanded), for loading into the form."""
        import yaml
        try:
            return yaml.safe_load(body.text) or {}
        except yaml.YAMLError as e:
            raise HTTPException(422, f"YAML: {e}")

    @app.post("/api/validate")
    def validate(body: RecipeIn):
        r = parse(body.text)
        return {"name": r.name, "items": [i.as_dict() for i in r.items],
                "stages": [{**s.info(), "settings": s.record_settings} for s in r.stages]}

    @app.post("/api/plan")
    def plan(body: RecipeIn):
        r = parse(body.text)
        return {"stages": [s.info() for s in r.stages], "cells": Runner(r, cfg).plan()}

    @app.post("/api/jobs")
    def submit(body: RecipeIn):
        parse(body.text)
        return {"run_id": queue.submit(body.text, recipes_dir)}

    @app.get("/api/jobs")
    def jobs():
        out = []
        for j in queue.list():
            j.pop("recipe_text", None)
            rec = store.run_dir(j["run_id"]) / "run.json"
            if rec.is_file():
                cells = json.loads(rec.read_text(encoding="utf-8"))["cells"]
                counts: dict[str, int] = {}
                for row in cells.values():
                    for c in row.values():
                        counts[c["status"]] = counts.get(c["status"], 0) + 1
                j["cells"] = counts
            out.append(j)
        return out

    @app.post("/api/jobs/{run_id}/cancel")
    def cancel(run_id: str):
        try:
            return queue.cancel(run_id)
        except KeyError:
            raise HTTPException(404, run_id)

    @app.get("/api/runs")
    def runs():
        out = []
        for p in store.list_runs():
            r = json.loads(p.read_text(encoding="utf-8"))
            out.append({k: r.get(k) for k in ("run_id", "name", "status", "created_utc", "finished_utc", "host")}
                       | {"items": len(r["items"]), "stages": [s["name"] for s in r["stages"]]})
        return out

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        p = store.run_dir(run_id) / "run.json"
        if not p.is_file():
            job = queue.get(run_id)
            if job:
                return {"run_id": run_id, "status": job["status"], "name": job["name"], "stages": [], "cells": {},
                        "items": []}
            raise HTTPException(404, run_id)
        return json.loads(p.read_text(encoding="utf-8"))

    @app.get("/api/runs/{run_id}/log", response_class=PlainTextResponse)
    def log(run_id: str, tail: int = 400):
        p = store.run_dir(run_id) / "log.txt"
        if not p.is_file():
            p = store.run_dir(run_id) / "job_stdout.txt"
        if not p.is_file():
            return ""
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    @app.get("/api/receipt")
    def receipt(path: str):
        return json.loads(in_workspace(path).read_text(encoding="utf-8"))

    @app.get("/api/output")
    def output(receipt: str, name: str, limit: int = 2_000_000):
        """One output of a store entry, for previewing: JSON parsed, text as text, anything else as a download."""
        rpath = in_workspace(receipt)
        r = json.loads(rpath.read_text(encoding="utf-8"))
        out = r["outputs"].get(name)
        if not out:
            raise HTTPException(404, name)
        p = Path(out["file"]) if out.get("external") else rpath.parent / out["file"]
        if p.suffix in (".json",) and p.stat().st_size <= limit:
            return json.loads(p.read_text(encoding="utf-8"))
        if p.suffix in (".txt", ".csv", ".rttm", ".srt", ".vtt") and p.stat().st_size <= limit:
            return PlainTextResponse(p.read_text(encoding="utf-8", errors="replace"))
        if p.suffix.lower() in MEDIA:  # streamed, with range requests, so the player can scrub
            return FileResponse(p, media_type=MEDIA[p.suffix.lower()], content_disposition_type="inline")
        return FileResponse(p, filename=p.name)

    # ------------------------------------------------------------------ library and player
    @app.get("/api/library")
    def recordings():
        return library.recordings()

    @app.get("/api/recording")
    def recording(run: str, item: str):
        try:
            return library.recording(run, item)
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/search")
    def search(q: str, limit: int = 200):
        return library.search(q, limit)

    app.state.stop_dispatch = stop
    return app


def serve(cfg: MachineConfig, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn
    print(f"On the Record: http://{host}:{port}   (workspace {cfg.workspace})")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")
