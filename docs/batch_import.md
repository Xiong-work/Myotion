# Batch Import — organizing data and running a batch

Batch Import loads a whole cohort of participants at once, using a single
saved config (`.toml` file) instead of repeating the single-participant
"Load EMG Data" wizard for every person. This doc covers how to lay out your
data on disk and how to run it.

---

## The one rule: one task per batch

A batch config describes **one task** for **one cohort** (a group of
participants who share the same EMG channel set). If your study has multiple
tasks (e.g. gait *and* sit-to-stand), or cohorts with different channel sets
(e.g. affected vs. control groups wired up differently), run **Batch Import
once per task** with its own config file. This keeps segmentation and
processing unambiguous — there's never a question of "which task does this
file belong to."

Each participant folder should contain:

- **exactly one** task file (`.c3d` or `.mat`) for the task this batch is for
- **zero or more** optional MVC files (only needed if you're doing MVC
  normalization)

---

## Sample folder layouts

Batch Import doesn't require a specific folder naming scheme — it reads a
config file that tells it where to look. Two common layouts:

### 1. Flat: one folder per participant

```text
MyStudy/
├── P01/
│   └── P01_gait.c3d
├── P02/
│   └── P02_gait.c3d
└── P03/
    └── P03_gait.c3d
```

### 2. Grouped, with separate MVC and task subfolders

This is the richer layout (matches multi-cohort studies with a dedicated MVC
recording session per participant):

```text
MyStudy/
├── Affected_Group/
│   ├── A01/
│   │   ├── MVCs/
│   │   │   ├── tibialis_ant.mat
│   │   │   ├── gastroc.mat
│   │   │   └── ...
│   │   └── Tasks/
│   │       ├── task_lift.mat
│   │       └── task_bending.mat
│   ├── A02/
│   │   └── ...
│   └── A03/
│       └── ...
└── Control_Group/
    ├── C01/
    │   └── ...
    ├── C02/
    │   └── ...
    └── C03/
        └── ...
```

Note the `Tasks/` folder here holds **two** task files — that's fine on
disk, but it means this cohort needs **two** batch configs (one for
`task_lift`, one for `task_bending`), each pointing at just one of them. See
below.

Any consistent depth works — one level (flat), two levels (Group/
Participant), or more — as long as every participant folder in the batch
follows the same pattern.

---

## Running a batch

1. Open **Load EMG Data** → **Batch Import…** (this replaces the old "Scan
   Folder…" option; a single-participant add is still available via
   **Import File**).
2. Pick the batch **config** (`.toml`) directly — a normal file-open dialog,
   so you see every config sitting in a folder as you browse (this is why a
   config is expected to live in its own batch root folder, same convention
   as Pose2Sim's `Config.toml` sitting at the session root: `MyStudy/` in
   the examples above). The root folder is just wherever that file lives.
   - If a cohort has more than one task (e.g. `batch_config_lift.toml` /
     `batch_config_bending.toml`), just pick the one for this run.
   - **No config yet?** Cancel — you'll be asked to pick the root **folder**
     instead, then build a new config from scratch.
3. If building a new config, click **Detect from folder…** and select the
   root folder. Myotion inspects the real folder tree and pre-fills:
   - **Participant folder glob** — how many levels deep participant
     folders sit (`*` for the flat layout, `*/*` for Group/Participant,
     etc.)
   - **MVC file glob** — the subfolder + extension pattern for MVC files,
     if an "MVC"-named subfolder was found
   - **Task file glob** — if participants have more than one task file
     (like the `Tasks/` example above), you'll be asked to pick exactly
     one; if every participant has just one matching file, it's filled in
     automatically as a wildcard.

   This is a *starting guess*, not an automatic decision — every field
   stays editable, and nothing is scanned until you confirm the dialog.
   Fill in the **EMG Processing** section (filter/rectify/envelope/
   normalization) the same way you would for a single participant.
4. **Channel mapping:**
   - If the config has no channel mapping yet, you'll be asked to map
     channels using one representative participant (enable/disable, rename
     to a muscle label, assign an MVC file) — the same UI as the
     single-participant wizard. You'll be offered to save this mapping back
     into the config so you don't have to repeat it next time.
   - If the config already has a mapping, you'll just be asked to confirm it
     before loading (with a summary of who's about to be imported and who's
     being skipped, e.g. mismatched channel sets or already-loaded
     participants).
5. Participants are loaded and added to the workspace, saving the config's
   processing settings as a named entry alongside the other saved EMG
   configs and pre-selecting the new participants — so **BATCH PROCESS**
   is one click away, whether you start processing immediately or come
   back to it later. You'll be asked whether to start now.

## Editing after the fact

- **Edit Config…** — opens the same layout/processing form standalone, for
  building or tweaking a `.toml` without running an import.
- **Edit Mapping…** — select one or more already-loaded participants in the
  table and use this to change which channels are enabled, how they're
  renamed, or fix a mapping mistake, without having to reload from disk. Note
  this does **not** touch MVC file assignment — that's only set at import
  time. After editing, re-run **BATCH PROCESS** to apply the change.

## What's out of scope for the config

The `.toml` only controls **folder layout** and **EMG processing**
parameters. Trial cropping/segmentation is intentionally not part of it —
per-participant segmentation should be done in the **Kinematics Inspection**
module for consistency, not baked into a batch config.
