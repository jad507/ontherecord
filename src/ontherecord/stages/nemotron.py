"""Transcript, speakers and word times in one pass: NVIDIA Nemotron 3.5 Transcribe, served by vLLM with NeMo
Speech's plugin as its model card sets it up (nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-FP8, "Inference Setup
and Prompts", read Nov 2, 2026), plus vLLM's expert offloading for cards smaller than the 48 GB the card lists.

It makes `transcript`, segments that already carry speakers and word times (so `turns`, `code` and everything after
take it directly, with no `align`), and `nemotron_raw`: every response as the server sent it.
The server runs from its own environment (install/nemotron-vllm.sh), named in the machine config as
`tools: {nemotron_vllm: .../bin/vllm}`.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..artifacts import segments, write_json
from .base import Check, Context, Item, Result, Setting, Stage

REPO = "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-FP8"
REVISION = "2b6209f89bec0da576d69183bfdabb3b6c05ade2"
# The card's prompt for "who said what, and when" (its Prompt Guide), sent with word timestamps on
SPEAKER_PROMPT = ("Transcribe this audio with punctuation and capitalization. Include all spoken words and verbal "
                  "fillers. Write numbers in their standard written form. Put speaker tags <spk:0> <spk:1> <spk:2> "
                  "<spk:3> on the transcription.")
MAX_AUDIO_S = 3600          # trained on up to an hour; the server is told to take no more
CUT_SEARCH_S = 30           # a long recording is cut at the quietest second within this of each even split
RAM_HEADROOM_GIB = 4        # left free beside the experts held in RAM (they are pinned, so they cannot swap)
SERVER_ENV = {
    "VLLM_PLUGINS": "nemo_speechlm",
    "VLLM_MAX_AUDIO_DECODE_DURATION_S": str(MAX_AUDIO_S),
    "VLLM_MAX_AUDIO_CLIP_FILESIZE_MB": "180",
    "VLLM_USE_V2_MODEL_RUNNER": "0",                               # word timestamps need the V1 model runner
    "VLLM_DISABLED_KERNELS": "FlashInferFP8ScaledMMLinearKernel",  # the CUTLASS FP8 kernels the card validated
}  # the server also inherits the stage's environment: config.LOCAL_ONLY keeps vLLM and FlashInfer offline
TAG = re.compile(r"<spk:\d+>")


class NemotronTranscribe(Stage):
    """Transcribe a recording and say who spoke each word, with Nemotron 3.5 Transcribe (its own speaker encoder,
    not pyannote). Recordings longer than an hour are cut into pieces at quiet moments; speakers are then numbered
    per piece (P1_SPEAKER_00, P2_SPEAKER_00, ...), because the model does not carry voices from one piece to the
    next."""
    name = "nemotron_transcribe"
    label = "Transcribe and diarize (Nemotron 3.5)"
    version = "2"
    group = "audio"
    gpu = True
    needs = {"audio": "audio"}
    makes = {"transcript": "segments", "nemotron_raw": "other"}
    headline = ("words", "words")
    packages = ("soundfile", "numpy")
    settings = {
        "model": Setting(REPO, "Hugging Face repo in the local cache (ontherecord models get "
                         "nemotron-3.5-transcribe-fp8), or a local directory holding the checkpoint."),
        "revision": Setting(REVISION, "The cached revision of that repo to serve."),
        "endpoint": Setting("managed", "'managed' starts vLLM from the machine config's tools.nemotron_vllm and "
                            "stops it afterwards; or the /v1 URL of a server already running this model.",
                            in_key=False),
        "prompt": Setting(SPEAKER_PROMPT, "The instruction. Speaker tags <spk:N> in the answer become speakers; "
                          "a transcript without them is aligned as one speaker."),
        "max_piece_minutes": Setting(60, "Longest piece sent at once (at most 60: the model is trained on up to "
                                     "an hour). Fewer pieces keep more speakers under one label."),
        "max_tokens": Setting(24576, "Completion limit per piece; the card says a dense hour can need about "
                              "20,000. A piece that hits it fails the 'finished' check."),
        "speculative_tokens": Setting(8, "Multi-token prediction drafts this many tokens per step (0 = off). With "
                                      "experts in RAM, every token checked per step saves a pass over them."),
        "max_num_batched_tokens": Setting(4096, "Prefill chunk size (the card's value). Smaller uses less GPU "
                                          "memory per step."),
        "offload": Setting("22; 24; 26", "GiB of expert weights held in RAM (vLLM --cpu-offload-gb with "
                           "--cpu-offload-params experts), tried in turn until the server loads. The experts are "
                           "30.65 GB of the 34.7; 0 keeps everything on the GPU (48 GB cards and up).",
                           in_key=False),
        "offload_backend": Setting("auto", "vLLM's --offload-backend: uva reads the weights from RAM in place, "
                                   "prefetch copies layers ahead.", choices=["auto", "uva", "prefetch"],
                                   in_key=False),
        "gpu_memory_utilization": Setting(0.85, "Share of the card's memory vLLM may take (the card's value).",
                                          in_key=False),
        "loop_guardrail": Setting(True, "Collapse a run of 1 to 64 words repeated 20 or more times to one copy, "
                                  "as the card's reported results do (they apply it to the text)."),
    }

    def setup(self, settings: dict, ctx: Context):
        state = {"server": None, "tools": {"model": settings["model"], "model_revision": settings["revision"]}}
        if settings["endpoint"] == "managed":
            binary = ctx.config.tool("nemotron_vllm")
            if not Path(binary).is_file():
                raise FileNotFoundError(f"no vLLM for Nemotron at {binary!r}: run install/nemotron-vllm.sh and name "
                                        "its .venv/bin/vllm as tools.nemotron_vllm in the machine config")
            srv = VllmServer(binary, model_dir(settings), settings, ctx.scratch / "vllm.log", log=ctx.log)
            from ..envinfo import tool_version
            commit = Path(binary).resolve().parent.parent / "nemo-speech-commit.txt"
            state.update(server=srv, url=srv.url)
            state["tools"].update(vllm=tool_version([binary, "--version"]), offload_gib_used=srv.offload,
                                  nemo_speech_commit=commit.read_text().strip() if commit.is_file() else None,
                                  server_args=srv.command[1:])
        else:
            state["url"] = settings["endpoint"].rstrip("/")
        return state

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        import soundfile as sf
        path = Path(inputs["audio"])
        info = sf.info(str(path))
        duration = info.frames / info.samplerate
        max_s = min(float(settings["max_piece_minutes"]) * 60, MAX_AUDIO_S)
        pieces = plan_pieces(duration, max_s, lambda t: quietest_second(path, t))
        raw, words, turns, problems, unfinished, collapsed = [], [], [], [], [], 0
        tokens_in = tokens_out = 0
        t0 = time.time()
        for k, (a, b) in enumerate(pieces):
            ctx.log(f"piece {k + 1} of {len(pieces)}: {a / 60:.1f} to {b / 60:.1f} min")
            data, mime = piece_bytes(path, a, b, whole=len(pieces) == 1)
            started = time.time()
            try:
                resp = ask(state["url"], data, mime, settings)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                problems.append(f"piece {k + 1}: {e}")
                raw.append({"start_s": a, "end_s": b, "error": str(e)})
                continue
            seconds = round(time.time() - started, 1)
            choice = resp["choices"][0]
            usage = resp.get("usage") or {}
            tokens_in += usage.get("prompt_tokens") or 0
            tokens_out += usage.get("completion_tokens") or 0
            ts = resp.get("ctc_timestamps") or {"error": "the server sent no ctc_timestamps (is --middleware set?)"}
            raw.append({"start_s": round(a, 3), "end_s": round(b, 3), "seconds": seconds,
                        "finish_reason": choice.get("finish_reason"), "usage": usage,
                        "text": choice["message"].get("content") or "", "ctc_timestamps": ts})
            if choice.get("finish_reason") == "length":
                unfinished.append(k + 1)
            if ts.get("error") is not None:
                problems.append(f"piece {k + 1}: {ts['error']}")
                continue
            prefix = f"P{k + 1}_" if len(pieces) > 1 else ""
            w, t = piece_words_and_turns(ts, a, prefix)
            if settings["loop_guardrail"]:
                w, n = collapse_loops(w)
                collapsed += n
            words += w
            turns += t
        elapsed = time.time() - t0

        segs = segments_from_words(words)
        write_json(out / "transcript.json", segments(segs, "segment"))
        (out / "raw.json").write_text(json.dumps({"pieces": raw}, indent=1, ensure_ascii=False), encoding="utf-8")
        st = turn_stats(turns)   # the server's own diarization: speakers and speech, for the checks and metrics

        n_words = sum(len(s["text"].split()) for s in segs)
        minutes = duration / 60 if duration else 0
        wpm = n_words / minutes if minutes else 0
        n_spk = st["speaker_count"]
        checks = [
            Check("answers", "fail" if problems else "ok", len(problems),
                  "; ".join(problems) if problems else f"{len(pieces)} of {len(pieces)} pieces aligned"),
            Check("finished", "fail" if unfinished else "ok", unfinished,
                  f"piece(s) {unfinished} stopped at max_tokens: the transcript is cut short; raise max_tokens"
                  if unfinished else "every piece ended on its own"),
            Check("words_per_minute", "warn" if wpm < 30 else "ok", round(wpm, 1),
                  f"{n_words:,} words over {minutes:.0f} min" + (" — very little speech recognized" if wpm < 30
                                                                  else "")),
            Check("speakers", "warn" if n_spk <= 2 and duration > 20 * 60 else "ok", n_spk,
                  f"{n_spk} speaker labels in {minutes:.0f} min"),
            Check("loops", "warn" if collapsed else "ok", collapsed,
                  f"{collapsed} repeated words collapsed (the model looped)" if collapsed else "no loops"),
            Check("pieces", "warn" if len(pieces) > 1 else "ok", len(pieces),
                  f"cut into {len(pieces)} pieces; a person has a different label in each piece"
                  if len(pieces) > 1 else "one piece"),
        ]
        from ..llm import leaves_machine_check, remote_host
        checks += leaves_machine_check(remote_host(settings["endpoint"]))
        return Result(
            files={"transcript": "transcript.json", "nemotron_raw": "raw.json"},
            checks=checks, device="server",
            metrics={"segments": len(segs), "words": n_words, "duration_s": round(duration, 1),
                     "pieces": len(pieces), "speaker_count": n_spk, "speakers": n_spk, "speech_s": st["speech_s"],
                     "overlap_s": st["overlap_s"], "llm_calls": len(pieces),
                     "input_tokens": tokens_in, "output_tokens": tokens_out,
                     "gen_tokens_per_s": round(tokens_out / elapsed, 2) if elapsed else None,
                     "audio_minutes_per_minute": round(duration / elapsed, 3) if elapsed else None},
            tools=state["tools"])

    def teardown(self, state) -> None:
        if state.get("server"):
            state["server"].stop()


# ---------------------------------------------------------------------------------------------- the server
def model_dir(settings: dict) -> Path:
    p = Path(settings["model"]).expanduser()
    if p.is_dir():
        return p
    from ..bootstrap import hf_cache
    snap = hf_cache() / ("models--" + settings["model"].replace("/", "--")) / "snapshots" / settings["revision"]
    if not snap.is_dir():
        raise FileNotFoundError(f"{settings['model']} @ {settings['revision'][:12]} is not downloaded: "
                                "ontherecord models get nemotron-3.5-transcribe-fp8")
    return snap


def available_ram_gib() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2 ** 20
    except OSError:
        pass
    return None


def server_args(model: Path, port: int, s: dict, offload_gib: float) -> list[str]:
    """`vllm serve` as the model card runs it, for one request at a time, plus expert offloading."""
    args = ["serve", str(model), "--served-model-name", REPO, "--host", "127.0.0.1", "--port", str(port),
            "--trust-remote-code", "--dtype", "bfloat16", "--kv-cache-dtype", "auto",
            "--gpu-memory-utilization", str(s["gpu_memory_utilization"]), "--max-model-len", "65536",
            "--max-num-batched-tokens", str(s["max_num_batched_tokens"]), "--max-num-seqs", "1",
            "--enable-chunked-prefill", "--no-enable-prefix-caching",
            "--use-replayssm", "--mamba-backend", "flashinfer", "--mamba-cache-mode", "none",
            "--mm-processor-cache-gb", "0", "--no-async-scheduling",
            "--limit-mm-per-prompt", json.dumps({"audio": {"count": 1, "length": MAX_AUDIO_S * 16000}}),
            "--reasoning-parser", "nemotron_v3", "--trust-request-mm-kwargs",
            "--middleware", "nemo.collections.speechlm2.vllm.salm.ctc_serving.ctc_timestamp_middleware",
            "--seed", "0"]
    if s["speculative_tokens"]:
        args += ["--speculative-config", json.dumps({"method": "mtp",
                                                     "num_speculative_tokens": int(s["speculative_tokens"])})]
    if offload_gib:
        args += ["--cpu-offload-gb", f"{offload_gib:g}", "--cpu-offload-params", "experts"]
        if s["offload_backend"] != "auto":
            args += ["--offload-backend", s["offload_backend"]]
    return args


class VllmServer:
    """vLLM on a free local port, trying each offload amount in turn until the model loads. An amount that would
    leave less than RAM_HEADROOM_GIB of memory free is skipped rather than tried: pinned memory cannot swap, and
    running the machine out of it takes everything else down with the server."""

    def __init__(self, binary: str, model: Path, settings: dict, log_path: Path, log=print):
        from ..llm import free_port
        self.proc, self.log_file = None, open(log_path, "a", encoding="utf-8")
        env = {**os.environ, **SERVER_ENV}
        if Path("/usr/local/cuda/bin/nvcc").is_file():  # FlashInfer compiles its kernels on first use
            env["PATH"] = "/usr/local/cuda/bin" + os.pathsep + env.get("PATH", "")
        tried = []
        for gib in [float(x) for x in str(settings["offload"]).split(";") if x.strip()]:
            free = available_ram_gib()
            if gib and free is not None and gib + RAM_HEADROOM_GIB > free:
                log(f"  not trying {gib:g} GiB of experts in RAM: {free:.1f} GiB is free")
                tried.append(f"{gib:g} GiB (only {free:.1f} GiB of RAM free)")
                continue
            port = free_port()
            self.command = [binary, *server_args(model, port, settings, gib)]
            log(f"vllm serve {model.name} with {gib:g} GiB of experts in RAM (the first start compiles kernels "
                "for several minutes)")
            started = time.time()
            self.proc = subprocess.Popen(self.command, stdout=self.log_file, stderr=subprocess.STDOUT, env=env)
            if self._wait(port):
                self.url, self.offload = f"http://127.0.0.1:{port}/v1", gib
                log(f"  ready after {time.time() - started:.0f} s")
                return
            log(f"  did not load with {gib:g} GiB in RAM")
            tried.append(f"{gib:g} GiB (did not load)")
            self.stop()
        self.log_file.close()
        raise RuntimeError(f"vLLM did not serve {model.name}: {'; '.join(tried) or 'no offload amounts given'}; "
                           f"see {log_path}")

    def _wait(self, port: int) -> bool:
        while self.proc.poll() is None:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
                return True
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(5)
        return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if not self.log_file.closed:
            self.log_file.flush()


def ask(url: str, data: bytes, mime: str, s: dict, timeout: int = 6 * 3600) -> dict:
    """One transcription request with word timestamps, with the card's transcription preset: temperature 0,
    reasoning off, seed 0, special tokens kept (they carry the speaker tags)."""
    body = {"model": REPO, "temperature": 0, "top_p": 1.0, "seed": 0, "max_tokens": int(s["max_tokens"]),
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": s["prompt"]},
                {"type": "audio_url", "audio_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}},
            ]}],
            "chat_template_kwargs": {"enable_thinking": False}, "skip_special_tokens": False,
            "spaces_between_special_tokens": False, "mm_processor_kwargs": {"capture_ctc_timestamps": True}}
    req = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


# ---------------------------------------------------------------------------------------------- the audio
def plan_pieces(duration: float, max_s: float, quiet_at) -> list[tuple[float, float]]:
    """(start, end) of each piece: the whole recording if it fits, else even splits moved to a quiet second
    nearby. Splits are planned CUT_SEARCH_S short of the limit, so moving a cut never makes a piece too long."""
    if duration <= max_s:
        return [(0.0, duration)]
    n = math.ceil(duration / (max_s - 2 * CUT_SEARCH_S))
    edges = [0.0, *(quiet_at(k * duration / n) for k in range(1, n)), duration]
    return list(zip(edges, edges[1:]))


def quietest_second(path: Path, t: float) -> float:
    """The middle of the quietest one-second window within CUT_SEARCH_S of t (100 ms steps)."""
    import numpy as np
    import soundfile as sf
    sr = sf.info(str(path)).samplerate
    a = max(0.0, t - CUT_SEARCH_S)
    x, _ = sf.read(str(path), start=int(a * sr), stop=int((t + CUT_SEARCH_S) * sr), dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    hop = sr // 10
    frames = len(x) // hop
    if frames < 10:
        return t
    energy = (x[: frames * hop].reshape(frames, hop) ** 2).mean(axis=1)
    window = np.convolve(energy, np.ones(10), mode="valid")
    return a + (int(window.argmin()) + 5) * hop / sr


def piece_bytes(path: Path, a: float, b: float, whole: bool) -> tuple[bytes, str]:
    """The audio to send: the file as it is when it is one piece, else that stretch as 16-bit FLAC."""
    if whole:
        mime = {".flac": "audio/flac", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}
        return path.read_bytes(), mime.get(path.suffix.lower(), "audio/flac")
    import soundfile as sf
    sr = sf.info(str(path)).samplerate
    x, _ = sf.read(str(path), start=int(a * sr), stop=int(b * sr), dtype="int16", always_2d=True)
    buf = io.BytesIO()
    sf.write(buf, x, sr, format="FLAC", subtype="PCM_16")
    return buf.getvalue(), "audio/flac"


# ---------------------------------------------------------------------------------------------- the answer
def speaker_label(prefix: str, column) -> str:
    return "UNKNOWN" if column is None else f"{prefix}SPEAKER_{int(column):02d}"


def piece_words_and_turns(ts: dict, offset: float, prefix: str = "") -> tuple[list[dict], list[dict]]:
    """One piece's ctc_timestamps as words and speaker turns on the recording's clock. A word's speaker is its
    transcript tag mapped to the diarization speaker the server paired it with, so words and turns share labels."""
    mapping = ts.get("speaker_tag_to_diarization_speaker") or {}
    words = []
    for w in ts.get("words") or []:
        text = TAG.sub("", w["word"]).strip()
        if not text:
            continue
        words.append({"start": round(w["start"] + offset, 3), "end": round(w["end"] + offset, 3), "word": text,
                      "speaker": speaker_label(prefix, mapping.get(str(w["speaker"])))})
    turns = [{"start": round(d["start"] + offset, 3), "end": round(d["end"] + offset, 3),
              "speaker": speaker_label(prefix, d["speaker"])} for d in ts.get("diarization") or []]
    return words, turns


def collapse_loops(words: list[dict], max_block: int = 64, min_repeats: int = 20) -> tuple[list[dict], int]:
    """The card's generation-loop guardrail on words: a block of 1 to max_block words repeated min_repeats or more
    times in a row keeps one copy. Returns the words and how many were dropped."""
    toks = [w["word"].lower() for w in words]
    out, i, n, dropped = [], 0, len(toks), 0
    while i < n:
        loop = None
        for size in range(1, max_block + 1):
            if i + size * min_repeats > n:
                break
            block, r = toks[i:i + size], 1
            while toks[i + r * size:i + (r + 1) * size] == block:
                r += 1
            if r >= min_repeats:
                loop = (size, r)
                break
        if loop:
            size, r = loop
            out += words[i:i + size]
            dropped += (r - 1) * size
            i += r * size
        else:
            out.append(words[i])
            i += 1
    return out, dropped


def segments_from_words(words: list[dict], gap: float = 1.0, longest: float = 30.0) -> list[dict]:
    """Whisper-shaped segments: a new one at a change of speaker, a pause longer than `gap`, or past `longest`
    seconds. Words keep faster-whisper's form (a leading space), without its probability."""
    segs, cur = [], None
    for w in words:
        if cur and (w["speaker"] != cur["speaker"] or w["start"] - cur["end"] > gap
                    or w["end"] - cur["start"] > longest):
            segs.append(cur)
            cur = None
        if cur is None:
            cur = {"start": w["start"], "end": w["end"], "speaker": w["speaker"], "words": []}
        cur["words"].append({"start": w["start"], "end": w["end"], "word": " " + w["word"]})
        cur["end"] = max(cur["end"], w["end"])
    if cur:
        segs.append(cur)
    return [{"id": f"s{i:04d}", "start": s["start"], "end": s["end"],
             "text": "".join(w["word"] for w in s["words"]).strip(), "speaker": s["speaker"], "words": s["words"]}
            for i, s in enumerate(segs)]


def turn_stats(turns: list[dict]) -> dict:
    """Speakers, speech per speaker, speech overall, and overlap, from the server's diarization turns."""
    spk: dict[str, float] = {}
    for t in turns:
        spk[t["speaker"]] = spk.get(t["speaker"], 0.0) + t["end"] - t["start"]
    union, last = 0.0, None
    for t in sorted(turns, key=lambda t: t["start"]):
        if last is None or t["start"] > last[1]:
            if last:
                union += last[1] - last[0]
            last = [t["start"], t["end"]]
        else:
            last[1] = max(last[1], t["end"])
    if last:
        union += last[1] - last[0]
    total = sum(spk.values())
    return {"speaker_count": len(spk), "speakers": {k: round(v, 3) for k, v in sorted(spk.items())},
            "speech_s": round(union, 1), "overlap_s": round(max(0.0, total - union), 1)}
