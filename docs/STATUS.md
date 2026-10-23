# Status and handoff (October 22, 2026)

Where On the Record stands, what was decided and why, what is waiting on Jeff, and what to do next. Written at the
end of the long first build session, for whoever picks it up next (a person or an agent). The Research-a-thon
write-up notes, with every number and its source, are in [researchathon-notes.md](researchathon-notes.md).

## Against the original four-week plan

| Week | Planned | State |
|---|---|---|
| 1 | Runner, store, receipts, checks, the council pipeline stages, from the command line | Done |
| 2 | The job queue and the GUI | Done |
| 3 | Compare-two-runs view; stress pipelines (StressTest, StressPresso, WildStressTest with clip and human-label stages, WhiStress to text-reader cascade); scoring against labels | Compare done. Stress set, WhiStress and reader stages done, scored against the labels inside each stage. The reader runs are queued. Not done: WildStressTest's clip-cutting and labelling stages (the finished items.json is loaded instead), the classical detectors (Praat, wavelet), and the audio-LLM listening arm. |
| 4 | Slurm backend; Windows and macOS tests; backfill receipts for existing hands_on_dl outputs; more checks | Not started. Installers for all three systems are written; only Linux is tested. |
| 5-6 | About 10 days of writing | Notes started |

Added since the plan, at Jeff's request: Library and player, Import from cameras and drives (verified copies,
catalog, clearing the card), the Ask assistant with confirm buttons, the MCP server, the Setup tab and model
catalog, installers (uv) and a Docker image, and one-job-at-a-time with `ontherecord exclusive`.

## October 23: first run, machine detection, picking up where you left off

Jeff asked for an easier first run (driver, token, first file), detection of the card, memory and system with
defaults to match, CUDA builds above 12.4/12.8 where the machine allows, and a better return to the program.

- **Detection** (`hwdetect.py`, standard library only, so the installers run it first): every NVIDIA card's memory,
  free memory, compute capability and driver, the newest CUDA the driver supports (both banner styles,
  `CUDA Version` and since the 610 drivers `CUDA UMD Version`), AMD and Intel graphics, WSL.
- **PyTorch build per machine**: dependency groups `cu128` (default, as before), `cu126`, `cpu`, chosen by the
  installers. Found while doing this: PyTorch 2.11's cu128 wheels have no kernels below compute capability 7.5
  (`.ci/manywheel/build_cuda.sh` at v2.11.0), so a GTX 10-series card got no GPU at all before; cu126 still has
  Maxwell to Hopper. CPU-only machines no longer download about 3 GB of CUDA libraries.
- **Not PyTorch's CUDA 13**: CTranslate2 4.8.3 (the newest) loads `libcublas.so.12`, and `nvidia-cudnn-cu12` and
  `-cu13` both install `nvidia/cudnn/lib/libcudnn.so.9`. CUDA 12 builds run on CUDA 13 drivers. Revisit when
  CTranslate2 ships CUDA 13 wheels, or with a torch upgrade (2.12 to 2.14 exist, with cu132).
- **llama.cpp goes higher**: its CUDA 13.4 builds (Windows and Linux, x64 and arm64; Windows was on CUDA 12.4) when
  the driver supports 13.4 and the card is Turing or newer, else CUDA 12, else Vulkan, else the CPU build. Each
  build is checked with `llama-server --list-devices` and setup falls down the list if it cannot see the card.
  Each build has its own folder (`bin/llama-b11249-<build>`). Docker stays on CUDA 12.8 (`--build-arg TORCH=`
  picks its PyTorch).
- **Whisper per machine**: large-v3 float16 on NVIDIA cards with 6 GB or more, int8_float16 below that, int8 on
  cards without fast float16 (compute capability below 7.0); large-v3-turbo int8 on the processor, including Macs
  (CTranslate2 has no Metal). Turbo is in the catalog, pinned (`mobiuslabsgmbh/faster-whisper-large-v3-turbo`,
  which Hugging Face redirects to `dropbox-dash/...`; rev `0a363e91...`, 1.6 GB, MIT). The transcribe stage now
  falls back to a compute type the card supports and to the CPU when CUDA 12's cuBLAS will not load, both recorded,
  and names turbo's revision correctly.
