# What PyIncucyte needs for the automatic SCN pipeline

`CLAUDE.md` already states this repository's job: stage 1 of the automated
pipeline, **meant to be imported by the rest of the pipeline, not shelled out
to**. This file is the short list of what still stands between that intent and a
hands-off run.

```
download from the Incucyte  ->  find the SCN  ->  outline / orient / crop  ->
register, filter, trace  ->  test the rhythm  ->  render videos
```

The stage after this one is Auto-Organotypic, which reads a folder of stacks plus
`pyincucyte-manifest.json` and outlines the suprachiasmatic nucleus in each.

Sibling plans:

- `Auto-Organotypic/docs/scn-pipeline-plan.md` — the consumer, and the canonical
  handoff contract
- `PyLV200/SCN_PIPELINE_PLAN.md` — the other acquisition stage, which needs
  considerably more work than this one

## Status, 2026-08-24 — items 1 to 5 are done

All of it landed in `models.py`, `client.py`, `manifest.py` and `watch.py`; no
written pixel, filename or request path changed. 444 tests pass offline, 29 of
them new in `tests/test_pipeline_handoff.py`, and the output was read back with
Auto-Organotypic's own `sources.read_manifest` — `scn_channel` for red resolves to 2,
`complete` reads `true` for a finished pull and `false` for a live one, and both
derived values arrive intact.

**The manifest entry is now the contract table verbatim**, rather than this
plan's own draft of it. `channels` holds one `{index, name, image_type,
source}` record per plane; `frames` replaced `frame_count` and `interval_s`
replaced this plan's `interval_seconds`; `field`, `complete`, `frames_expected`
and `blank_planes` are new. `registered` and `valid_mask` are deliberately not
written — Auto-Organotypic's plan says they belong to whatever aligns the pixels, and
an instrument cannot know whether its output was later registered.

That took the sibling with it, in the same pass: `sources._from_incucyte` now
prefers the stated records and falls back to the old shape, and its fixture
grew a `legacy=True` mode so folders pulled before the change still read. Both
suites pass — 444 here, 255 there.

One thing came out differently from what this plan assumed. **The Incucyte
*does* report a pixel size.** `ImageSize.MicronsPerPixel` is on every
`GetAllSearchVessels` record — 2.824051 µm at 4x, in the captured
`.tmp/vessels_with_scan.json`. So `pixel_size_um` is a read fact, not the honest
`null` this plan expected, and every micron measurement downstream gets a real
number. It is not written into the TIFF (engine.py passes no `resolution=`), so
the manifest is the only place it survives.

Item 4's `on_file` landed at the same time rather than waiting, because the
watcher needed a completion signal anyway and both touch the same call.

### Still open

- **`complete` for a plain download of a live experiment.** A stated `end_at`
  that has passed gives `true`; a watcher gives `false` then `true` on the
  flush. An open-ended `download` gives `null`, because `end_at=None`/`"now"`
  slide with the clock and nothing else in the payload says the experiment has
  finished. The real signal exists — `Scheduling` knows whether the vessel has a
  next scan — but reading it would put a network call into every download, which
  this plan explicitly rules out. A caller that wants it can read
  `client.scan_pattern(38)` and pass `complete=` itself.

---

## Where this package actually stands

Close. The importable surface is already the right shape:

- `IncucyteClient.fetch()` plans and downloads in one call and returns a
  `DownloadResult` (`pyincucyte/client.py:978`).
- `IncucyteClient.watch(on_result=...)` returns a threaded `Watcher`
  (`pyincucyte/client.py:985`) so a pipeline can start a standing order and get
  on with analysing what has landed.
- `manifest.py` already exists, already merges across polls, and its docstring
  already names the consumer: *"The next stage of an automated pipeline (find
  the SCN, outline, crop, analyse) should read this file rather than globbing
  the folder and re-parsing filenames."*

The output format needs nothing. `layout="time_channel_stack"` writes ImageJ
`TCYX`, which is exactly what the outline's `_source_layout` wants — one Y axis,
one X axis, YX last.

What is left is five fields the consumer needs and one callback. All of it lands
in `models.py` and `manifest.py`.

---

## The work

### 1. Put a one-based channel index on each file entry

The downstream outline takes `scn_channel` as a **one-based ImageJ index**, and
this package offers a consumer two things that are neither:

- `OutputFile.channels` — display names in stack order
- `OutputFile.image_types` — the device's own channel numbering

So a consumer that wants "which number is red in this file" has to know that the
answer is the position in stack order plus one, and get that inference right.
Write it down instead:

```json
"channels": [{"index": 1, "name": "Phase",  "image_type": 1},
             {"index": 2, "name": "Red",    "image_type": 3}]
