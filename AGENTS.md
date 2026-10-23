# Notes for agents and scripts

How a coding agent (Claude Code, Codex, a CI job, a notebook) should drive On the Record. Everything the GUI does is
also a command, and the commands that report results take `--json`.

## Run things through the queue

```bash
ontherecord submit recipes/eleven-left.yaml --wait --json   # queue it, follow it, print the final run record
ontherecord submit RECIPE                                    # or just queue it; prints the run id
ontherecord jobs --json                                      # the queue, and which process is running it
ontherecord wait RUN_ID --json                               # block until it ends
ontherecord cancel RUN_ID                                    # stop it cleanly; returns once it has stopped
ontherecord transcribe FILE_OR_LINK... --wait --json         # speakers + transcript with no recipe (presets.py)
```

`transcribe` queues the ready-made recipe the GUI and the assistant use. It writes this machine's Whisper model,
compute type and (with `--comments`) extraction model and offload into the recipe, so the run record shows them;
`--show` prints the recipe instead. It refuses, naming them, when the models it needs are not downloaded.

- The queue is the GUI's queue (SQLite in `<workspace>/jobs.db`), so a person watching the GUI sees an agent's jobs
  and the reverse. One job runs at a time per workspace, which keeps two models off one GPU.
- Something must be running the queue: the GUI (`ontherecord serve`) or the headless `ontherecord dispatch`.
  `submit` and `jobs` say so when nothing is. Only one dispatcher runs per workspace; a second one waits.
- A job runs in its own process, detached from whatever started it: closing the GUI, or killing its process tree,
  does not stop the job. To stop it, use `ontherecord cancel`, which removes partial outputs and records the cancel.
- One thing runs at a time on the machine: a job of any kind starts only when no other job is running, and the
  assistant switches off while a job runs or waits. Most machines this runs on cannot fit two models at once, and
  timings taken while something else runs mean little.
- So never run heavy work outside the queue (a model, an evaluation, ffmpeg, a script that loads torch). Queue it:
  `ontherecord exclusive -- COMMAND [ARGS...]` waits its turn, runs alone, prints the output and exits with the
  command's exit code. `ontherecord run RECIPE` runs a recipe in the foreground, outside the queue: only when the
  queue is empty (`ontherecord jobs`) and nobody is using the Ask tab.

Exit codes for `run`, `wait` and `submit --wait`: 0 done, 1 failed, 2 recipe error, 3 cancelled.

## Imports from cameras and drives

```bash
ontherecord devices --json                                   # what is plugged in
ontherecord ingest SOURCE --project NAME --wait --json       # copy, verify, name, catalog (a queued job)
ontherecord clear IMPORT_RUN_ID --wait                       # delete those files from the device
```

Imports wait their turn in the queue like any job. `clear` refuses unless every imported file was
verified, re-reads each file from the device before deleting it, and never deletes a file that was not imported.
Do not run `clear` unless the person asked for it in so many words.

## The assistant's tools, and MCP

`src/ontherecord/tools.py` defines the tools the in-GUI assistant uses (find_recordings, search_transcripts,
recording_overview, read_transcript, public_comments, transcribe, jobs_status, cancel_job, devices,
import_device). Each answers in short plain text with quotes, times and `#play/RUN/ITEM/SECONDS` links; the ones
that start or stop work only propose, and the person confirms. Write tool descriptions for a small model: say
what the arguments may look like (dates, words), because a 4B model will not guess that an id can be a date. `ontherecord mcp`
serves the same tools over stdio to any MCP client, e.g. `claude mcp add ontherecord -- ontherecord mcp`.
`evals/assistant_eval.py` scores a model on plain-English requests; run it after changing a tool's description.

## Setting up a machine

```bash
ontherecord setup --check          # this machine: GPUs, driver, the PyTorch and llama.cpp builds it suits, models
ontherecord setup                  # fetch ffmpeg and llama-server if missing; write a machine config if none
ontherecord models                 # the catalog and what fits this machine
ontherecord models get NAME        # pinned revision, checked against Hugging Face's SHA-256
```

`src/ontherecord/hwdetect.py` (standard library only; the installers run it before installing anything) decides
from `nvidia-smi` which PyTorch build the machine gets (a dependency group: `cu128` by default, `cu126` for cards
older than Turing or drivers older than CUDA 12.8, `cpu` without an NVIDIA card) and which llama.cpp build (CUDA
13.4, CUDA 12, Vulkan or CPU, verified with `llama-server --list-devices`, falling back down that list). PyTorch's
CUDA 13 build is out on purpose: CTranslate2 (faster-whisper) needs CUDA 12's cuBLAS, and the CUDA 12 and 13 cuDNN
wheels install the same files. Nobody needs the CUDA Toolkit, only a new enough driver.

