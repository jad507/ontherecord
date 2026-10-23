"""The GUI's web server: a JSON API over the job queue, the store and the recipes, plus the page itself.

    ontherecord serve                 # http://127.0.0.1:8765
    ontherecord serve --port 9000

It listens on 127.0.0.1 only. Reach it from another machine through an SSH tunnel or a private network (Tailscale),
never by opening the port: the page can start jobs on this machine and shows research data.
"""
from __future__ import annotations

import json
import os
import threading
import time
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


class ChatIn(BaseModel):
    messages: list[dict]


class TokenIn(BaseModel):
    token: str


class ModelsIn(BaseModel):
    models: list[str]


class TokenCheckIn(BaseModel):
    token: str | None = None  # default: the saved one


class TranscribeIn(BaseModel):
    sources: list[str]
    with_comments: bool = False
    language: str | None = None  # None: the stage's default (en); "": let Whisper detect it


class RevealIn(BaseModel):
    which: str


class ReviewNewIn(BaseModel):
    run: str
    kind: str                     # comments, speakers, transcript
    sample: int = 100
    name: str | None = None
    blind: bool = False


class ReviewAnswerIn(BaseModel):
    item: str
    value: str | list[int]
    reason: str = ""
    flags: dict = {}
    note: str = ""
    seconds: float | None = None
    by: str = "me"


class ConfirmIn(BaseModel):
    tool: str
    args: dict = {}