```

`index` describes the written stack, not the vessel — a well that missed a
channel would otherwise shift every name by one.

*Done when* a consumer can compute `scn_channel` from one file entry alone,
without knowing this package's ordering rules.

### 2. Say whether the file is finished

`tiffstack.py` extends an ImageJ time stack in place on each poll — that is the
whole point of it, and it is why a week-long watch does not rewrite gigabytes
every hour. The consequence for a downstream reader is that `frame_count` in the
manifest is true at the moment it was written and stale immediately afterwards.

Add `complete` and, where it is known, `frames_expected`.

Without it Auto-Organotypic cannot resume a live run correctly: its skip rule keys on
its own report existing, so a stack that grew from 40 frames to 400 is silently
skipped and never re-outlined. With it, the consumer can also decline to outline
a stack that is still filling, which matters because the newest frames are the
ones most likely to be partial.

A watch that has been told the experiment is over — or a fetch of a vessel whose
`last_scan` has passed — knows the answer. Where it genuinely does not, say
`null` rather than guessing.

*Done when* a manifest written mid-watch says `complete: false` and one written
after the run ends says `true`.

### 3. Add `interval_seconds` and `pixel_size_um`, each with a `source`

`scan_times` are on every file, so the interval is derivable — but the rhythm
step at the end of this pipeline should be handed a stated cadence rather than
re-deriving one from timestamps, and a derived value should say that it was
derived.

Pixel size is absent entirely, and every micron-denominated measurement
downstream needs it.

Borrow the pattern from the sister project: PyLV200's manifest gives each
derived value a `source` string — read from a sidecar, given on the command
line, or inferred — so a consumer can tell a fact from a guess. Do the same
here:

```json
"interval_seconds": {"value": 1800, "source": "derived from scan times"},
"pixel_size_um":    {"value": null, "source": "not known - the device does not report it"}
```

`null` with an honest reason is a better answer than a plausible number.

*Done when* both appear on every file entry, each carrying where it came from.

### 4. Add a per-file callback to the watcher

`Watcher` fires `on_result` once per poll with a whole `DownloadResult`. For a
pipeline that is workable but coarse: a poll that collects 96 wells delivers
nothing until all 96 have landed.

`engine`'s download already tracks per file — `on_file(fname, size, done,
total)` closures exist at `pyincucyte/client.py:931` and `:944`. Surface an
`on_file=` on `Watcher` and on `download()`.

The outline is roughly thirty seconds of single-threaded work per field, so
starting well A1 while B1 is still downloading is most of what makes the
pipeline feel continuous rather than batched.

*Done when* a watcher can drive a downstream step file by file.

### 5. Leave `layout="separate"` exactly as it is

Recorded here so nobody 'fixes' it. That layout writes one `YX` TIFF per well
*per channel per timepoint* — thousands of files, each of which the SCN step
would treat as a recording and spend thirty seconds outlining.

It is already marked in the manifest as `axes: "YX"`, which is all a consumer
needs to refuse it. **The refusal belongs in Auto-Organotypic**, which knows what its
own step costs; this package has no business knowing that, and adding a warning
here would put an assumption about a downstream consumer into a general-purpose
downloader.

No work. The field that makes it detectable already exists.

---

## What this plan does not change

- **`engine.py`.** The bearer-token REST path and the ImageJ TIFF writing are
  proven and cannot be tested against the real instrument from off-site. Items 1
  through 3 add fields to `models.OutputFile` and `manifest._file_entry`
  (`pyincucyte/manifest.py:72`); none of them changes a written pixel or a
  filename.
- **The import direction.** `tiffstack.py` and `processing.py` are imported
  *by* `engine.py` at call time and must never import it. Nothing here touches
  that.
- **Filenames.** `VID{vessel}_{well}_{channels}_{stamp}.tif` stays. The whole
  point of the manifest is that nothing downstream has to parse it, and changing
  it would break the resume ledger's state keys.

## Order

Items 1 through 3 are all edits to the same two places — `models.OutputFile` and
`manifest._file_entry` — and should land together as one change. Item 4 is
independent and can wait until the reactive path is actually being built. Item 5
is a decision already made.

None of this blocks Auto-Organotypic from starting its adapter: it can be built
against a checked-in fixture of the manifest shape and updated as these fields
appear. Each field landed here is a piece of guesswork removed from there.
