"""The assistant: a small local model that answers plain-English questions and starts work through the tools in
tools.py. It runs on llama-server, started on first use and stopped after a while unused.

By default it runs on the CPU, so it never takes GPU memory from a transcription or extraction job and never
changes their timings. A machine with GPU memory to spare can set `assistant: {gpu_layers: 999}` in machine.yaml.

The model never acts on anything marked `confirm` (starting, stopping or importing): it proposes, and the person
presses a button. Every tool call and its result is returned with the reply, so the GUI can show what the answer
rests on.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from datetime import date
from pathlib import Path

from .config import MachineConfig
from .llm import ManagedLlamaServer
from .tools import Toolbox, openai_tools

DEFAULT_MODEL = "gemma-4-E4B-it-UD-Q8_K_XL.gguf"

SYSTEM = """You are the assistant inside On the Record, a program that makes transcripts of recorded meetings and \
finds what people said in them. The person you help is not technical.
- Use the tools to look things up. Never guess what is in a recording.
- To start work (transcribing links, importing a camera card) call the tool; the person will be shown a button to \
confirm, so tell them to press it.
- Answer in a few plain sentences. When you mention something said in a recording, give the recording and the time.
- If a search finds nothing, search once more with fewer or simpler words before you answer.
Today is {today}."""


def proposal(p: dict) -> str:
    """What a waiting action will do, in a sentence."""
    a = p["args"]
    if p["tool"] == "transcribe":
        n = len(a.get("links") or [])
        return (f"I can transcribe {n} link{'s' if n != 1 else ''}"
                + (" and list its public comments" if a.get("with_comments") else "") + ".")
    if p["tool"] == "cancel_job":
        return f"I can stop job {a.get('job_id')}; work already finished is kept."
    if p["tool"] == "import_device":
        return f"I can copy everything on {a.get('device')} into the archive, checking every copy; the card is not changed."
    return f"I can run {p['tool']} with {a}."


def time_forms(t: str) -> set[str]:
    """'0:08:23' -> {'0:08:23', '8:23'}; '1:02:03' -> {'1:02:03'}; '17:05' -> {'17:05'}."""
    parts = [int(p) for p in t.split(":")]
    secs = parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]
    forms = {t}
    if secs < 3600:
        forms.add(f"{secs // 60}:{secs % 60:02d}")
    return forms


def time_links(steps: list[dict]) -> dict[str, str]:
    """Times the tools quoted, each with its player link, so the GUI can make the times in the answer clickable
    without relying on the model to copy links. A time quoted from two different recordings is left out."""
    seen: dict[str, set] = {}
    for s in steps:
        for t, url in re.findall(r"\[(\d+:\d{2}(?::\d{2})?)\][^\n]*?\((#play/[^)\s]+)\)", s["result"]):
            for form in time_forms(t):
                seen.setdefault(form, set()).add(url)
    return {t: next(iter(urls)) for t, urls in seen.items() if len(urls) == 1}


class Assistant:
    def __init__(self, cfg: MachineConfig, toolbox: Toolbox | None = None, idle_s: float = 900):
        self.cfg, self.tb, self.idle_s = cfg, toolbox or Toolbox(cfg), idle_s
        self.server: ManagedLlamaServer | None = None
        self.url: str | None = cfg.assistant.get("endpoint")  # an already-running OpenAI-compatible server
        self.lock = threading.Lock()
        self.last_used = 0.0
        threading.Thread(target=self._reaper, daemon=True).start()

    # ------------------------------------------------------------------ the model server
    def _ensure(self) -> str:
        if self.url and (self.server is None or self.server.proc is not None):
            return self.url
        a = self.cfg.assistant
        model = self.cfg.model_path(a.get("model", DEFAULT_MODEL))
        if not model.is_file():
            raise FileNotFoundError(f"the assistant's model is not at {model}; set assistant.model in machine.yaml")
        log_dir = self.cfg.workspace / "assistant"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.server = ManagedLlamaServer(self.cfg.tool("llama_server"), model, int(a.get("context", 16384)),
                                         [f"-ngl {a.get('gpu_layers', 0)} --jinja"], log_dir / "llama-server.log",
                                         log=lambda m: None)
        self.url = self.server.url
        return self.url

    def _reaper(self) -> None:
        while True:
            time.sleep(30)
            with self.lock:
                if self.server and time.time() - self.last_used > self.idle_s:
                    self.server.stop()
                    self.server.log_file.close()
                    self.server, self.url = None, self.cfg.assistant.get("endpoint")

    def _complete(self, url: str, messages: list[dict]) -> dict:
        body = {"messages": messages, "tools": openai_tools(self.tb), "temperature": 0.0, "seed": 42,
                "max_tokens": 1024, "chat_template_kwargs": {"enable_thinking": False}}
        req = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=600).read())["choices"][0]["message"]

    # ------------------------------------------------------------------ one turn
    def chat(self, messages: list[dict], max_rounds: int = 5) -> dict:
        """messages: the conversation so far (user and assistant turns, tool messages included), newest last.
        Returns the reply, the tool calls made (with results), any actions waiting for the person to confirm,
        and the conversation to send back next time."""
        with self.lock:
            self.last_used = time.time()
            url = self._ensure()
        msgs = [{"role": "system", "content": SYSTEM.format(today=date.today().isoformat())}, *messages]
        tools = {t.name: t for t in self.tb.tools()}
        steps, pending, reply = [], [], ""
        t0 = time.perf_counter()
        for _ in range(max_rounds):
            m = self._complete(url, msgs)
            calls = m.get("tool_calls") or []
            if not calls:
                reply = (m.get("content") or "").strip()
                msgs.append({"role": "assistant", "content": reply})
                break
            msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool = tools.get(name)
                if tool and tool.confirm:
                    pending.append({"tool": name, "args": args, "description": tool.description.split(".")[0]})
                    result = ("Nothing has started yet. The person now sees a 'Go ahead' button for this. Reply "
                              "with one sentence that says what will happen, and ask them to press Go ahead.")
                else:
                    result = self.tb.call(name, args)
                steps.append({"tool": name, "args": args, "result": result})
                msgs.append({"role": "tool", "tool_call_id": c.get("id", name), "content": result})
        else:
            reply = "I could not finish that in a few steps. Could you say it another way?"
            msgs.append({"role": "assistant", "content": reply})
        if pending:
            # Said by the program, not the model: a small model may claim it already did what still waits for a click.
            reply = " ".join(proposal(p) for p in pending) + " Press Go ahead to start it, or No to leave it."
            msgs[-1] = {"role": "assistant", "content": reply}
        with self.lock:
            self.last_used = time.time()
        return {"reply": reply, "steps": steps, "pending": pending, "messages": msgs[1:],
                "links": time_links(steps), "seconds": round(time.perf_counter() - t0, 1)}

    def confirm(self, tool: str, args: dict) -> str:
        """The person pressed the button: do it."""
        return self.tb.call(tool, args)

    def stop(self) -> None:
        with self.lock:
            if self.server:
                self.server.stop()
                self.server = None
