# Projects and folders: a proposal (October 23, 2026)

Not built yet; waiting on Jeff's choice below. What was built alongside it (the guided Setup, `transcribe`, picking
up where you left off, transcript files) is in [STATUS.md](STATUS.md).

## Today

- **Inputs** live anywhere. Files are read in place, links download into the workspace's store, and camera imports
  are filed under `Archive/<project>/...`.
- **Outputs** live in the store, `<workspace>/store/<stage>/<item>/<key>/`: content-addressed, right for receipts and
  caching, unreadable for a person. The Library reads them; the player's **Save** now writes .txt/.srt/.vtt.
- **Projects** exist only in imports. The Library, search and the assistant see every recording at once, so the
  stress-test items sit beside council meetings and camera footage.

## Recommended: projects as labels in one workspace

- A recipe may say `project: City Council 2026`; the run record keeps it, and imports already carry one. A
  recording belongs to every project whose runs touched it.
- A project picker in the header (All, each project, New project). The Library, search, Ask and Jobs show the
  current project; Transcribe a recording, Import and Build default to it. The choice is remembered like the tab.
- Each project gets a readable folder, `OnTheRecord/Projects/<name>/`:
  - `Transcripts/`: written when a run finishes, one .txt and .srt per recording, named after it, with speaker
    names where the project has them. These are copies; the store stays the record.
  - `project.yaml`: a description, the usual language, how many speakers to expect, and speaker names
    (`SPEAKER_02: Chair Smith`, per recording or for the whole project), which the Library and the exports use.
- The store stays shared, so a file in two projects is transcribed once.

## The alternative: one workspace per project

Each project in its own folder or drive, with its own store, job list and Library. It separates data most cleanly,
for example when two projects fall under different ethics approvals. Costs: no reuse between projects, switching
project means restarting the GUI on another workspace, and two projects' queues would need one machine-wide lock
(the machine lease covers the assistant today, not two dispatchers). It can come later, as an option for one
project ("keep this project's data in its own folder") on top of the labels.

## For Jeff to decide

1. Labels in one workspace (recommended), separate workspaces, or labels now with separate workspaces as an option
   later?
2. Write each project's `Transcripts/` folder automatically when a run finishes, or only when asked (a button)?
3. Speaker names per project in `project.yaml`, edited from the player (click a speaker to name them)?

## Next after that

Time estimates ("fits a lunch break, a long meeting, or overnight") from the receipts' `timing`, per stage and per
minute of audio on this machine, as in the Oct 22 notes. The Transcribe button is where an estimate belongs.