- **`ontherecord transcribe FILE|LINK...`** and the GUI's **Transcribe a recording** (Library, and Setup step 5)
  queue one ready-made recipe (`presets.py`) with this machine's settings written in. The assistant's transcribe
  tool uses it too (its description is unchanged, so no eval rerun is needed).
- **Setup is a guide**: five steps with status, opening by itself until the Library has a recording; the driver
  advice (needed, recommended, optional); what each part runs on; a warning when another program holds the GPU's
  memory; the installed PyTorch build against the one the card needs; a token **Check** (Hugging Face's whoami and
  the gated file at the pinned revision; the only setup call that goes online, on a click); where the folders are,
  with Open buttons.
- **Picking up where you left off** (per browser): the tab, the recording paused where it stopped, the Ask
  conversation (a confirm button that was waiting is not brought back), the Build draft, and a banner of jobs that
  finished or failed since the last visit. The player's **Save** writes .txt, .srt or .vtt.
- INSTALL.md is now the walkthrough. Projects and folders: a proposal waiting on Jeff,
  [projects-and-folders.md](projects-and-folders.md).
- Tested: 74 tests; the GUI headless in Chrome against a scratch workspace and, read-only, the real one (resume to
  the same second, paused; the banner; an .srt saved). Not tried: the installers with the new groups on a fresh
  machine (the three groups resolve in `uv sync --dry-run`), the CUDA 13.4 llama.cpp download (about 600 MB),
  Windows, a GTX 10-series card.

## October 23: compute time, tokens and their API price (Jeff's Oct 22 item 3)

- `usage.py` and `ontherecord usage` (and the Jobs tab's line and details): compute hours from the store's receipts
  (each one a piece of work done once; a batch's model loading counted once), by stage, device and month; the
  queue's busy time; tokens; their price at six API models (Claude Sonnet 5.5 and GPT-5.6 Terra headline; Opus 5.5
  and Sol, Fable 5.1 and Astra too), read Oct 23 from Anthropic's and OpenAI's pricing pages and written in
  `usage.py` with that date; speeds by software version (Whisper and pyannote as times real time; llama.cpp prompt
  and generation tok/s).
- Tokens now recorded by every language-model stage (stress_read too) with llama-server's own speeds (its `timings`,
  checked in tools/server/server-common.cpp), in metrics only: timings vary per run, and comments.json must stay
  byte-identical. The assistant logs each call to `<workspace>/usage/assistant.jsonl`.
- Estimates from this machine's receipts (per minute of audio, per stage and model, plus model loading), in Jeff's
  yardsticks (a coffee break, a lunch break, a long meeting, overnight): `plan` and its `--json`, `transcribe`, the
  Build tab, the Transcribe form, the Jobs tab's time left, and the assistant's transcribe and jobs answers (their
  outputs; no tool description changed, so no eval rerun).
- shiro so far: 5.66 h since Oct 22; 468,867 tokens in and 39,879 out (comment extraction), $1.34 at Sonnet 5.5,
  $1.42 at GPT-5.6 Terra. The 10 stress_read receipts from before Oct 23 carry no token counts and are reported as
  not counted. Whisper large-v3 float16 runs at 7.5x real time and diarization at 58x on the 5070 Ti; this machine
  takes about 9 minutes per hour of audio for a transcript.
- Not done: tok/s has no data until the next language-model run; transcription is not priced at API speech-to-text
  rates (only tokens are); hands_on_dl and AITranscribe work before On the Record is not in the ledger.

## October 23: security review (after a run of zero-days)

What goes online: setup's downloads, the YouTube download stage, the token Check, and an assistant endpoint if one
is configured. Everything else stays on 127.0.0.1. Found and fixed:

- **DNS rebinding on the GUI server** (since the GUI existed): it answered any Host header, so a web page whose
  domain was re-pointed at 127.0.0.1 could read transcripts, list folders, queue jobs (an `extract_comments`
  endpoint can send a transcript anywhere), replace the token, or clear a card after an import. Now only requests
  addressed to 127.0.0.1, localhost or ::1 (or an explicit `--host`) are answered, and a POST naming another origin
  is refused. Tested with curl and in the browser.
- **ffmpeg for Windows and Linux was "BtbN's latest"**, checked only against the digest the same release
  published. Now BtbN's month-end build of Sep 30, 2026 (8.1.3, kept long-term, uploaded by its Actions bot and
  never replaced), pinned by SHA-256.
