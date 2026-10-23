"""Reaching a language model: an OpenAI-compatible server that is already running, or a llama-server that the stage
starts itself and stops when it is done. Ported from hands_on_dl/llm_endpoint.py and run_comment_pipeline.sh.

Reproducibility on a server: temperature 0, a fixed seed, and cache_prompt off, so no call reuses the previous call's
KV cache (with the cache kept, an answer depends on which call came before it).
"""
from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def quick_fingerprint(path: Path, span: int = 16 << 20) -> dict:
    """Size plus a sha256 of the first and last 16 MB. Hashing a 16 GB model in full takes minutes per run; this
    still tells two different files apart, and the receipt says it is partial."""
    h = hashlib.sha256()
    size = path.stat().st_size
    with open(path, "rb") as f:
        h.update(f.read(span))
        if size > span:
            f.seek(max(span, size - span))
            h.update(f.read(span))
    return {"file": path.name, "bytes": size, "sha256_head_tail_16mb": h.hexdigest()}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """An OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(self, base_url: str, thinking: bool = False, seed: int = 42, timeout: int = 7200):
        self.base_url, self.thinking, self.seed, self.timeout = base_url.rstrip("/"), thinking, seed, timeout
        self.served_model = self._get("/models")["data"][0]["id"]
        root = self.base_url.removesuffix("/v1")
        try:  # llama-server reports its context size; other servers may not
            props = json.loads(urllib.request.urlopen(root + "/props", timeout=30).read())
            self.n_ctx = int(props["default_generation_settings"]["n_ctx"])
        except (urllib.error.URLError, KeyError, ValueError):
            self.n_ctx = 32768

    def _get(self, path: str) -> dict:
        return json.loads(urllib.request.urlopen(self.base_url + path, timeout=60).read())

    def chat(self, messages, temperature=0.0, max_tokens=4096, schema=None, seed=None, **sampling) -> dict:
        """`seed` overrides the server's for one call; `sampling` adds top_p, top_k and the like."""
        body = {"model": self.served_model, "messages": messages, "temperature": temperature,
                "max_tokens": max_tokens, "seed": self.seed if seed is None else seed, "cache_prompt": False,
                "chat_template_kwargs": {"enable_thinking": self.thinking}, **sampling}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
        req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        for attempt in range(3):
            try:
                r = json.loads(urllib.request.urlopen(req, timeout=self.timeout).read())
                break
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(30)
        choice = r["choices"][0]
        text = choice["message"].get("content") or ""
        finish = choice.get("finish_reason")
        usage, t = r.get("usage", {}), r.get("timings") or {}  # timings: llama-server's own, per call
        return {"text": text if finish == "stop" else "", "raw_text": text, "finish_reason": finish,
                "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                "prompt_n": t.get("prompt_n"), "prompt_ms": t.get("prompt_ms"), "gen_n": t.get("predicted_n"),
                "gen_ms": t.get("predicted_ms")}


class ManagedLlamaServer:
    """Start llama-server on a free local port, trying each offload setting in turn until the model loads."""

    def __init__(self, binary: str, model: Path, ctx_size: int, offloads: list[str], log_path: Path, log=print):
        self.proc = None
        self.log_file = open(log_path, "a", encoding="utf-8")
        for args in offloads:
            port = free_port()
            cmd = [binary, "-m", str(model), "-c", str(ctx_size), "-np", "1", *args.split(),
                   "--host", "127.0.0.1", "--port", str(port)]
            log(f"llama-server {model.name} -c {ctx_size} {args}")
            self.proc = subprocess.Popen(cmd, stdout=self.log_file, stderr=subprocess.STDOUT)
            if self._wait(port):
                self.url, self.offload, self.command = f"http://127.0.0.1:{port}/v1", args, cmd[1:]
                return
            log(f"  did not load with {args!r}")
            self.stop()
        self.log_file.close()
        raise RuntimeError(f"llama-server could not load {model.name} with any of {offloads}; see {log_path}")

    def _wait(self, port: int) -> bool:
        while self.proc.poll() is None:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
                return True
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(3)
        return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


def open_model(settings: dict, ctx) -> dict:
    """A stage's model, from its `endpoint`, `model`, `context`, `offload`, `thinking` and `seed` settings: a
    llama-server it starts (endpoint 'managed') or a server already running. Returns the stage state: `llm`, the
    `server` to stop in teardown (or None), and `tools` for the receipt."""
    state = {"server": None}
    if settings["endpoint"] == "managed":
        from .envinfo import tool_version
        binary = ctx.config.tool("llama_server")
        model = ctx.config.model_path(settings["model"])
        if not model.is_file():
            raise FileNotFoundError(f"model not found: {model} (set models_root in the machine config)")
        offloads = [s.strip() for s in settings["offload"].split(";") if s.strip()]
        srv = ManagedLlamaServer(binary, model, settings["context"], offloads, ctx.scratch / "llama-server.log",
                                 log=ctx.log)
        state.update(server=srv, url=srv.url,
                     tools={"llama_server": tool_version([binary, "--version"]), "offload_used": srv.offload,
                            "model": quick_fingerprint(model)})
    else:
        state.update(url=settings["endpoint"], tools={})
    state["llm"] = Server(state["url"], thinking=settings["thinking"], seed=settings["seed"])
    state["tools"]["served_model"] = state["llm"].served_model
    state["tools"]["n_ctx"] = state["llm"].n_ctx
    return state


def close_model(state: dict) -> None:
    srv = state.get("server")
    if srv:
        srv.stop()
        srv.log_file.close()


def parse_json(text: str):
    """The model's JSON, tolerating a ```json fence; None if it does not parse."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None


def est_tokens(text: str) -> int:
    """A conservative token estimate (transcript text runs about 4 characters a token)."""
    return len(text) // 3 + 1
