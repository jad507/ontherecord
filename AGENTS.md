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
```

- The queue is the GUI's queue (SQLite in `<workspace>/jobs.db`), so a person watching the GUI sees an agent's jobs
  and the reverse. One job runs at a time per workspace, which keeps two models off one GPU.
- Something must be running the queue: the GUI (`ontherecord serve`) or the headless `ontherecord dispatch`.
  `submit` and `jobs` say so when nothing is. Only one dispatcher runs per workspace; a second one waits.
- A job runs in its own process, detached from whatever started it: closing the GUI, or killing its process tree,
  does not stop the job. To stop it, use `ontherecord cancel`, which removes partial outputs and records the cancel.
- `ontherecord run RECIPE` runs in the foreground instead, outside the queue. Use it only when nothing else uses
  the GPU.

Exit codes for `run`, `wait` and `submit --wait`: 0 done, 1 failed, 2 recipe error, 3 cancelled.

## Imports from cameras and drives

```bash
ontherecord devices --json                                   # what is plugged in
ontherecord ingest SOURCE --project NAME --wait --json       # copy, verify, name, catalog (the "io" lane)
ontherecord clear IMPORT_RUN_ID --wait                       # delete those files from the device
```

Imports run in their own lane, so they do not wait for GPU jobs. `clear` refuses unless every imported file was
verified, re-reads each file from the device before deleting it, and never deletes a file that was not imported.
Do not run `clear` unless the person asked for it in so many words.

## The assistant's tools, and MCP

`src/ontherecord/tools.py` defines the tools the in-GUI assistant uses (find_recordings, search_transcripts,
recording_overview, read_transcript, transcribe, jobs_status, cancel_job, devices, import_device). `ontherecord mcp`
serves the same tools over stdio to any MCP client, e.g. `claude mcp add ontherecord -- ontherecord mcp`.
`evals/assistant_eval.py` scores a model on plain-English requests; run it after changing a tool's description.

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
- The server binds to 127.0.0.1. Do not expose it on another interface: it runs jobs and serves files from the
  workspace. From another machine, use an SSH tunnel (`ssh -L 8765:127.0.0.1:8765 HOST`).
- Tests: `pytest`. `tests/stages_for_tests.py` has tiny model-free stages, loaded through `ONTHERECORD_STAGES`.
  The same variable adds your own stages (`name=module:Class;...`) without changing this package.