- **uv came from `curl https://astral.sh/uv/install.sh | sh`** (and `irm ... | iex`), always the newest. Now
  `install/uv-pinned.sh` and the PowerShell equivalent fetch uv 0.11.25 (June 27) from its GitHub release and keep
  it only if its SHA-256 matches (GitHub's digest, which uv's own installer also carries). uv's install.ps1 does not
  check its download, and its install.sh skips the check on macOS (no `sha256sum`). The Dockerfile's uv image is
  pinned by digest too. Tested on Linux, including a mismatch stopping the install; the PowerShell is untested.
- **The speech models were pinned by revision only.** `model_hashes.py` now holds every file's hash (sha256, or
  git's blob id for small files) at the pinned revision; setup checks each download and deletes a file that does
  not match, and `ontherecord models verify` checks what is on disk. shiro's cached speech models and the Oct 22
  Gemma files all match. Downloads are now only the files the programs load (faster-whisper's own list).
- **yt-dlp with bun runs YouTube's JavaScript unsandboxed** (yt-dlp's bun provider: "No sandboxing options
  available"; deno runs with no permissions). `machine.example.yaml` now names deno, and `setup --check` warns.
  shiro still uses bun: deno is not installed.

Checked and fine: no LiteLLM anywhere (lock or environments). Every PyPI release in the lock published since Sep 23
either has PyPI provenance from its own GitHub repository, or comes from a project that has never published with
provenance (no break in habit). The one break, SQLAlchemy 2.0.55, is the 2.0 branch's norm, and only used on Python
below 3.11. Yesterday's re-lock added only PyTorch and NVIDIA packages, the newest being cuda-bindings 13.4.4 (Oct
14, NVIDIA's provenance), reachable only without a PyTorch group. uv verifies every package against uv.lock's
hashes, and managed Pythons against its own. llama-server binds 127.0.0.1; WhiStress loads with
`weights_only=True`; no `shell=True`, `eval` or unsafe YAML; file writes from the GUI are slugged.

Still depending on TLS: a forged certificate for huggingface.co (the post claims a CA's keys were cracked) would
expose the Hugging Face token during a download or Check (it is read-only; revoke it if in doubt), but could not
change a model file. Keep the system's root certificates updated.

## Built and tested (shiro, Linux, RTX 5070 Ti)

- 79 tests pass (`pytest`; 55 before Oct 23). Commits on `main`, none pushed.
- The 11 meetings, three ways (left channel, averaged, as coded in May), each compared with hands_on_dl's files:
  see researchathon-notes.md.
- WhiStress inside On the Record gives the poster's marks on every item (StressTest 218/218, StressPresso
  202/202).
- Linux installer in a fresh clone; Docker image on the GPU as the host user.

## Decisions (with Jeff, Oct 22)

- Name **On the Record**, package `ontherecord`, licence **MIT**.
- Web GUI on localhost, not PyQt (a pywebview window could wrap it later). uv installers for laptops, Docker for
  Linux servers.
- PewDiePie's Odysseus is AGPL-3.0: ideas only, no code.
- **One thing at a time.** Every job (a recipe, an import, a download, a queued command) waits for whatever is
  running, and the assistant switches off while a job runs or waits. "We just assume we're always resource
  constrained." Agents use `ontherecord exclusive -- CMD` for heavy work. Nothing heavy runs outside the queue.
- Models: official sources first. When an official file cannot run, the compromise is named and measured on our
  own tasks. Assistant tiers: E2B for a 4 GB laptop card, E4B from 6 GB; with nothing else running, it takes as
  much of the GPU as fits.

## Waiting on Jeff

1. **Downloads for the quantization comparison (15.3 GB).** Google's `gemma-4-E2B_q4_0-it.gguf` (3.35 GB) and
   `gemma-4-E4B_q4_0-it.gguf` (5.15 GB), and Unsloth's QAT `gemma-4-E2B-it-qat-UD-Q4_K_XL.gguf` (2.62 GB) and
   `gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf` (4.22 GB). Optionally Google's BF16 reference
   `google/gemma-4-E2B-it-qat-q4_0-unquantized` (10.2 GB), for our own KL measurement (`llama-perplexity
   --kl-divergence`). Unsloth's page reports a mean KL of 0.0017 (theirs) against 0.051 (plain Q4_0) for E2B. Their
   stated cause: llama.cpp's Q4_0 conversion recomputes the block scales instead of keeping QAT's. That claim is
   not yet checked independently.
2. **The paper's 26B file: found.** shiro's `gemma-4-26B-A4B-it-MXFP4_MOE.gguf` (15,419,239,968 bytes, sha256
   `9d46651aecae9701e7ec7c9dd48418f1d7bdecbb0adc41199eff79684c48ed6c`, quantized_by Unsloth) came from
   `noctrex/gemma-4-26B-A4B-it-MXFP4_MOE-GGUF`, downloaded Aug 3, 2026 (Chrome's download record). Hugging Face's
   SHA-256 for that file at main (`ba1408cd8891f9d3af97595ec838779ae8d0ec9b`, May 4, 2026) matches. It is in none
   of the 142 revisions of `unsloth/gemma-4-26B-A4B-it-GGUF`, whose MXFP4_MOE file changed six times between April
   and July. The catalog still pins Unsloth's July file (16.55 GB), so a fresh install gets a different model than
   the paper used. Open question for Jeff: pin noctrex's file instead?
3. **Projects and folders**: labels in one workspace (recommended) or a workspace per project, automatic
   transcript folders, speaker names. See [projects-and-folders.md](projects-and-folders.md).

## Next, in order

1. Read the stress-benchmarks run (`20261022-132631-stress-benchmarks`) against the poster's cascade files
   (`AITranscribe/stress_search/{stresstest,stresspresso}_repro/gemma-4-26B-A4B-it-mxfp4/`). On StressTest the
   poster had 36.7% unmarked, 76.6% top word, 72.9% WhiStress marks, 74.3% true words, and 86.2% WhiStress marks
   with the paper's prompt. Expect close agreement, not identical: our llama-server calls set `cache_prompt: false`,
   and the poster's file is the unlocated one above.
2. After the downloads: the assistant eval (`evals/assistant_eval.py`, 22 cases) on each E2B/E4B file, and the
   stress reader on E4B. Run them through `ontherecord exclusive`.
3. Windows test on Jeff's work machine following INSTALL.md; then macOS.
4. Receipts backfilled for the existing hands_on_dl outputs (the corpus as the paper used it).
5. Slurm backend (ROAR), if there is time; otherwise say so in the write-up.
6. The write-up (three pages, due Dec 1, 11:59 pm ET).

## Pitfalls

- Stop the GUI with `kill <serve pid>`. TaskStop kills the whole process tree. Jobs survive either way now, but the
  assistant's llama-server would not be stopped cleanly.
- shiro's system CUDA hides library-path bugs. Test installs in Docker or a fresh machine. Example, found Oct 23:
  hands_on_dl's `.venv-audio` has PyPI's `torch 2.11.0`, which on Linux is the CUDA 13 build; faster-whisper works
  there only because the system CUDA 12 supplies `libcublas.so.12`. `ontherecord setup --check` now says so.
- Stages run offline everywhere now (`config.LOCAL_ONLY`, Oct 22): `HF_HUB_OFFLINE=1`, and pyannote's usage
  metrics off. pyannote 4.0 sends them to otel.pyannote.ai by default (version, session id, audio duration,
  speaker counts), and `HF_HUB_OFFLINE` does not stop it, so shiro's diarization runs before Oct 22 sent them.
  A model or dataset must be in the cache before a run.
- On Oct 22, WhiStress was run outside the queue for about 7 minutes while the left-channel run diarized. That broke
  the rule above. Outputs are unaffected (the extraction started after it and used its usual offload), but two
  diarization timings in that run are inflated.
- `docs/private/` is gitignored: Jeff's turns, regenerated with `python docs/private/user_turns.py >
  docs/private/user-turns.md`.

## Resuming

```bash
cd ~/src/ontherecord
.venv/bin/ontherecord jobs                 # what is queued or running (the GUI runs the queue: serve on :8765)
.venv/bin/ontherecord show RUN_ID          # a run's grid and checks
.venv/bin/ontherecord models               # what is downloaded, and what this machine should use
.venv/bin/python -m pytest -q              # 55 tests; run it through `ontherecord exclusive` if a job is running
```