class IngestIn(BaseModel):
    source: str
    archive: str | None = None
    project: str = "Project"
    template: str | None = None
    kinds: list[str] | None = None


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def host_name(header: str) -> str:
    """The name part of a Host header: '127.0.0.1:8765' -> '127.0.0.1', '[::1]:8765' -> '::1'."""
    h = header.strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def create_app(cfg: MachineConfig, dispatch: bool = True, extra_hosts: tuple[str, ...] = ()) -> FastAPI:
    from contextlib import asynccontextmanager
    held: dict = {}  # the assistant, started on first use

    @asynccontextmanager
    async def lifespan(app):
        yield
        stop.set()
        if "assistant" in held:
            held["assistant"].stop()  # never leave its llama-server running

    app = FastAPI(title="On the Record", lifespan=lifespan)
    allowed = LOOPBACK | {h.lower() for h in extra_hosts if h not in ("0.0.0.0", "::", "")}

    @app.middleware("http")
    async def only_from_this_machine(request, call_next):
        """Answer only requests addressed to this machine by name. A web page whose own domain has been re-pointed at
        127.0.0.1 (DNS rebinding) reaches this port as same-origin to the browser, but its requests carry its own name
        in Host and are refused. Changes must also come from this GUI's own page: a request that names another origin
        (another site, or another program's page on this machine) is refused. Scripts send no Origin and are fine."""
        host = request.headers.get("host", "")
        if host_name(host) not in allowed:
            return PlainTextResponse("On the Record answers only at http://127.0.0.1 or http://localhost.", 400)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD") and origin is not None and origin.lower() != f"http://{host.lower()}":
            return PlainTextResponse("Refused: this request came from another web page.", 403)
        return await call_next(request)
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

    held_rates: dict = {}

    def rates():
        """This machine's pace per stage (usage.Rates), read again at most once a minute."""
        from .usage import Rates
        if time.time() - held_rates.get("t", 0) > 60:
            held_rates.update(t=time.time(), rates=Rates(cfg.workspace))
        return held_rates["rates"]

    @app.post("/api/plan")
    def plan(body: RecipeIn):
        from .usage import estimate_plan
        r = parse(body.text)
        cells = Runner(r, cfg).plan()
        return {"stages": [s.info() for s in r.stages], "cells": cells,
                "estimate": estimate_plan(rates(), r, cells, cfg.tool("ffprobe"))}

    @app.post("/api/jobs")
    def submit(body: RecipeIn):
        parse(body.text)
        return {"run_id": queue.submit(body.text, recipes_dir)}

    queued_estimates: dict = {}

    @app.get("/api/jobs")
    def jobs():
        from .usage import estimate_run, estimate_text
        out = []
        for j in queue.list():
            text = j.pop("recipe_text", None)
            rec = store.run_dir(j["run_id"]) / "run.json"
            if rec.is_file():
                record = json.loads(rec.read_text(encoding="utf-8"))
                counts: dict[str, int] = {}
                for row in record["cells"].values():
                    for c in row.values():
                        counts[c["status"]] = counts.get(c["status"], 0) + 1
                j["cells"] = counts
                if j["status"] == "running" and record.get("stages"):
                    j["left_s"] = estimate_run(rates(), record, cfg.tool("ffprobe"))["seconds"]
            elif j["status"] == "queued" and j.get("backend") == "local" and text:
                if j["run_id"] not in queued_estimates:  # a queued recipe does not change; plan it once
                    try:
                        queued_estimates[j["run_id"]] = estimate_text(cfg, text, Path(j["recipe_dir"]), rates())["seconds"]
                    except Exception:
                        queued_estimates[j["run_id"]] = None
                j["left_s"] = queued_estimates[j["run_id"]]
            out.append(j)
        return out

    # ------------------------------------------------------------------ reviews (hand labels; see reviews.py)
    def review_or_404(set_id: str) -> dict:
        from . import reviews
        try:
            return reviews.load(cfg.workspace, set_id)
        except KeyError:
            raise HTTPException(404, f"no review {set_id}")

    @app.get("/api/reviews")
    def reviews_list():
        from . import reviews
        return reviews.list_sets(cfg.workspace)

    @app.post("/api/reviews")
    def reviews_new(body: ReviewNewIn):
        from . import reviews
        try:
            spec = reviews.from_run(store.root, body.run, body.kind, body.sample, name=body.name, blind=body.blind)
            return reviews.progress(cfg.workspace, reviews.create(cfg.workspace, spec))
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(422, str(e))

    @app.get("/api/reviews/{set_id}")
    def review_state(set_id: str):
        """The set as the labelling page sees it: question, answer kind, order, and the answers so far (values
        only). Machine answers and hidden fields stay on the server."""
        from . import reviews
        s = review_or_404(set_id)
        return {**reviews.progress(cfg.workspace, set_id), "question": s["question"], "answer": s["answer"],
                "order": s["order"], "prefill": s.get("prefill", False),
                "answers": {k: {f: r[f] for f in ("value", "reason", "flags", "note", "machine_seen", "by")}
                            for k, r in reviews.latest(reviews.rows(cfg.workspace, set_id)).items()}}

    @app.get("/api/reviews/{set_id}/items/{item_id:path}")
    def review_item(set_id: str, item_id: str, k: int = 2):
        from . import reviews
        s = review_or_404(set_id)
        if item_id not in s["by_id"]:
            raise HTTPException(404, item_id)
        return reviews.public_item(s, item_id, max(0, min(k, 12)))

    @app.get("/api/reviews/{set_id}/clip/{item_id:path}")
    def review_clip(set_id: str, item_id: str):
        from . import reviews
        review_or_404(set_id)
        p = reviews.clip(cfg.workspace, set_id, item_id, cfg.tool("ffmpeg"))
        if p is None:
            raise HTTPException(404, "no audio for this item")
        return FileResponse(p)

    @app.post("/api/reviews/{set_id}/answer")
    def review_answer(set_id: str, body: ReviewAnswerIn):
        from . import reviews
        review_or_404(set_id)
        try:
            return reviews.answer(cfg.workspace, set_id, body.item, body.value, reason=body.reason, flags=body.flags,
                                  note=body.note, seconds=body.seconds, by=body.by)
        except (KeyError, ValueError) as e:
            raise HTTPException(422, str(e).strip("'\""))

    @app.post("/api/reviews/{set_id}/reveal/{item_id:path}")
    def review_reveal(set_id: str, item_id: str, by: str = "me"):
        from . import reviews
        review_or_404(set_id)
        try:
            return {"machine": reviews.reveal(cfg.workspace, set_id, item_id, by)}
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))

    @app.get("/api/reviews/{set_id}/export.csv")
    def review_export(set_id: str):
        import csv
        import io
        from . import reviews
        review_or_404(set_id)
        rows = reviews.export_rows(cfg.workspace, set_id)
        buf = io.StringIO()
        fields = list(rows[0]) if rows else ["item"]
        w = csv.DictWriter(buf, fields)
        w.writeheader()
        w.writerows(rows)
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{set_id}.csv"'})

    @app.get("/api/reviews/{set_id}/score")
    def review_score(set_id: str):
        from . import reviews
        s = review_or_404(set_id)
        return {**reviews.score(s, reviews.rows(cfg.workspace, set_id)), **reviews.fingerprints(cfg.workspace, set_id)}

    @app.get("/api/usage")
    def usage_summary():
        from .usage import ledger
        return ledger(cfg.workspace, queue.list(limit=100000))

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

    # ------------------------------------------------------------------ import from cameras and drives
    @app.get("/api/devices")
    def devices():
        from .ingest import volumes
        return volumes()

    @app.get("/api/fs")
    def folders(path: str | None = None, files: bool = False):
        """Subfolders of a folder, for choosing a source or an archive; with files=true, also its audio and video
        files (no other kind of file is ever listed)."""
        from .ingest import KINDS
        p = Path(path).expanduser() if path else Path.home()
        if not p.is_dir():
            raise HTTPException(404, f"not a folder: {p}")
        try:
            entries = [e for e in p.iterdir() if not e.name.startswith(".")]
        except PermissionError:
            entries = []
        dirs = sorted((d.name for d in entries if d.is_dir()), key=str.lower)
        media = KINDS["audio"] | KINDS["video"]
        found = sorted(({"name": f.name, "bytes": f.stat().st_size} for f in entries
                        if files and f.is_file() and f.suffix.lower() in media), key=lambda f: f["name"].lower())
        return {"path": str(p.resolve()), "parent": str(p.resolve().parent) if p.resolve().parent != p.resolve()
                else None, "dirs": dirs, "files": found, "home": str(Path.home()), "archive": str(cfg.archive)}

    @app.post("/api/ingest/preview")
    def ingest_preview(body: IngestIn):
        from .ingest import DEFAULT_TEMPLATE, KINDS, plan, scan
        src = Path(body.source).expanduser()
        if not src.is_dir():
            raise HTTPException(422, f"not a folder: {src}")
        wanted = set(body.kinds or (*KINDS, "sidecar"))
        found = scan(src, ffprobe=cfg.tool("ffprobe"))
        files = plan([f for f in found if f.kind in wanted], body.template or DEFAULT_TEMPLATE, body.project)
        by_kind: dict[str, list] = {}
        for f in found:
            by_kind.setdefault(f.kind, [0, 0])
            by_kind[f.kind][0] += 1
            by_kind[f.kind][1] += f.size
        times = sorted(f.taken for f in files if f.taken)
        return {"source": str(src), "files": len(files), "bytes": sum(f.size for f in files),
                "skipped": len(found) - len(files), "by_kind": by_kind,
                "devices": sorted({f.device for f in files}), "first": times[0] if times else None,
                "last": times[-1] if times else None,
                "time_from": {k: sum(f.taken_from == k for f in files) for k in {f.taken_from for f in files}},
                "sample": [{"rel": f.rel, "dest": f.dest, "taken_from": f.taken_from} for f in files[:40]]}

    @app.post("/api/ingest")
    def ingest_start(body: IngestIn):
        from .ingest import DEFAULT_TEMPLATE
        src = Path(body.source).expanduser()
        if not src.is_dir():
            raise HTTPException(422, f"not a folder: {src}")
        spec = {"source": str(src.resolve()), "archive": str(Path(body.archive).expanduser() if body.archive
                                                               else cfg.archive),
                "project": body.project or "Project", "template": body.template or DEFAULT_TEMPLATE}
        if body.kinds:
            spec["kinds"] = body.kinds
        return {"run_id": queue.submit_task("ingest", spec, f"import {src.name or src}")}

    @app.get("/api/ingests")
    def ingests():
        from .ingest import clearable
        out, clears = [], {}
        recs = sorted((json.loads(p.read_text(encoding="utf-8")) for p in store.list_runs()),
                      key=lambda r: r.get("created_utc") or "", reverse=True)
        for r in recs:
            if r.get("kind") == "clear":
                clears.setdefault(r["clear"]["manifest"], r)
        for r in recs:
            if r.get("kind") != "ingest":
                continue
            counts: dict[str, int] = {}
            for row in r["cells"].values():
                st = row["ingest"]["status"]
                counts[st] = counts.get(st, 0) + 1
            ok, why = (False, "the import has not finished")
            man = r.get("manifest")
            if man and Path(man).is_file():
                ok, why = clearable(json.loads(Path(man).read_text(encoding="utf-8")))
            c = clears.get(man)
            out.append({"run_id": r["run_id"], "name": r["name"], "status": r["status"],
                        "created_utc": r.get("created_utc"), "ingest": r.get("ingest", {}), "counts": counts,
                        "manifest": man, "clearable": ok, "why": why,
                        "clear": {"run_id": c["run_id"], "status": c["status"], **c.get("clear", {})} if c else None,
                        "media": [str(Path(r["ingest"]["archive"]) / f) for f in _media_files(man)] if man else []})
        return out

    def _media_files(manifest: str) -> list[str]:
        try:
            m = json.loads(Path(manifest).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [f["dest"] for f in m["files"] if f["kind"] in ("video", "audio")
                and f["status"] in ("copied", "already archived")]

    @app.post("/api/ingest/{run_id}/clear")
    def ingest_clear(run_id: str):
        from .ingest import clearable
        p = store.run_dir(run_id) / "run.json"
        if not p.is_file():
            raise HTTPException(404, run_id)
        r = json.loads(p.read_text(encoding="utf-8"))
        if r.get("kind") != "ingest" or not r.get("manifest"):
            raise HTTPException(422, f"{run_id} is not an import")
        ok, why = clearable(json.loads(Path(r["manifest"]).read_text(encoding="utf-8")))
        if not ok:
            raise HTTPException(422, f"not clearing: {why}")
        return {"run_id": queue.submit_task("clear", {"manifest": r["manifest"], "ingest_run": run_id},
                                            f"clear {r['ingest']['volume']}"), "why": why}

    @app.get("/api/ingest/{run_id}/manifest.csv")
    def ingest_manifest(run_id: str):
        p = store.run_dir(run_id) / "run.json"
        r = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        man = r.get("manifest")
        if not man:
            raise HTTPException(404, run_id)
        csv_path = Path(man).with_suffix(".csv")
        if not csv_path.is_file():
            raise HTTPException(404, f"the manifest is not at {csv_path}: is the archive's drive plugged in?")
        return FileResponse(csv_path, filename=csv_path.name, media_type="text/csv")

    # ------------------------------------------------------------------ setup
    def folder_paths() -> dict[str, Path]:
        from . import bootstrap as b
        from .config import user_config_dir
        return {"workspace": cfg.workspace, "archive": cfg.archive, "models": cfg.models_root or b.home_dir() / "models",
                "settings": cfg.source.parent if cfg.source else user_config_dir()}

    @app.get("/api/setup")
    def setup_info():
        import shutil as sh
        from . import bootstrap as b
        from .config import user_config_dir
        hw = b.hardware()
        root = cfg.models_root or b.home_dir() / "models"
        status = b.model_status(root)
        fit = b.fit(hw)
        want = b.recommended_models(fit)
        tools = {}
        for t in ("ffmpeg", "ffprobe", "llama_server"):
            path = cfg.tool(t)
            tools[t] = path if (Path(path).is_file() or sh.which(path)) else None

        def free_gb(p: Path) -> int | None:
            while not p.exists() and p.parent != p:
                p = p.parent
            try:
                return round(sh.disk_usage(p).free / 1e9)
            except OSError:
                return None
        folders = [{"key": k, "path": str(p), "exists": p.exists(), "free_gb": free_gb(p)}
                   for k, p in folder_paths().items()]
        return {"hardware": hw, "fit": fit, "accel": b.accel(hw, cfg.python_for("audio")), "tools": tools,
                "models_root": str(root), "folders": folders,
                "models": [{"name": n, "size_gb": round(m["bytes"] / 1e9, 1), "bytes": m["bytes"], "about": m["about"],
                            "role": m["role"],
                            "gated": bool(m.get("gated")), "status": status[n], "recommended": n in want}
                           for n, m in b.MODELS.items()],
                "has_token": bool(os.environ.get("HF_TOKEN") or cfg.secret_env().get("HF_TOKEN")),
                "secrets_file": str(cfg.env_file or user_config_dir() / "secrets.env"),
                "config_file": str(cfg.source) if cfg.source else None, "workspace": str(cfg.workspace),
                "archive": str(cfg.archive)}

    @app.post("/api/setup/check-token")
    def setup_check_token(body: TokenCheckIn):
        """Ask Hugging Face about the token (pasted, or the saved one). The one setup call that goes online, and only
        when a person presses Check."""
        from . import bootstrap as b
        token = (body.token or "").strip() or os.environ.get("HF_TOKEN") or cfg.secret_env().get("HF_TOKEN")
        if not token:
            raise HTTPException(422, "There is no token to check yet: paste one first.")
        try:
            return b.check_hf_token(token)
        except OSError as e:
            raise HTTPException(502, f"Could not reach Hugging Face ({e}). Is this computer online?")

    @app.post("/api/reveal")
    def reveal(body: RevealIn):
        """Open one of On the Record's own folders in this computer's file manager."""
        import subprocess
        import sys
        p = folder_paths().get(body.which)
        if p is None or not p.is_dir():
            raise HTTPException(404, f"no folder {body.which!r} yet")
        cmd = {"win32": ["explorer", str(p)], "darwin": ["open", str(p)]}.get(sys.platform, ["xdg-open", str(p)])
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            raise HTTPException(501, f"cannot open a file manager here; the folder is {p}")
        return {"opened": str(p)}

    @app.post("/api/transcribe")
    def transcribe(body: TranscribeIn):
        """Queue the ready-made transcript recipe for links and files, set for this machine (presets.py)."""
        from . import presets
        sources = [x.strip() for x in body.sources if x.strip()]
        if not sources:
            raise HTTPException(422, "Choose a file or paste a link first.")
        bad = [x for x in sources if not presets.is_link(x) and not Path(x).expanduser().is_absolute()]
        if bad:
            raise HTTPException(422, f"Give the whole path of each file: {', '.join(bad)}")
        missing = presets.missing_models(cfg, body.with_comments)
        if missing:
            raise HTTPException(422, f"Download these models first (Setup, step 3): {', '.join(missing)}")
        text = presets.transcript_recipe([x if presets.is_link(x) else str(Path(x).expanduser()) for x in sources],
                                         body.with_comments, body.language)
        from .usage import estimate_text
        try:
            est = estimate_text(cfg, text, recipes_dir, rates())
            return {"run_id": queue.submit(text, recipes_dir), "recipe": text, "estimate": est}
        except RecipeError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/setup/token")
    def setup_token(body: TokenIn):
        from . import bootstrap as b
        from .config import user_config_dir
        t = body.token.strip()
        if not t.startswith("hf_") or len(t) < 20:
            raise HTTPException(422, "that does not look like a Hugging Face token (they start with hf_)")
        path = b.save_token(cfg.env_file or user_config_dir() / "secrets.env", t)
        if not cfg.env_file:
            cfg.env_file = path  # this session; `ontherecord setup` writes it into the machine config
        return {"saved": str(path)}

    @app.post("/api/setup/models")
    def setup_models(body: ModelsIn):
        from . import bootstrap as b
        bad = [m for m in body.models if m not in b.MODELS]
        if bad or not body.models:
            raise HTTPException(422, f"unknown models: {bad}" if bad else "choose at least one model")
        return {"run_id": queue.submit_task("download", {"models": body.models}, "download models")}

    # ------------------------------------------------------------------ the assistant
    def assistant():
        if "assistant" not in held:
            from .assistant import Assistant
            from .tools import Toolbox
            held["assistant"] = Assistant(cfg, Toolbox(cfg))
        return held["assistant"]

    @app.post("/api/assistant")
    def ask(body: ChatIn):
        try:
            return assistant().chat(body.messages)
        except FileNotFoundError as e:
            raise HTTPException(503, str(e))

    @app.post("/api/assistant/confirm")
    def confirm(body: ConfirmIn):
        return {"result": assistant().confirm(body.tool, body.args)}

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

    @app.get("/api/compare")
    def compare_runs(a: str, b: str):
        from .library import compare
        try:
            return compare(store, a, b)
        except KeyError as e:
            raise HTTPException(404, f"no run {e}")

    @app.get("/api/search")
    def search(q: str, limit: int = 200):
        return library.search(q, limit)

    app.state.stop_dispatch = stop
    return app


def serve(cfg: MachineConfig, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    import socket
    import urllib.request
    import webbrowser
    import uvicorn
    url = f"http://127.0.0.1:{port}/"
    try:  # already running (a second double-click on the shortcut): just show it
        urllib.request.urlopen(url + "api/machine", timeout=2)
        print(f"On the Record is already running: {url}")
        if open_browser:
            webbrowser.open(url)
        return
    except OSError:
        pass
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: listening on {host}. Anyone who can reach this address can start jobs on this machine and "
              "read its recordings. Use this only inside a container or a private network.")
    print(f"On the Record: {url}   (workspace {cfg.workspace})   Close this window to stop it.")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(cfg, extra_hosts=(host,)), host=host, port=port, log_level="warning")