Model downloads are large (3 to 17 GB each): ask the person before starting one. The Hugging Face token goes in
the machine's secrets file (`--save-token`), never in a recipe, a receipt or the log.

## Before running: plan

`ontherecord plan RECIPE --json` says, for every item and stage, whether it will be `cached` (reused), `run`,
`run-if-changed` (runs unless the stage before it produces byte-identical output) or `blocked` (by a failed
check). A cell with `mixed` takes inputs that trace back to two different stages making the same artifact. Plan
costs nothing, so check it before submitting anything long.

## Reading results

`ontherecord show RUN_ID --json` prints the run record plus `problems`, a flat list of every warn or fail check and
every failed or blocked cell. Each cell's `receipt` is a path relative to the workspace:

```
<workspace>/runs/<run_id>/run.json        the run: stages, settings, and one cell per item x stage
<workspace>/runs/<run_id>/log.txt         everything the workers printed
<workspace>/store/<stage>/<item>/<key>/   one stage's outputs for one item, and receipt.json
```

A receipt holds `settings` (defaults included; prompt files as name, sha256 and text), `inputs` and `outputs` (paths
and sha256), `checks`, `metrics`, `tools` and model revisions, `environment` (packages, GPU, ontherecord commit),
`device` and `timing`. `ontherecord receipt RUN_ID ITEM STAGE` prints one.

The GUI's HTTP API (127.0.0.1:8765 by default) serves the same: `GET /api/jobs`, `POST /api/jobs` with
`{"text": "<recipe yaml>"}`, `POST /api/jobs/{run_id}/cancel`, `POST /api/plan`, `GET /api/runs/{run_id}`,
`GET /api/receipt?path=...`, `GET /api/output?receipt=...&name=...`.

## Writing recipes

`ontherecord stages` lists every stage with its settings, defaults and help. A recipe names its sources (`urls`,
`url_file`, or `files`, which may be globs) and its stages in order. A setting left out takes the default, and the
default is recorded. Per stage entry:

- `on_fail: continue` runs later stages even when this stage's checks fail (to measure the damage).
- `as: NAME` names the step, needed when a stage type appears twice.
- `from: {artifact: NAME}` takes that input from the named earlier step instead of the latest one.

If a stage's inputs trace back to different bytes of one artifact, its `same_source` check fails. See
`recipes/eleven-as-coded.yaml`, which reproduces the corpus mistake on purpose.

## Rules

- Do not edit or delete anything under `<workspace>/store/` by hand: entries are content-addressed, and receipts
  vouch for their bytes. To redo something, change a setting or the input, and the runner computes a new entry.
- `machine.yaml` is per machine and gitignored. Secrets go in the file named by its `env_file`. They reach stages
  as environment variables and are never written to receipts; do not print them either.
- Nothing talks to the network unless a person asked for it: `setup`, `models get`, the download stage (yt-dlp)
  and an `assistant.endpoint` a person configured. Stages run with `HF_HUB_OFFLINE=1`,
  `HF_HUB_DISABLE_TELEMETRY=1` and `PYANNOTE_METRICS_ENABLED=0` (`config.LOCAL_ONLY`); a machine's `stage_env`
  can override them on purpose. A model or dataset must be fetched before a run needs it. Do not add a stage,
  library or page asset that fetches, checks for updates or reports usage.
- The server binds to 127.0.0.1. Do not expose it on another interface: it runs jobs and serves files from the
  workspace. From another machine, use an SSH tunnel (`ssh -L 8765:127.0.0.1:8765 HOST`). It answers only requests
  addressed to 127.0.0.1, localhost or ::1 (DNS rebinding), and refuses a POST whose Origin is another page; keep
  both checks when adding routes.
- Every download is pinned by SHA-256 in the code (bootstrap.py, model_hashes.py, install/uv-pinned.sh), never
  "the latest". To move a pin, take a release that is a few weeks old and write its digest.
- Tests: `pytest`. `tests/stages_for_tests.py` has tiny model-free stages, loaded through `ONTHERECORD_STAGES`.
  The same variable adds your own stages (`name=module:Class;...`) without changing this package.
