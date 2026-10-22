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

## Built and tested (shiro, Linux, RTX 5070 Ti)

- 55 tests pass (`pytest`). Commits on `main`, none pushed.
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
2. **Where did the paper's 26B file come from?** shiro's `gemma-4-26B-A4B-it-MXFP4_MOE.gguf` (15,419,239,968
   bytes, sha256 `9d46651aecae9701e7ec7c9dd48418f1d7bdecbb0adc41199eff79684c48ed6c`, quantized_by Unsloth) is in
   none of the 142 revisions of `unsloth/gemma-4-26B-A4B-it-GGUF`. That repo replaced its MXFP4_MOE file under the
   same name six times between April and July 2026. The catalog pins the July file (16.55 GB), so a fresh install
   gets a different model than the paper used. `ontherecord models` already says "a different file by that name".

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
- shiro's system CUDA hides library-path bugs. Test installs in Docker or a fresh machine.
- `HF_HUB_OFFLINE=1` is set for stages on shiro (`stage_env`). A model or dataset must be in the cache before a run.
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
