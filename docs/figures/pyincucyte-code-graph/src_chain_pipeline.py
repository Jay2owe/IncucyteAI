"""One headless command, from an instrument experiment to finished traces.

``docs/automated-scn-pipeline.md`` stage 3. The pipeline is the thin thing that
calls three separately installable packages in order — this one, and an
instrument client at either end of it:

```
pull TIFFs  ->  broad crop  ->  register  ->  outline / orient / crop  ->
[ still | sheet | movie | mosaic ]  ->
remove cosmic rays, trace each region, test the rhythm
```

The four in brackets are the display exports, and they are opt-in and a dead
end by design: nothing downstream reads what they write, nothing may be measured
from it, and they hand the recordings on untouched.

**This module is the order and nothing else.** Every step belongs to the package
that owns it, and each is reached by *dotted name* rather than by import — the
same device PyMicroglia's action registry uses, and for the same reason. A stage
whose package is not installed is reported as **pending**: a half-installed
pipeline is then a visible state at the top of a run rather than an ImportError
somewhere in the middle of one, and importing this module costs nothing but the
standard library.

Two things follow from that, and both are the point.

**Auto-Organotypic never imports upward.** Nothing here imports PyMicroglia, at module
scope or anywhere else, and since 2026-08-26 nothing here calls it either: the
trace stage is a *region* trace and lives in this package. PyMicroglia imports
*this* one — fourteen of its modules are this package's under an old name — and
that arrow runs downhill, which is the direction that was always wanted. The
single-cell analysis is still PyMicroglia's and is reached by running
PyMicroglia, not by installing it as an extra of this.

**Reading a finished pull needs no extra.** ``pip install auto-organotypic`` gets numpy,
scipy, tifffile and scikit-image, and that is enough to index a folder, outline
it and write the run record. The optional extras are needed only to *start* an
acquisition, to render a picture, or to put a period on a trace; a stage that
needs one says which.

What the run record is for: every stage appends what it did, what it produced
and how long it took, so a run that stopped four stages in says where and why
without anybody reading a log. It is written after every stage, not at the end,
because the stage that fails is the one whose record matters.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import (config, corrections, hand_drawn, io as _io, layout, sources,
               staleness, store, windows)
from . import batch as _batch
from .batch import MANIFEST_NAME
from .sources import Recording

__all__ = [
    "PIPELINE_VERSION",
    "RUN_RECORD_NAME",
    "STAGES",
    "Stage",
    "StagePending",
    "describe",
    "grid_of",
    "resolve",
    "run_pipeline",
    "stage_names",
]

#: The order versions separately from every method it calls. Adding a stage is
#: not a change to an outline, a registration or a trace.
PIPELINE_VERSION = "2026-08-24-pipeline-v1"

RUN_RECORD_NAME = "auto-organotypic-pipeline-run.json"


class StagePending(RuntimeError):
    """A stage whose implementation is not installed, or not written yet."""


@dataclass(frozen=True)
class Correctable:
    """One group of settings a person may answer instead of measuring.

    A group rather than a field because what matters about a drawing is *which
    picture it was made on*, and that is shared by a few settings at a time and
    differs between them: the outline is drawn on the plane the outline reads,
    the square crop is cut from the oriented canvas, and an angle has no
    position at all.

    ``preview`` and ``frame`` are prose, for a person to act on. ``picture`` is
    the machine half — how to find the file, resolved by :func:`_drawn_on` —
    and is empty for an answer that names no place.

    ``flag`` and ``reader`` say which pipeline option carries a folder of
    drawings for this group and which :mod:`auto_organotypic.hand_drawn` function
    reads it. They are on the table rather than written out at each call site
    because three places now need to know that a broad crop is read with
    ``boxes_for`` and an outline with ``outlines_for``: the stage, the grid
    that says what the stage would do, and ``auto-organotypic correct``. Three copies
    of that pairing is three chances for one of them to read a drawing with the
    wrong reader and record an answer nothing will ever apply.
    """

    fields: tuple[str, ...]
    preview: str
    frame: str
    picture: str = ""
    flag: str = ""
    reader: str = ""
    #: The field a bare value from ``reader`` becomes. ``boxes_for`` hands back
    #: four numbers rather than a mapping, because that is the shape
    #: ``crop_recordings`` takes; everything else already names its field.
    wrap: str = ""

    def read(self, recordings, rois, *,
             if_missing: str = "auto") -> dict[str, dict[str, Any]]:
        """A folder of drawings as ``{path: {field: value}}``.

        One shape whichever reader answered, so a caller merging these into
        per-recording settings never has to ask which group it is holding.
        """
        if not self.reader:
            raise ValueError(f"{self.fields} is not read from a drawing")
        found = getattr(hand_drawn, self.reader)(recordings, rois,
                                                 if_missing=if_missing)
        if not self.wrap:
            return found
        return {path: {self.wrap: value} for path, value in found.items()}


@dataclass(frozen=True)
class Stage:
    """One step of the pipeline, and where its implementation lives.

    ``target`` is ``"module:attribute"``, resolved when the stage runs. ``needs``
    names the extra that provides it, so a pending stage can say what to install
    rather than only that it is missing. ``covers`` names the steps in the
    pipeline diagram that this one call subsumes — the trace stage is three
    boxes of that diagram, and pretending otherwise would make the run record
    lie about what happened.

    ``probes`` names third-party modules the target imports *lazily*, inside the
    function that needs them. Resolving the target would not touch those, so
    without this a stage reports ready and then fails on an import several
    minutes into a run — which is the exact failure this module exists to turn
    into a line at the top. Registration is the case: it lives here now, so
    resolving it always succeeds, while the estimator it calls needs
    scikit-image.

    ``corrigible`` says what a person may answer instead, what to draw it on,
    and which coordinate system that is. A table on the stage and **not a
    module per stage**: the interesting invariant is the frame, six rows of it
    have to be readable against each other, and six files holding six ten-line
    functions would scatter the one thing that actually goes wrong. It is the
    same shape ``chain.Step`` uses for ``plan``, ``settle`` and ``check`` —
    a registry, not a package.
    """

    name: str
    owner: str
    target: str
    summary: str
    needs: str = ""
    opt_in: bool = False
    covers: tuple[str, ...] = ()
    probes: tuple[str, ...] = ()
    corrigible: tuple[Correctable, ...] = ()

    @property
    def fields(self) -> tuple[str, ...]:
        """Every setting this stage will take an answer for."""
        return tuple(name for group in self.corrigible
                     for name in group.fields)

    def group_for(self, field: str) -> Correctable | None:
        """Which picture this field's answer belongs to, if any."""
        for group in self.corrigible:
            if field in group.fields:
                return group
        return None


#: The pipeline, in order. Ownership follows ``docs/automated-scn-pipeline.md``'s
#: table; anything that is not this package's is reached by name.
STAGES: tuple[Stage, ...] = (
    Stage("acquire", "PyIncucyte / pylv200", "",
          "pull the recordings off the instrument",
          needs="auto-organotypic[incucyte] or auto-organotypic[lv200]", opt_in=True),
    Stage("index", "Auto-Organotypic", "auto_organotypic.sources:read_manifest",
          "read the pull's manifest as recordings",
          corrigible=(
              # The cheapest correction in the pipeline by a wide margin: a
              # dead well dropped here costs nothing at the broad crop, nothing
              # at registration, nothing at the outline and nothing at the
              # trace. It is done today by moving a file out of a folder, which
              # leaves no record of why.
              Correctable(("exclude",),
                          "nothing — this is a judgement about a whole "
                          "recording, not about a place in one",
                          "no frame: in the run or out of it"),)),
    # Two trim entries and one function, because ``STAGES`` is a fixed tuple
    # and a stage cannot choose its own position at run time. The usual trim is
    # the later one: the copy it writes is a fifth of the frame area, the broad
    # crop having already been taken. This early one exists only for a
    # recording whose bad frames fooled the crop detector, and costs a
    # full-frame copy of every recording it touches. Both are no-ops when no
    # window was given, which is what lets them sit here unconditionally.
    Stage("trim_before_crop", "Auto-Organotypic", "auto_organotypic.trim:trim_recordings",
          "drop frames the crop detector should not see",
          corrigible=(
              Correctable(("window",),
                          "nothing — a window names frames, not a place in "
                          "one",
                          "no frame: which frames are in the run"),)),
    Stage("broad_crop", "Auto-Organotypic", "auto_organotypic.broad_crop:crop_recordings",
          "a coarse box around the tissue, so registration works on a small frame",
          opt_in=True,  # opt-in, not absent: see the stage function below
          corrigible=(
              Correctable(("region",), "the raw recording, as it came off the "
                                       "instrument",
                          "the raw sensor frame", picture="source",
                          flag="broad_crop_rois", reader="boxes_for",
                          wrap="region"),)),
    Stage("trim", "Auto-Organotypic", "auto_organotypic.trim:trim_recordings",
          "drop frames registration should not pay for",
          corrigible=(
              Correctable(("window",),
                          "nothing — a window names frames, not a place in "
                          "one",
                          "no frame: which frames are in the run"),)),
    # Auto-Organotypic's since stage 6, and the change is not cosmetic: this stage
    # used to require the whole single-cell package, so a machine that only
    # wanted a registered stack was told it could not have one.
    Stage("register", "Auto-Organotypic",
          "auto_organotypic.registration:estimate_and_apply",
          "align every frame, and crop to the overlap they all cover",
          needs="auto-organotypic[register]",
          # OpenCV is not probed: only the sequential three-channel estimator
          # uses it, and ``estimate_and_apply`` is the reference one.
          probes=("skimage",),
          corrigible=(
              # Not a drawing. A shift table is data, read and checked for its
              # sign before it is believed; see ``docs/given-registration.md``
              # and ``hand_drawn.read_shifts``. It has no frame because it is
              # not about a place, it is about a whole recording's motion.
              Correctable(("shifts", "method", "registration_channel"),
                          "a table of shifts measured elsewhere, one row per "
                          "frame — see docs/given-registration.md",
                          "no frame: a table, not a region",
                          reader="shifts_for", wrap="shifts"),)),
    # The one stage that hands the next one *more* recordings than it was
    # given. After the register and not before it, so both epochs of a slice
    # share one drift correction and one overlap crop: two halves registered
    # separately are measured through different pixels, and the difference
    # lands in the answer as though it were biology.
    Stage("split", "Auto-Organotypic", "auto_organotypic.split:split_recordings",
          "one registered recording as several, each complete in itself",
          corrigible=(
              Correctable(("segments",),
                          "nothing — a segment names frames, not a place "
                          "in one",
                          "no frame: where one epoch ends and the next starts"),)),
    Stage("outline", "Auto-Organotypic", "auto_organotypic.batch:run",
          "the accepted two-lobe outline, orientation and square crop",
          corrigible=(
              Correctable(("outline_roi",),
                          "the plane the outline reads: the "
                          "*_OUTLINE_INPUT_*.tif a previous run wrote",
                          "the registered stack's selected plane",
                          picture="outline_input",
                          flag="outline_rois", reader="outlines_for"),
              # The one exception to the frame rule, and worth its own row.
              # An angle has no position, so the same stroke means the same
              # thing on the raw recording and on the registered plane. It must
              # still not be drawn on a picture somebody has already rotated.
              Correctable(("orient_up_deg", "orient_flip", "orient_roi"),
                          "any frame nobody has already rotated",
                          "a direction, so no position",
                          flag="orient_rois", reader="orientations_for"),
              Correctable(("crop_region", "crop_roi"),
                          "the *_ORIENTED_SOURCE.tif a previous run wrote, "
                          "which is the frame the square is cut from",
                          "the oriented canvas",
                          picture="oriented_source",
                          flag="outline_crop_rois", reader="crops_for"),)),
    # The two display exports, and the only stages here whose output nobody may
    # measure from: both mark what they write as a display artefact and
    # ``guards.require_measurement`` refuses it. They sit after the outline and
    # before the trace so that a still costs the cheap half of the pipeline and
    # not the multi-hour half — "index, outline and show me" is a complete run
    # on a machine with nothing installed beyond this package.
    #
    # Opt-in for the reason ``broad_crop`` is: a person who wanted traces did
    # not ask for ninety-six PNGs, and rendering them is minutes of a run that
    # produced nothing anybody asked for.
    Stage("image", "Auto-Organotypic", "auto_organotypic.image:stack_to_image",
          "one still per recording — a projection, or a named frame",
          opt_in=True),
    # One sheet for the whole plate, not one per recording: the only stage in
    # this table that produces a single artefact from all of them at once, and
    # the reason ``_grid_sheet`` takes the recordings as a list where every
    # other runner loops.
    Stage("grid", "Auto-Organotypic", "auto_organotypic.grid:stack_to_grid",
          "every recording, or every moment, tiled into one sheet",
          opt_in=True),
    # The moving pair. Unlike the two above these can be *pending*: an encoder
    # is a real dependency and ``auto-organotypic[video]`` ships it, so a machine
    # without it is told at the top of the run rather than after the outline
    # stage has finished. ``imageio_ffmpeg`` is probed because it is imported
    # inside ``encode.available`` rather than at module scope, and a stage that
    # reported ready and then failed on that import is the exact thing the
    # probe list exists for.
    Stage("video", "Auto-Organotypic", "auto_organotypic.video:stack_to_video",
          "one movie per recording",
          needs="auto-organotypic[video]", probes=("imageio_ffmpeg",),
          opt_in=True),
    Stage("video_grid", "Auto-Organotypic",
          "auto_organotypic.video_grid:stack_to_video_grid",
          "every recording playing at once, tiled into one movie",
          needs="auto-organotypic[video]", probes=("imageio_ffmpeg",),
          opt_in=True),
    # A trace per *region* of the outline, and no cells. Which pixels are one
    # cell is per-object work and is PyMicroglia's; the decoy control goes with
    # it, because a whole lobe has no area-matched patch of tissue to sit
    # beside and the test is undefined rather than merely expensive. What a
    # region trace stands on instead is the instrumental control, which asks a
    # question a region can answer. ``segment`` therefore leaves ``covers``:
    # this stage does not do it, and claiming it would make the run record lie.
    Stage("trace", "Auto-Organotypic", "auto_organotypic.region_trace:run",
          "one trace per region of the outline, by way of the cosmic-ray "
          "rule, the instrumental control and the rhythm verdict",
          needs="auto-organotypic[rhythm]",
          probes=("circadian_workbench",),
          covers=("cosmic_rays", "trace", "rhythm")),
    # Last, and it measures nothing. Every stage above records what it decided
    # and how sure it was; this one reads those records back and draws them,
    # so a person can see in a minute what would otherwise be six JSON files
    # per recording. Display only, like the four exports, and opt-in for the
    # same reason: somebody who wanted traces did not ask for a contact sheet.
    #
    # It is not one of ``EXPORTS`` because it is not about a recording. The
    # four exports each render a stack; this reads a whole finished run —
    # the run record, the manifest, every stage's own report — so it takes the
    # output root and not a list of recordings.
    Stage("review", "Auto-Organotypic", "auto_organotypic.review:run_to_review",
          "how well the run went, as pictures: a scorecard, the crop and "
          "orientation strip, and each stage's own confidence",
          needs="auto-organotypic[image]", probes=("matplotlib", "PIL"),
          opt_in=True),
)

#: Which public client performs an acquisition, by instrument.
INSTRUMENTS: dict[str, str] = {
    "incucyte": "pyincucyte:IncucyteClient",
    "lv200": "pylv200:LV200Client",
}


def stage_names() -> tuple[str, ...]:
    return tuple(stage.name for stage in STAGES)


def _stage(name: str) -> Stage:
    for stage in STAGES:
        if stage.name == name:
            return stage
    raise KeyError(f"no such stage: {name!r}; the pipeline is "
                   f"{', '.join(stage_names())}")


# ------------------------------------------------------------- resolution
def _probe(target: str) -> None:
    """Check the lazy imports a target will reach for once it is running."""
    for stage in STAGES:
        if stage.target != target:
            continue
        for module_name in stage.probes:
            if importlib.util.find_spec(module_name) is None:
                raise StagePending(
                    f"{module_name} is not installed, and {stage.name} imports "
                    f"it once it is already running")


def resolve(target: str) -> Callable[..., Any]:
    """``"module:attribute"`` -> the callable, or :class:`StagePending`.

    By name and not by import so that this module has no dependencies of its
    own. An action whose target cannot be resolved is a reported state, not a
    crash — thirteen of PyMicroglia's were pending at one point, and a
    half-ported pipeline being *visible* is what stopped that being a surprise.
    """
    module_name, _, attribute = str(target).partition(":")
    if not module_name or not attribute:
        raise StagePending(f"{target!r} is not a 'module:attribute' target")
    _probe(target)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise StagePending(f"{module_name} is not installed: {exc}") from exc
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise StagePending(
            f"{module_name} has no {attribute}: {exc}") from exc


def describe() -> list[dict[str, Any]]:
    """Every stage, and whether its implementation is here.

    What ``auto-organotypic pipeline --check`` prints. Worth running before a nine-day
    experiment rather than after one.
    """
    out = []
    for stage in STAGES:
        row = {"stage": stage.name, "owner": stage.owner,
               "summary": stage.summary, "opt_in": stage.opt_in,
               "target": stage.target, "covers": list(stage.covers)}
        if not stage.target:
            row["status"] = "chosen at run time"
        else:
            try:
                resolve(stage.target)
                row["status"] = "ready"
            except StagePending as exc:
                row["status"] = "pending"
                row["reason"] = str(exc)
                if stage.needs:
                    row["install"] = stage.needs
        out.append(row)
    return out


# ------------------------------------------------------------- run record
class _Run:
    """The run record, written after every stage rather than at the end."""

    def __init__(self, folder: Path, target: Path | None,
                 on_progress: Callable[[Mapping[str, Any]], None] | None):
        self.folder = folder
        self.target = target
        self.on_progress = on_progress
        self.payload: dict[str, Any] = {
            "tool": "AUTO_ORGANOTYPIC_PIPELINE",
            "pipeline_version": PIPELINE_VERSION,
            "contract_version": sources.CONTRACT_VERSION,
            "folder": str(folder),
            "complete": False,
            "stages": [],
        }

    def note(self, name: str, status: str, seconds: float,
             **detail: Any) -> dict[str, Any]:
        entry = {"stage": name, "status": status,
                 "seconds": round(seconds, 3), **detail}
        self.payload["stages"].append(entry)
        # Written before the caller is told, not after: the record is the
        # durable half and the callback is only a notification. A watcher that
        # raises — which is how the browser interface asks a run to stop — must
        # not cost the record of the stage that had already finished.
        self.write()
        if self.on_progress is not None:
            self.on_progress(entry)
        return entry

    def write(self) -> None:
        if self.target is None:
            return
        _io.makedirs(self.target.parent)
        self.target.write_text(
            json.dumps(self.payload, indent=2, default=str), encoding="utf-8")


# ------------------------------------------------------- what a person said
def _drawn_on(kind: str, source, output_dir=None) -> Path | None:
    """The picture a drawing for this kind of field was made on.

    Three answers, one per row of the ``corrigible`` table that names a place:

    ==================  ====================================================
    ``source``          the recording itself, which is what the broad crop
                        reads
    ``outline_input``   the plane the outline reads, named by the previous
                        run's own report
    ``oriented_source`` the canvas the square crop is cut from, likewise
    ==================  ====================================================

    The last two are read out of the report rather than rebuilt from suffixes.
    The report already records the exact path it wrote, and reconstructing a
    name from four constants is how a rename turns into a correction quietly
    checked against the wrong file.

    ``None`` when there is nothing to compare against — the first run over a
    folder, where no preview exists yet. That is not an error: a correction
    recorded then simply carries no frame, and says so.
    """
    if kind == "source":
        return Path(source)
    if kind not in {"outline_input", "oriented_source"}:
        return None
    report = _batch.expected_report_path(source, output_dir=output_dir)
    if not _io.isfile(report):
        return None
    try:
        payload = json.loads(Path(report).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    named = payload.get(kind)
    return Path(named) if named and _io.isfile(named) else None


def _output_dir_for(stage: str, source, output_root,
                    known: Mapping[str, str] | None = None) -> Path | None:
    """Where this stage writes for one recording, or ``None`` for beside it.

    One line, kept as a function because it is the answer a resume has to give
    the same way twice: predicted before the stage runs and used again when it
    does. :mod:`auto_organotypic.layout` holds the shape; this holds the fact that
    every part of the pipeline asks it through here.
    """
    return layout.folder(output_root, stage, source, known)


class _Corrections:
    """What people have said about this stage's recordings, applied or not.

    One object per stage per run. It reads the corrections, refuses any whose
    drawing cannot have been made on the frame it is about to hit, hands back a
    mapping in the shape the stage already takes, and keeps the records so the
    run record can name them.

    Refusing happens **here**, before the stage starts. A run that discovers
    after forty minutes of registration that a region was drawn on the wrong
    picture has wasted the forty minutes, and the check costs one header read.
    """

    def __init__(self, stage: str, recordings, options: Mapping[str, Any],
                 output_root=None):
        self.stage = _stage(stage)
        self.ignored = bool(options.get("ignore_corrections"))
        self.found = corrections.records_for_stage(stage, recordings)
        self.shared = self._shared()
        self.names = layout.names(recordings)
        if not self.ignored:
            self._check(output_root)

    def _check(self, output_root) -> None:
        for path, said in self.found.items():
            for one in said:
                group = self.stage.group_for(one.field)
                if group is None or not group.picture:
                    continue
                picture = _drawn_on(
                    group.picture, path,
                    _output_dir_for(self.stage.name, path, output_root,
                                    self.names))
                if picture is None:
                    continue        # nothing written yet to compare against
                corrections.check_frame(one, picture, what=Path(path).name)

    def _shared(self) -> dict[str, list[str]]:
        """Corrected recordings whose files hold identical bytes.

        A decision is keyed on the source's *contents*, deliberately: that is
        what lets a correction survive a Dropbox move, a rename and a copy onto
        another machine. The other side of it is that two recordings holding
        identical bytes are one recording as far as the store is concerned, so
        a region drawn for one is found for both — and no frame check can
        object, because the frames really are the same picture.

        Two wells rarely produce identical files. Two wells that were never
        seeded, and came back as the same flat frame, can. So this is not
        prevented — preventing it would mean hashing the path, which is the one
        thing the store refuses to do — it is *named*, in the run record, where
        somebody can see that one drawing covered two wells.
        """
        by_identity: dict[str, list[str]] = {}
        for path in self.found:
            try:
                identity = store.fingerprint(path).identity
            except OSError:
                continue
            by_identity.setdefault(identity, []).append(str(path))
        return {identity: sorted(paths)
                for identity, paths in by_identity.items() if len(paths) > 1}

    def per_source(self) -> dict[str, dict[str, Any]]:
        """``{path: {field: value}}`` — ``per_source``, or ``regions``."""
        if self.ignored:
            return {}
        return {path: {one.field: one.value for one in said}
                for path, said in self.found.items()}

    def report(self) -> dict[str, Any]:
        """One line of the run record. Never a bare count.

        A stale correction is the named risk of keeping them at all — a box
        drawn in August still winning in October — and this is where somebody
        notices. ``oldest`` is the date to look at; the list is what to look
        at it against.
        """
        said = [one for said in self.found.values() for one in said]
        if not said:
            return {}
        rows = [{"recording": Path(path).name, "field": one.field,
                 "made": one.made, "note": one.note}
                for path, group in sorted(self.found.items())
                for one in group]
        out = {"corrected": 0 if self.ignored else len(self.found),
               "corrections_applied": not self.ignored,
               "oldest_correction": min((one.made for one in said
                                         if one.made), default=""),
               "corrections": rows}
        if self.shared:
            out["corrections_shared_by_identical_recordings"] = [
                [Path(one).name for one in paths]
                for paths in self.shared.values()]
        return out


def _record_given(stage: str, field: str, found: Mapping[str, Any],
                  options: Mapping[str, Any], output_root=None, *,
                  flag: str, known: Mapping[str, str] | None = None) -> None:
    """Keep a drawing passed on this run, so the next run need not be told.

    This is what makes ``auto-organotypic pipeline <folder>``, run again with no
    flags at all, the rerun interface. It is also the thing that surprises
    somebody who passed a folder of regions to try something — which is why the
    run record names every applied correction and ``auto-organotypic correct
    --forget`` exists.
    """
    if options.get("ignore_corrections"):
        return
    group = _stage(stage).group_for(field)
    for path, extra in found.items():
        if field not in extra:
            continue
        picture = None if group is None or not group.picture else _drawn_on(
            group.picture, path,
            _output_dir_for(stage, path, output_root, known))
        corrections.record(stage, path, field, extra[field],
                           drawn_on=picture,
                           which=(group.frame if group else ""),
                           note=f"passed as {flag}")


# -------------------------------------------------------------- the grid
def grid_of(record: Mapping[str, Any]) -> staleness.Grid:
    """The grid a ``what_would_run`` run recorded, ready to print.

    Rebuilt from the record rather than recomputed, so that what the terminal
    shows and what the record holds are one set of facts. Two computations of a
    grid would eventually disagree, and the one somebody happened to read would
    be the wrong one.
    """
    payload = dict(record.get("grid") or {})
    return staleness.Grid(
        [staleness.Cell(str(one.get("stage", "")),
                        str(one.get("recording", "")),
                        str(one.get("verdict", "")),
                        str(one.get("why", "")))
         for one in payload.get("cells", [])],
        stages=[str(one) for one in payload.get("stages", [])],
        recordings=[str(one) for one in payload.get("recordings", [])])


def _grid(state: dict[str, Any], options: Mapping[str, Any],
          chosen: Sequence[str]) -> staleness.Grid:
    """What this run would do, worked out before it does any of it.

    Walks the stages in order, threading forward what each one *would* hand the
    next. That threading is the whole trick, and it is also what makes the
    answer cheap: once a cell is stale, every cell below it in the same column
    is stale too — the stage below will be handed different pixels — so nothing
    downstream of a rerun has to be resolved at all.

    It is also why redrawing an outline costs two squares and redrawing a broad
    crop costs four, including the multi-hour one. That asymmetry is the reason
    this exists: "something is stale" is not worth printing, and "two of three
    hundred and eighty-four squares, and none of them is registration" is.

    Nothing here opens a stack. Manifests, markers and the store's index only —
    a plate-wide answer that hydrated ninety-six online-only placeholders to
    say "nothing needs doing" would cost more than not asking.
    """
    recordings = list(state["recordings"])
    stages = [name for name in chosen if name not in ("acquire", "index")]
    # ``(column name, the recording that column is about)``. A list of pairs
    # and not two lists, because the split stage turns one of these into
    # several and a name that lost track of its recording would put a
    # segment's verdict in the parent's column.
    rows: list[tuple[str, Recording]] = [
        (sources.composed_name(one) or one.path.stem, one)
        for one in recordings]
    grid = staleness.Grid(stages=stages,
                          recordings=[name for name, _one in rows])
    root = state.get("output_root")
    # ``--only`` is not applied here. ``run_pipeline`` narrows the recordings
    # before building this, so the columns are already the ones somebody asked
    # for — and filtering twice, once by well name and once by whatever the
    # grid calls a column, is how the two disagree.
    forced = bool(options.get("force"))

    # Which column is already doomed, by name. A stage that will rerun makes
    # every later stage on that recording rerun as well.
    doomed: set[str] = set()
    for position, stage in enumerate(stages):
        carried = [one for _name, one in rows]
        verdicts = _stage_verdicts(stage, carried, options, root)
        # Keyed on the paths this stage is being handed, so it has to be rebuilt
        # here rather than once above: ``carried`` moves down the chain.
        folders = layout.names(carried)
        # Worked out once for the whole stage rather than once per row: it
        # reads the decision store, and ninety-six reads of it to answer one
        # question is what this function exists not to do.
        cutting = (_split_settings(carried, options, root)[:2]
                   if stage == "split" else ((), {}))
        after: list[tuple[str, Recording]] = []
        # Over ``carried``, never over the raw recordings. Each stage answers
        # about the file it would actually be handed — the broad crop's output
        # for the register stage, the registered stack for the outline — so a
        # verdict looked up by the raw path would miss every time and read as
        # ``unknown``, which then cascades down the whole column as though the
        # plate were stale. It did, once.
        for name, one in rows:
            settled = False
            if name in doomed:
                grid.add(staleness.Cell(stage, name, "upstream",
                                        "an earlier stage will rerun, so this "
                                        "one is handed different pixels"))
            elif forced:
                grid.add(staleness.Cell(stage, name, "forced",
                                        "asked for by name with --force"))
                doomed.add(name)
            else:
                verdict, why = verdicts.get(str(one.path), ("unknown", ""))
                grid.add(staleness.Cell(stage, name, verdict,
                                        why or staleness.VERDICTS.get(
                                            verdict,
                                            staleness.GRID_VERDICTS.get(
                                                verdict, ""))))
                # Only a settled stage can say what it would hand on, and only
                # a settled one needs to: a column that is already rerunning is
                # answered above without anybody having to guess its inputs.
                settled = verdict in ("current", "adopted")
                if not settled:
                    doomed.add(name)
            if stage == "split":
                after.extend(_segment_rows(grid, stages, position, name, one,
                                           cutting, root, folders, doomed))
                continue
            after.append((name, _handed_on(stage, one, root, folders)
                          if settled else one))
        rows = after
    return grid


def _segment_rows(grid: staleness.Grid, stages: Sequence[str], position: int,
                  name: str, one: Recording, cutting, root,
                  folders: Mapping[str, str],
                  doomed: set[str]) -> list[tuple[str, Recording]]:
    """One column per segment, in place of the recording they are cut from.

    A split makes more analysable units than there are recordings, and a grid
    that did not show them would say a run will do less than it will — which is
    the one thing this grid exists to get right.

    The parent's column stops here and the segments' start here, both said with
    ``not_run``, which already means "this stage is not in the run" and reads
    correctly for both halves of that. No new verdict: ``GRID_VERDICTS`` is a
    table people read, and the column label already says which is which.

    Columns are added whether or not the split is settled. A segment nothing
    has been written for is exactly what somebody asking what would run wants
    to see, and it says ``NEW`` at the outline rather than being absent.
    """
    from . import split as _split_stage

    segments, per_source = cutting
    made = _split_stage._settle(one, segments=segments, per_source=per_source,
                                root=layout.stage_root(root, "split"),
                                known=folders)
    if not made:
        return [(name, one)]

    for later in stages[position + 1:]:
        grid.add(staleness.Cell(later, name, "not_run",
                                "this recording is split; its segments are the "
                                "columns beside it"))
    rows: list[tuple[str, Recording]] = []
    at = (grid.recordings.index(name) + 1 if name in grid.recordings
          else len(grid.recordings))
    for offset, (segment, composed, folder) in enumerate(made):
        grid.recordings.insert(at + offset, composed)
        for earlier in stages[:position + 1]:
            grid.add(staleness.Cell(earlier, composed, "not_run",
                                    "the recording this is cut from ran this "
                                    "stage"))
        if name in doomed:
            doomed.add(composed)
        written = _split_stage.report_for(composed, folder) or {}
        rows.append((composed, replace(
            one, path=Path(written.get("output") or folder / f"{composed}.tif"),
            frames=int(written.get("frames") or 0) or one.frames,
            segment=segment.name)))
    return rows


def _stage_verdicts(stage: str, recordings: Sequence[Recording],
                    options: Mapping[str, Any],
                    root) -> dict[str, tuple[str, str]]:
    """Each stage answers for itself, in its own terms.

    Never a copy of a stage's settings resolution living here. A second opinion
    about which channel the broad crop would read, or which plane the outline
    would take, is a second implementation that drifts — and the way it fails is
    a grid that says a plate is finished when it is not.

    A stage this package does not own answers ``unknown``. That is the honest
    verdict, and it is better than a confident guess about somebody else's
    cache. Only ``acquire`` is in that position now; the trace stage came here
    on 2026-08-26 and answers for itself like the rest.
    """
    if stage == "broad_crop":
        from . import broad_crop as _broad_crop

        regions, _said, _drawn = _broad_crop_settings(recordings, options, root)
        return _broad_crop.pending(
            recordings,
            output_root=layout.stage_root(root, "broad_crop"),
            mode=options.get("broad_crop_mode", "standard"),
            channel=options.get("channel"),
            region=options.get("broad_crop_region"),
            regions=regions,
            rois_if_missing=str(options.get("rois_if_missing", "auto")),
            **dict(options.get("broad_crop_options") or {}))
    if stage in ("trim", "trim_before_crop"):
        from . import trim as _trim_stage

        window, per_source, _said = _trim_settings(recordings, options, root,
                                                   stage)
        return _trim_stage.pending(
            recordings, window=window, per_source=per_source,
            output_root=layout.stage_root(root, stage),
            known=layout.names(recordings), stage=stage)
    if stage == "register":
        return _register_verdicts(recordings, options, root)
    if stage == "split":
        from . import split as _split_stage

        wanted, per_source, _said = _split_settings(recordings, options, root)
        return _split_stage.pending(
            recordings, segments=wanted, per_source=per_source,
            output_root=layout.stage_root(root, "split"),
            known=layout.names(recordings))
    if stage == "outline":
        return _outline_verdicts(recordings, options, root)
    if stage == "review":
        # Always redrawn, and it says so rather than answering ``unknown`` —
        # which means "another package owns this" and would be a lie about a
        # stage in this one. A review is a picture of the run that has just
        # finished, so last week's is never the answer to this week's question.
        return {str(one.path):
                ("stale", "a review describes the run that has just "
                          "finished, so it is drawn again every time")
                for one in recordings}
    return {str(one.path): ("unknown", staleness.GRID_VERDICTS["unknown"])
            for one in recordings}


def _register_verdicts(recordings, options, root) -> dict[str, tuple[str, str]]:
    """Whether the multi-hour step would run again, from the store's index.

    Registration already keys its shifts through the store, so this asks the
    store rather than keeping a second record of the same fact — the house rule
    that nothing may copy what decides whether two artefacts are the same.

    Both halves have to be true: the shifts have to be stored *and* the
    registered stack has to still be on disk. A hit on the table with no stack
    beside it means somebody cleared the exports and the pixels have to be
    written again, and reporting that as current is how a later stage gets
    pointed at a file that is not there.
    """
    try:
        from . import registration as _registration
    except ImportError as exc:              # pragma: no cover - needs the extra
        return {str(one.path): ("unknown", f"registration is not installed: {exc}")
                for one in recordings}

    wanted = options.get("registration_channel")
    extra = dict(options.get("register_options") or {})
    named = layout.names(recordings)
    out: dict[str, tuple[str, str]] = {}
    for one in recordings:
        channel = wanted if wanted is not None else _structural_channel(one)
        params = {
            "registration_channel": int(
                channel if channel is not None
                else _registration.DEFAULT_REGISTRATION_CHANNEL),
            "downsample": int(extra.get(
                "downsample", _registration.DEFAULT_DOWNSAMPLE)),
            "margin_px": int(extra.get(
                "margin_px", _registration.DEFAULT_MARGIN_PX)),
            "content_crop": bool(extra.get(
                "content_crop", _registration.DEFAULT_CONTENT_CROP))}
        folder = _output_dir_for("register", one.path, root, named)
        stack = _registration.registered_stack_path(
            one.path, _registration.output_dir_for(one.path, folder),
            extra.get("output_name"))
        if not _io.isfile(stack):
            out[str(one.path)] = ("never", "no registered stack has been "
                                           "written for this recording")
            continue
        try:
            hit = store.get(_registration.REGISTRATION_STAGE, one.path, params,
                            method_version=_registration.METHOD_VERSIONS[
                                "reference"])
        except OSError as exc:
            out[str(one.path)] = ("stale", f"the recording cannot be read: {exc}")
            continue
        out[str(one.path)] = (("current", "the stored shifts match, and the "
                                          "registered stack is on disk")
                              if hit is not None else
                              ("stale", "no stored shifts match these settings"))
    return out


def _outline_verdicts(recordings, options, root) -> dict[str, tuple[str, str]]:
    """Whether the accepted outline would run again, asked of its own resume.

    The same :class:`~auto_organotypic.staleness.Resume` the stage itself uses, built
    from the same plan — so what the grid predicts and what the run does cannot
    come apart.
    """
    try:
        plan = _outline_settings(recordings, options, root)
    except corrections.WrongFrame:
        # Not swallowed into "stale". Somebody asking what would run is owed
        # the news that a recorded drawing cannot be applied at all, and
        # burying that as a square marked STALE would let them start the run
        # and hit it anyway.
        raise
    except Exception as exc:            # a plan that cannot be made yet
        return {str(one.path): (
            "stale", f"the outline plan cannot be made yet: {exc}")
            for one in recordings}
    plan_sources, per_source = plan["plan_sources"], plan["per_source"]
    resume = staleness.resume_for(
        recordings, "outline", plan["shared"],
        method_version=_batch.METHOD_VERSION, per_source=per_source,
        unmarked=str(options.get("unmarked", "adopt")))
    out: dict[str, tuple[str, str]] = {}
    for one in recordings:
        if one.path not in plan_sources:
            out[str(one.path)] = ("not_run", "not in the outline plan: no "
                                             "matching channel, or incomplete")
            continue
        report = _batch.expected_report_path(
            one.path,
            output_dir=_output_dir_for("outline", one.path, root,
                                       plan["names"]))
        if not _io.isfile(report):
            out[str(one.path)] = ("never", "nothing has been outlined here yet")
            continue
        # Asked without letting it adopt: a grid must not change what is on
        # disk. Adoption is a decision the *run* makes, and a question about
        # what would happen is not permission to make it.
        key = resume.key_for(one.path)
        current, why = staleness.is_current(report.parent, key,
                                            name=one.path.stem)
        out[str(one.path)] = ("current", why) if current else ("stale", why)
    return out


def _handed_on(stage: str, recording: Recording, root,
               known: Mapping[str, str] | None = None) -> Recording:
    """What a settled stage would give the next one, without running it.

    Read off what that stage already wrote — the crop's report, the registered
    stack's predicted name — because the only way a stage's output path is
    knowable in advance is that the stage already produced it, and a cell is
    only asked this when it is current.
    """
    if stage == "broad_crop":
        from . import broad_crop as _broad_crop

        report = (_broad_crop.output_folder(
                      recording.path, layout.stage_root(root, "broad_crop")) /
                  f"{recording.path.stem}{_broad_crop.REPORT_SUFFIX}")
        if not _io.isfile(report):
            return recording
        try:
            plan = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return recording
        output = plan.get("output")
        return (replace(recording, path=Path(output))
                if plan.get("enabled") and output and _io.isfile(output)
                else recording)
    if stage in ("trim", "trim_before_crop"):
        from . import trim as _trim_stage

        folder = _trim_stage.output_folder(
            recording.path, layout.stage_root(root, stage), known)
        report = folder / f"{recording.path.stem}{_trim_stage.REPORT_SUFFIX}"
        if not _io.isfile(report):
            # No report is the ordinary case here, not a missing one: it is
            # what a run with no window leaves behind, and that recording is
            # handed on exactly as it arrived.
            return recording
        try:
            plan = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return recording
        output = plan.get("output")
        return (replace(recording, path=Path(output),
                        frames=int(plan.get("frames") or 0) or recording.frames)
                if output and _io.isfile(output) else recording)
    if stage == "register":
        from . import registration as _registration

        folder = _output_dir_for("register", recording.path, root, known)
        stack = _registration.registered_stack_path(
            recording.path,
            _registration.output_dir_for(recording.path, folder))
        return (replace(recording, path=Path(stack), registered=True,
                        valid_mask=None)
                if _io.isfile(stack) else recording)
    return recording


# ----------------------------------------------------------- the stages
def _acquire(state: dict[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
    """Pull an experiment onto disk. The only stage that talks to hardware.

    Deliberately thin: it opens the saved login, asks for a time-and-channel
    stack, and returns. Which wells, which channels and where to start from are
    the instrument package's questions and are passed through untouched.
    """
    instrument = str(options.get("instrument") or "").lower()
    if instrument not in INSTRUMENTS:
        raise ValueError(
            f"instrument must be one of {', '.join(INSTRUMENTS)}; "
            f"got {instrument!r}")
    experiment = options.get("experiment")
    if experiment is None:
        raise ValueError("acquire needs an experiment: a vessel id for the "
                         "Incucyte, an experiment name for the LV200")
    folder = Path(state["folder"])
    extra = dict(options.get("acquire_options") or {})

    client_class = resolve(INSTRUMENTS[instrument])
    if instrument == "incucyte":
        client = client_class.from_saved()
        # A time-and-channel stack, never ``separate``: one YX image per well
        # per channel per timepoint is thousands of files the outline would
        # treat as thousands of recordings. See ``sources.refuse_per_image``.
        extra.setdefault("layout", "time_channel_stack")
        result = client.fetch(vessel=experiment, output=str(folder), **extra)
        written = len(getattr(result, "files", ()) or ())
    else:
        # ``LV200Client.pull`` landed on 2026-08-24 - it is ``fetch`` under the
        # verb this pipeline uses, and returns a PullResult. The check stays
        # for an older pylv200 that is installed but predates it, where the
        # pull still lived in that package's command-line layer.
        if not hasattr(client_class, "pull"):
            raise StagePending(
                "the installed pylv200 has no importable pull - LV200Client"
                ".plan() stops short of writing, and the pull lives in its "
                "cli. Upgrade it, or run 'pylv200 pull' and point this "
                "pipeline at the folder.")
        client = client_class(options.get("source") or extra.pop("source", None))
        # ``out`` and not ``output``: both name the same folder and ``out``
        # wins, so passing only the recipe field would be quietly overridden by
        # a saved preset. The Incucyte branch above has no such pair.
        result = client.pull(experiment, out=folder, **extra)
        written = len(getattr(result, "files", ()) or ())
    return {"instrument": instrument, "experiment": str(experiment),
            "files": written, "folder": str(folder)}


def _index(state: dict[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
    """Read the pull's manifest, drop the wells somebody rejected, and refuse a
    folder of single planes.

    Rejecting is the correction that saves the most compute of anything in this
    pipeline: a dead well dropped here costs nothing at the broad crop, nothing
    at registration, nothing at the outline and nothing at the trace. It is
    also the one most often done by moving a file out of a folder, which works
    and leaves nobody any way of knowing why — including the person who did it,
    three months later.
    """
    read = resolve(_stage("index").target)
    recordings = read(state["folder"], instrument=options.get("manifest_kind"))
    if not options.get("allow_per_image"):
        sources.refuse_per_image(recordings)

    said = _Corrections("index", recordings, options, state.get("output_root"))
    rejected = {path for path, extra in said.per_source().items()
                if extra.get("exclude")}
    kept = [one for one in recordings if str(one.path) not in rejected]
    state["recordings"] = kept

    # Rejected, not missing. A recording that vanishes between two runs with no
    # line saying why is exactly what this record exists to stop, so each one
    # is named with the reason somebody gave and with whether outputs from
    # before it was rejected are still sitting on disk. Nothing in this package
    # deletes results, so they are left — but a folder holding outputs for a
    # recording that is no longer in the run has to say so, or the next person
    # reads them as current.
    dropped = []
    for one in recordings:
        if str(one.path) not in rejected:
            continue
        [why] = [c for c in corrections.all_for(one.path)
                 if c.stage == "index" and c.field == "exclude"] or [None]
        folder = _output_dir_for("outline", one.path, state.get("output_root"),
                                 layout.names(recordings))
        report = _batch.expected_report_path(one.path, output_dir=folder)
        dropped.append({"recording": str(one.field) or one.path.stem,
                        "path": str(one.path),
                        "why": (why.note if why and why.note else "not stated"),
                        "rejected_on": (why.made if why else ""),
                        "outputs_still_on_disk": bool(_io.isfile(report))})

    return {"recordings": len(kept),
            "fields": sorted({str(one.field) for one in kept}),
            "incomplete": sum(1 for one in kept if one.complete is False),
            "rejected": dropped,
            "manifest": str(sources.find_manifest(state["folder"])[0])}


def _broad_crop(state: dict[str, Any],
                options: Mapping[str, Any]) -> dict[str, Any]:
    """A coarse box around the tissue, before registration sees a full frame.

    Not the accepted outline and no substitute for it: a rectangle, so that
    registration works on a small frame instead of most of an instrument
    sensor. It is opt-in because the failure it can have is asymmetric — a box
    that is too tight does not fail visibly, it shifts a threshold in the
    method that runs three stages later.

    That is why the counts below are worth a line of the run record rather than
    only a total. A recording this stage cannot read confidently is one it does
    not crop, which costs the compute the crop would have saved and cannot cost
    a pixel of tissue; ``kept_whole`` is that number, and a run where it is
    every recording is telling you the detector could not read the plate.
    """
    crop = resolve(_stage("broad_crop").target)
    before = list(state["recordings"])
    seen: list[Mapping[str, Any]] = []
    # The hand-drawn half. ``broad_crop_rois`` and not ``rois`` or
    # ``crop_rois``: this pipeline crops twice, here on the raw recording and
    # again around the outline on the oriented canvas, and an ImageJ region
    # does not record which frame it was drawn on. One name for both would be
    # one drawing applied to two coordinate systems, of which at most one is
    # right -- so every name here says its stage.
    regions, said, drawn_now = _broad_crop_settings(
        before, options, state.get("output_root"), record=True)
    # ``broad_crop_region`` is the same thing said once for the whole run: four
    # numbers for a plate imaged the same way throughout, where drawing
    # ninety-six identical boxes would be ninety-six chances to draw one wrong.
    # A per-recording drawing wins over it, recording by recording.
    after = list(crop(before,
                      output_root=layout.stage_root(
                          state.get("output_root"), "broad_crop"),
                      # ``--force`` in this stage's own word for it. Already
                      # scoped: ``--only`` narrowed the recordings before this
                      # stage was reached.
                      overwrite=bool(options.get("force")),
                      mode=options.get("broad_crop_mode", "standard"),
                      channel=options.get("channel"),
                      region=options.get("broad_crop_region"),
                      regions=regions,
                      rois_if_missing=str(
                          options.get("rois_if_missing", "auto")),
                      on_progress=seen.append,
                      **dict(options.get("broad_crop_options") or {})))
    state["recordings"] = after
    cropped = [row for row in seen if row.get("cropped")]
    kept = [row for row in seen if row.get("ok") and not row.get("cropped")]
    return {"recordings": len(after),
            "cropped": len(cropped),
            "kept_whole": len(kept),
            # Measured on an earlier run and still current. Counted apart from
            # the two above, which say what is on disk: a plate can be fully
            # cropped and fully skipped at once, and the difference between
            # those two sentences is the hours this stage did not spend.
            "skipped": sum(1 for row in seen if row.get("skipped")),
            "hand_drawn": sum(1 for row in seen
                              if str(row.get("region_source", "")
                                     ).startswith("hand")),
            "given_region": sum(1 for row in seen
                                if str(row.get("region_source", "")
                                       ).startswith("given")),
            "failed": sum(1 for row in seen if not row.get("ok")),
            # One number, not ninety-six: the share of the frame a typical crop
            # kept. A plate that comes back near 1.0 across the board is telling
            # you the stage is not earning the pass it costs.
            "median_area_fraction": _median(
                float((row.get("plan") or {}).get("area_fraction", 1.0))
                for row in cropped),
            # Why the rest were not cropped, deduplicated. Each of these is a
            # sentence from the detector, and a run where they are all the same
            # sentence is one worth reading.
            "kept_whole_because": sorted({
                str((row.get("plan") or {}).get("reason") or "not stated")
                for row in kept}),
            **said.report(),
            }


def _broad_crop_settings(recordings: Sequence[Recording],
                         options: Mapping[str, Any], root, *,
                         record: bool = False):
    """The boxes this stage would use, worked out once for two callers.

    The stage itself and the grid that says what the stage would do. They must
    not each work this out: a grid with its own idea of which box wins is a
    second implementation, and the way it fails is a plate that says it is
    finished and then re-crops half of it.

    Precedence, top wins: a drawing passed on **this** run, then one recorded
    on an earlier run, then a box given for the whole run, then the detector.
    A drawing passed now beats a recorded one because the person looking at the
    picture today is the most recent answer — so recorded boxes go in first and
    are overwritten by the ones passed.

    ``record`` is the only difference between the two callers. Passing a
    drawing *keeps* it, so the next run needs no flags; asking what would run
    must change nothing at all.
    """
    group = _stage("broad_crop").corrigible[0]
    drawn = None
    if options.get(group.flag):
        found = group.read(recordings, options[group.flag],
                           if_missing=str(options.get("rois_if_missing",
                                                      "auto")))
        drawn = {path: extra["region"] for path, extra in found.items()}
        if record:
            _record_given("broad_crop", "region", found, options, root,
                          flag=f"--{group.flag.replace('_', '-')}",
                          known=layout.names(recordings))

    said = _Corrections("broad_crop", recordings, options, root)
    recorded = said.per_source()
    regions = drawn
    if recorded:
        merged = {path: extra["region"] for path, extra in recorded.items()
                  if "region" in extra}
        merged.update(drawn or {})
        regions = merged
    return regions, said, len(drawn or {})


def _trim_settings(recordings: Sequence[Recording], options: Mapping[str, Any],
                   root, stage: str):
    """The window each recording would be trimmed to, for two callers.

    The stage itself and the grid that says what the stage would do. They must
    not each work this out: a grid with its own idea of which window wins is a
    second implementation, and the way it fails is a plate that says it is
    finished and then re-trims.

    ``trim_before_crop`` is a boolean, not a second window. There is one window
    per run and it goes on one of the two stages, so the other is a no-op —
    which is also what both are when nobody asked for a trim at all. A window
    *recorded* against a stage by ``auto-organotypic correct`` names that stage
    outright and is applied whichever way the flag points.
    """
    early = bool(options.get("trim_before_crop"))
    wanted = options.get("trim")
    mine = wanted if (stage == "trim_before_crop") == early else None
    said = _Corrections(stage, recordings, options, root)
    return mine, said.per_source(), said


def _run_trim(state: dict[str, Any], options: Mapping[str, Any],
              stage: str) -> dict[str, Any]:
    """Both trim stages. See :mod:`auto_organotypic.trim` for why there are two.

    With no window this hands ``state["recordings"]`` back exactly as it found
    them and writes nothing — no stack, no report, no marker. A run that never
    asked for a trim has to be indistinguishable from a run made before this
    stage existed, stage keys included, and this is where that is kept true.
    """
    from . import trim as _trim_stage

    run = resolve(_stage(stage).target)
    before = list(state["recordings"])
    window, per_source, said = _trim_settings(
        before, options, state.get("output_root"), stage)
    seen: list[Mapping[str, Any]] = []
    after = list(run(before,
                     window=window, per_source=per_source,
                     output_root=layout.stage_root(
                         state.get("output_root"), stage),
                     known=layout.names(before),
                     stage=stage,
                     overwrite=bool(options.get("force")),
                     on_progress=seen.append))
    state["recordings"] = after
    trimmed = [row for row in seen if row.get("trimmed")]
    return {"recordings": len(after),
            "trimmed": len(trimmed),
            "skipped": sum(1 for row in seen if row.get("skipped")),
            "failed": sum(1 for row in seen if not row.get("ok")),
            "window": str(_trim_stage._as_window(window) or ""),
            # Per recording, because ``--only B2 --value '"0..120h"'`` is the
            # whole point of the correction and a plate-wide number would hide
            # the one well that differs.
            "windows": sorted({str(row.get("window") or "") for row in trimmed}),
            "frames_kept": sorted({int(row["frames"]) for row in trimmed
                                   if row.get("frames")}),
            **said.report()}


def _trim_before_crop(state: dict[str, Any],
                      options: Mapping[str, Any]) -> dict[str, Any]:
    return _run_trim(state, options, "trim_before_crop")


def _trim(state: dict[str, Any],
          options: Mapping[str, Any]) -> dict[str, Any]:
    return _run_trim(state, options, "trim")


def _split_settings(recordings: Sequence[Recording],
                    options: Mapping[str, Any], root):
    """Where each recording divides, worked out once for two callers.

    The stage itself and the grid that says what the stage would do, for the
    reason ``_broad_crop_settings`` gives: a grid with its own idea of which
    segments win is a second implementation, and the way it fails is a plate
    that says it is finished and then re-splits.

    ``--split`` and ``--split-at`` are two spellings of one decision and are
    refused together by :func:`auto_organotypic.windows.segments`, which is also
    where they are sorted and their names checked. Parsed here rather than in
    the stage so that a typo costs a message and not the registration that
    would have run before the stage was reached.
    """
    wanted = windows.segments(options.get("split") or (),
                              options.get("split_at") or ())
    said = _Corrections("split", recordings, options, root)
    return wanted, said.per_source(), said


def _split(state: dict[str, Any],
           options: Mapping[str, Any]) -> dict[str, Any]:
    """One registered recording becomes several, and the chain carries on.

    The only stage in the package that hands the next one more recordings than
    it was given, and it needs no new contract to do it: ``_register`` already
    hands on *different* recordings than it got, and nothing downstream asks
    how many there were to begin with.

    A recording nobody split passes through unchanged in the same list as the
    split ones. Mixed plates are the normal case — one well got a drug and
    the rest did not — and with no segments at all this writes nothing and
    changes nothing.
    """
    from . import split as _split_stage

    run = resolve(_stage("split").target)
    before = list(state["recordings"])
    root = state.get("output_root")
    wanted, per_source, said = _split_settings(before, options, root)
    seen: list[Mapping[str, Any]] = []
    after = list(run(before, segments=wanted, per_source=per_source,
                     output_root=layout.stage_root(root, "split"),
                     known=layout.names(before),
                     overwrite=bool(options.get("force")),
                     on_progress=seen.append))
    state["recordings"] = after
    made = [row for row in seen if row.get("split")]
    detail: dict[str, Any] = {
        "recordings": len(after),
        "split": len({row["parent"] for row in made}),
        "segments": len(made),
        "skipped": sum(1 for row in seen if row.get("skipped")),
        "failed": sum(1 for row in seen if not row.get("ok")),
        # Which segment came from which parent, by name. The parent keeps its
        # registered stack and gains no outline or trace of its own, so ``A1``
        # appears under ``registration/`` and not under ``traces/`` -- which is
        # right, and looks wrong to anybody scanning folders.
        "made": [{"segment": row["name"], "parent": Path(row["parent"]).name,
                  "window": row.get("window", ""), "frames": row.get("frames")}
                 for row in made],
        # N segments of a registered stack is N times its size. Said here so it
        # is visible before somebody fills a Dropbox rather than afterwards.
        "bytes_written": sum(int(row.get("bytes") or 0) for row in made
                             if not row.get("skipped")),
        **said.report()}
    left = _split_stage.orphans(root, before, after)
    if left:
        # Never deleted. A folder whose name stopped matching a flag still
        # holds somebody's results, and the next person to open it reads it as
        # current unless the run says otherwise.
        detail["orphaned_segment_folders"] = left
    return detail


def _median(values: Any) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 4)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, 4)


def _register(state: dict[str, Any],
              options: Mapping[str, Any]) -> dict[str, Any]:
    """Align every frame on a structural channel, then crop to their overlap.

    Two facts about the result matter to everything downstream. The accepted
    outline reads a *registered* recording and ``scn_time="mean"`` averages the
    whole time axis, so on a drifting stack that mean is smeared across the
    drift — this is why registration comes first. And the export is cropped to
    the box every frame still covers, so the registered stack's whole frame is
    valid by construction: that is what lets the outline stage pass
    ``allow_full_frame_valid`` as a stated fact rather than a shrug.

    ``registration_channel`` is one-based and carries the engine's caution:
    estimating on the signal channel makes the cells register to themselves and
    hides the motion being measured. Unset, it picks a phase or brightfield
    channel when the manifest names one, and otherwise leaves the choice to
    the estimator's default rather than guessing.
    """
    run = resolve(_stage("register").target)
    root = state.get("output_root")
    wanted = options.get("registration_channel")
    said = _Corrections("register", state["recordings"], options, root)
    recorded = said.per_source()
    _refuse_unusable(run, recorded)
    named = layout.names(state["recordings"])
    done: list[Recording] = []
    failures: list[dict[str, Any]] = []
    for one in state["recordings"]:
        settings = dict(options.get("register_options") or {})
        # Before the defaults below, so a recorded answer wins over the
        # structural-channel guess and loses to nothing but an explicit
        # ``register_options``, which is the caller saying it now.
        settings = {**recorded.get(str(one.path), {}), **settings}
        if options.get("force"):
            # The store's own word for it. This is the multi-hour stage, so
            # it only ever happens because somebody named it.
            settings["reuse"] = False
        channel = wanted if wanted is not None else _structural_channel(one)
        if channel is not None:
            settings.setdefault("registration_channel", int(channel))
        if root is not None:
            settings.setdefault(
                "output_dir",
                str(_output_dir_for("register", one.path, root, named)))
        try:
            result = run(str(one.path), **settings)
        except Exception as exc:            # one bad stack is one recording
            failures.append({"path": str(one.path), "error": str(exc)})
            continue
        stack = result.get("registered_stack") if isinstance(result, Mapping) \
            else None
        if not stack:
            failures.append({"path": str(one.path),
                             "error": "registration wrote no stack"})
            continue
        done.append(replace(
            one, path=Path(stack), registered=True, valid_mask=None,
            frames=result.get("frames", one.frames),
            valid_mask_policy=(
                "the whole frame, which is the registration overlap: the "
                f"export is cropped to {result.get('crop_xyxy')}")))
    state["recordings"] = done
    return {"registered": len(done), "failed": len(failures),
            "failures": failures, **said.report()}


def _refuse_unusable(run: Callable[..., Any],
                     recorded: Mapping[str, Mapping[str, Any]]) -> None:
    """Stop before the hours, not after them, on an answer this call cannot take.

    ``registration.estimate_and_apply`` measures the drift itself and has no
    argument for a table somebody measured elsewhere; ``auto_organotypic chain`` does,
    and checks the table's length and its sign before believing it
    (``docs/given-registration.md``). Recording a ``shifts`` correction is
    therefore allowed and applying it here is not, and the difference has to be
    said out loud: registration is the multi-hour step, and quietly dropping the
    answer would mean somebody waits for it and then gets the measured drift
    they were trying to replace.
    """
    import inspect

    try:
        takes = set(inspect.signature(run).parameters)
    except (TypeError, ValueError):
        return
    unusable = sorted({name for extra in recorded.values() for name in extra
                       if name not in takes})
    if not unusable:
        return
    raise ValueError(
        f"register: a correction names {', '.join(unusable)}, and this stage "
        f"calls {getattr(run, '__name__', run)!r}, which does not take "
        f"{'them' if len(unusable) > 1 else 'it'}. A shift table measured "
        "elsewhere is read, length-checked and sign-checked by 'auto_organotypic "
        "chain' — see docs/given-registration.md — not by this stage. Forget "
        "the correction with 'auto-organotypic correct <folder> --forget "
        f"register:{unusable[0]}', or run the chain instead.")


def _structural_channel(recording: Recording) -> int | None:
    """A channel whose structure does not itself move, if one is named."""
    for name in ("phase", "blue", "green"):
        try:
            return recording.channel_index(name)
        except LookupError:
            continue
    return None


def _outline_settings(recordings: Sequence[Recording],
                      options: Mapping[str, Any], root, *,
                      record: bool = False) -> dict[str, Any]:
    """Everything the outline stage would run with, worked out once.

    Two callers: the stage itself, and the grid that says what the stage would
    do. They must not each work this out — a grid built from its own idea of
    which channel and which plane the outline would take is a second
    implementation, and the way it fails is a plate that says it is finished
    and then spends an hour proving otherwise.

    ``record`` is the one thing that differs between them. Passing a drawing on
    the command line *keeps* it, so that the next run needs no flags; asking
    what would run must change nothing at all, so the grid asks with ``record``
    off. Everything else — the plan, the corrections, the drawings, the values
    given for the whole run, and the precedence between them — is identical.
    """
    plan_sources, per_source = sources.outline_plan(
        recordings,
        channel=options.get("channel", "red"),
        if_missing=options.get("if_missing", "raise"),
        include_incomplete=bool(options.get("include_incomplete", False)),
        allow_per_image=bool(options.get("allow_per_image", False)),
        scn_time=options.get("scn_time"))
    if not plan_sources:
        raise RuntimeError(
            "nothing to outline: every recording was either incomplete or had "
            "no matching channel. Pass include_incomplete=True for a live pull, "
            "or name a channel the manifest holds.")

    # What a person said, where they said it. Each joins the per-recording
    # settings the plan already carries, so the frozen method's own interface
    # is untouched and the batch layer needs no new argument.
    policy = str(options.get("rois_if_missing", "auto"))
    planned = [one for one in recordings if str(one.path) in per_source]

    # What somebody recorded earlier, first — so that a drawing passed on this
    # run overwrites it below. Per-recording still beats the whole-run values,
    # which are merged into the shared settings and lose to ``per_source``
    # inside ``batch.run``; nothing about that precedence changes here.
    said = _Corrections("outline", planned, options, root)
    for key, extra in said.per_source().items():
        per_source.setdefault(key, {}).update(extra)

    named = layout.names(planned)

    # A segment's answers, from the recording it was cut from. Inside this
    # function and not inside ``_outline``, for the reason the docstring above
    # gives: the grid predicts what this stage would do by calling it, and
    # inheritance arranged one level up would have the grid promise a
    # per-segment outline and the run deliver a shared one.
    inherited = _from_the_parent(planned, per_source, said, root, named)

    def keep(field: str, found: Mapping[str, Any], flag: str) -> None:
        if record:
            _record_given("outline", field, found, options, root, flag=flag,
                          known=named)

    # Three groups, one loop, off the ``corrigible`` table. ``outline_roi``
    # decides *whether* a recording is outlined at all under ``skip``; the
    # other two are answers about this recording's geometry and never about
    # whether to outline it, so ``skip`` reads as ``auto`` for them — a
    # recording nobody named an angle for gets the accepted rule, which is the
    # whole point of being able to name one for the three that need it.
    counted: dict[str, int] = {}
    outline_found: dict[str, Any] = {}
    for group in _stage("outline").corrigible:
        if not group.flag or not options.get(group.flag):
            continue
        found = group.read(planned, options[group.flag],
                           if_missing=("raise" if policy == "raise" else "auto"))
        for key, extra in found.items():
            per_source.setdefault(key, {}).update(extra)
        counted[group.fields[0]] = len(found)
        for field in group.fields:
            keep(field, found, f"--{group.flag.replace('_', '-')}")
        if group.fields[0] == "outline_roi":
            outline_found = found

    drawn = counted.get("outline_roi", 0)
    given_orientation = counted.get("orient_up_deg", 0)
    given_crop = counted.get("crop_region", 0)

    # The same three said once for the whole run. A per-recording answer wins,
    # because ``per_source`` wins over the shared settings in ``batch.run``.
    given: dict[str, Any] = {}
    if options.get("lobes") is not None:
        given["lobes"] = options["lobes"]
    if options.get("orient_up_deg") is not None:
        given["orient_up_deg"] = float(options["orient_up_deg"])
    if options.get("orient_flip"):
        given["orient_flip"] = True
    if options.get("outline_crop_region") is not None:
        given["crop_region"] = [int(one)
                                for one in options["outline_crop_region"]]

    return {"plan_sources": plan_sources, "per_source": per_source,
            "planned": planned, "said": said, "given": given,
            "inherited": inherited,
            "drawn": drawn, "outline_found": outline_found,
            # What each recording's folder is called under an output root.
            # Returned rather than recomputed by each caller for the reason the
            # rest of this function is shared: the stage writes there and the
            # grid predicts it, and two ways of deciding one folder is a
            # finished recording reported as never outlined.
            "names": named,
            "orientation": given_orientation, "crop": given_crop,
            "shared": {**given, **dict(options.get("outline_options") or {})}}


def _from_the_parent(planned: Sequence[Recording],
                     per_source: dict[str, dict[str, Any]],
                     said: "_Corrections", root,
                     named: Mapping[str, str]) -> dict[str, Any]:
    """A segment's answers: the recording it was cut from, then its own on top.

    **The lookup order for a segment, top wins:**

    1. what somebody recorded against this segment;
    2. what somebody recorded against the recording it was cut from;
    3. what the run was given for the whole plate;
    4. nothing, and the accepted method finds its own region.

    Written down because it is the fourth merge order in this file and the
    other three are all written down; a fifth unwritten one is how they start
    disagreeing with each other.

    Two epochs of one slice measured through **one** region is the point of
    splitting after the registration rather than before it. A registered slice
    has not moved between day two and day five, so a difference between the two
    epochs is then a difference in the signal rather than in where it was read.
    A segment that recorded its own region keeps it: somebody who drew for one
    epoch was looking at that epoch and meant it.

    The frame check the parent's drawing passed still holds for the segment. A
    split changes the time axis and nothing else, so the segment's outline
    input is the same width and height as the parent's — the picture the region
    was drawn on.
    """
    from . import split as _split_stage

    mine = said.per_source()
    groups: dict[str, dict[str, Any]] = {}
    own: list[str] = []
    for one in planned:
        if not one.segment:
            continue
        parent = _split_stage.parent_of(one.path)
        if parent is None:
            continue
        found = corrections.records_for_stage("outline", [parent], root=root)
        fields = {row.field: row.value for row in found.get(str(parent), ())}
        if not fields:
            continue
        here = mine.get(str(one.path), {})
        taken = {field: value for field, value in fields.items()
                 if field not in here}
        label = named.get(str(one.path)) or one.path.stem
        if taken:
            per_source.setdefault(str(one.path), {}).update(taken)
            row = groups.setdefault(str(parent), {
                "recording": str(one.field) or Path(parent).stem,
                "parent": Path(parent).name, "segments": [], "fields": []})
            row["segments"].append(label)
            row["fields"] = sorted(set(row["fields"]) | set(taken))
        if set(fields) & set(here):
            own.append(label)
    out: dict[str, Any] = {}
    if groups:
        # Named and not counted, for the reason ``Resume.report`` names every
        # adoption: "shared across 2" and "shared across 40" read identically
        # and mean very different things.
        out["outline_shared_across_segments"] = [
            {**row, "segments": sorted(row["segments"])}
            for row in groups.values()]
    if own:
        out["outline_drawn_per_segment"] = sorted(set(own))
    return out


def _outline(state: dict[str, Any],
             options: Mapping[str, Any]) -> dict[str, Any]:
    """The accepted outline over every recording, one process each if asked.

    Everything per-recording — which channel, which valid field, which
    identity — comes out of the manifest through
    :func:`auto_organotypic.sources.outline_plan`, so the frozen method sees only its
    own settings and the parity test over the ten accepted fields still means
    what it meant.
    """
    recordings = state["recordings"]
    root = state.get("output_root")
    plan = _outline_settings(recordings, options, root, record=True)
    plan_sources, per_source = plan["plan_sources"], plan["per_source"]
    given, shared, said = plan["given"], plan["shared"], plan["said"]
    drawn = plan["drawn"]
    given_orientation, given_crop = plan["orientation"], plan["crop"]

    # ``skip`` is the one policy that changes *which* recordings are outlined
    # rather than how, so it is applied here and not in the read-only plan: a
    # question about what would run must not be able to change the answer.
    if drawn and str(options.get("rois_if_missing", "auto")) == "skip":
        found = plan["outline_found"]
        plan_sources = [one for one in plan_sources if str(one) in found]
        per_source = {key: value for key, value in per_source.items()
                      if key in found}
        if not plan_sources:
            raise RuntimeError(
                "nothing to outline: rois_if_missing='skip' and nobody "
                f"drew a region for any of the {len(plan['planned'])} "
                "recordings in the plan.")
    # The resume knows what this stage *would do*, not only that it once did
    # something. Without that, a recording somebody has just redrawn is skipped
    # because last week's report is still on disk, and the drawing is discarded
    # with no error — which is the failure this whole stage of the plan exists
    # to end. It keeps the frame-count rule as well: both must pass to skip.
    resume = staleness.resume_for(
        recordings, "outline", shared,
        method_version=_batch.METHOD_VERSION,
        per_source=per_source,
        unmarked=str(options.get("unmarked", "adopt")),
        force=bool(options.get("force")))
    records = _batch.run(
        plan_sources,
        output_root=root,
        # Past ``per_source`` deliberately. A folder handed through the
        # settings would land in the resume key, and moving a run's outputs
        # would then make every recording in it look stale.
        output_dir_for=lambda source: _output_dir_for(
            "outline", source, root, plan["names"]),
        per_source=per_source,
        record_extras=sources.identities(recordings),
        is_done=resume.is_done,
        on_done=resume.on_done,
        workers=options.get("workers"),
        on_progress=options.get("on_outline"),
        **shared)

    manifest = (Path(root) / MANIFEST_NAME) if root is not None else None
    if manifest is not None and _io.isfile(manifest):
        # The loop closes here: this package reads its own manifest back the
        # same way it read the instrument's, so the trace stage below consumes
        # a contract record rather than a pile of filenames.
        state["recordings"] = sources.read_manifest(manifest,
                                                    instrument="auto_organotypic")
    else:
        state["recordings"] = _outlined(records, recordings)
    return {"outlined": sum(1 for row in records if row["ok"]),
            "hand_drawn": drawn,
            # Recordings whose answer was given, not answers given: a value
            # for the whole run covers every recording in the plan, and a
            # per-recording one beats it rather than adding to it.
            "given_orientation": (
                len(plan_sources)
                if ("orient_up_deg" in given or "orient_flip" in given)
                else given_orientation),
            "given_crop": (len(plan_sources) if "crop_region" in given
                           else given_crop),
            "skipped": sum(1 for row in records if row["skipped"]),
            "failed": sum(1 for row in records if row["ran"] and not row["ok"]),
            # Which recordings the resume let through and why. Named rather
            # than counted: "adopted 96" is a library converging on its first
            # run under stage keys, and "adopted 3" is somebody's outputs
            # having been half deleted, and those are the same number.
            "resume": resume.report(),
            **said.report(),
            **plan["inherited"],
            "manifest": str(manifest) if manifest else None}


def _outlined(records: Sequence[Mapping[str, Any]],
              before: Sequence[Recording]) -> list[Recording]:
    """The cropped recordings, when no batch manifest was written to read back."""
    from .batch import contract_entry

    was = {str(one.path): one for one in before}
    out: list[Recording] = []
    for row in records:
        entry = contract_entry(row)
        if entry is None or not entry.get("path"):
            continue
        origin = was.get(str(row.get("input")))
        out.append(replace(origin, path=Path(entry["path"]),
                           axes=entry.get("axes") or origin.axes)
                   if origin is not None else
                   Recording(path=Path(entry["path"]),
                             axes=entry.get("axes") or ""))
    return out


def _trace(state: dict[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
    """Region traces, the instrumental control and the rhythm verdict.

    One call covers three boxes of the diagram, which is why :class:`Stage` has
    a ``covers`` field: :mod:`auto_organotypic.region_trace` owns the order of those
    three. The instrumental control is the part worth knowing about — it runs
    before any circadian claim, because in the reference dataset a beautiful
    22.8 h rhythm turned out to be a daily focus cycle.

    Anything in ``trace_options`` reaches
    :func:`auto_organotypic.region_trace.run` unchanged, which is where a drawn
    region is passed as ``roi=``.
    """
    run = resolve(_stage("trace").target)
    root = state.get("output_root")
    named = layout.names(state["recordings"])
    visual_root = (layout.visual(root, layout.TRACE_PLOTS)
                   if root is not None else None)
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for one in state["recordings"]:
        settings = _trace_settings(options)
        if root is not None:
            settings.setdefault(
                "output_dir",
                str(_output_dir_for("trace", one.path, root, named)))
            if settings.get("visualise", True):
                short = named.get(str(one.path), str(one.field) or one.path.stem)
                settings.setdefault("visual_output_dir", str(visual_root))
                settings.setdefault("visual_name", f"{short}_trace")
                settings.setdefault("visual_label", short)
        if one.interval_s.value and "dt_min" not in settings:
            settings["dt_min"] = float(one.interval_s.value) / 60.0
        if one.pixel_size_um.value and "um_per_px" not in settings:
            settings["um_per_px"] = float(one.pixel_size_um.value)
        try:
            results.append({"path": str(one.path),
                            "field": str(one.field),
                            "result": run(str(one.path), **settings)})
        except Exception as exc:
            failures.append({"path": str(one.path), "error": str(exc)})
    grid_report = None
    supergraph_report = None
    display = _trace_settings(options)
    trace_folders = [row["result"].get("output_dir") for row in results]
    if (results and visual_root is not None
            and display.get("visualise", True)
            and display.get("visual_grid", True)
            and all(trace_folders)):
        from . import trace_plot

        grid_report = trace_plot.traces_to_grid(
            trace_folders,
            output_dir=visual_root, output_name="all_slices_traces",
            well_names=[row["field"] or Path(row["path"]).stem
                        for row in results],
            detrend=display.get("primary", trace_plot.DEFAULT_DETREND),
            regions=display.get("visual_regions", "auto"),
            trim_detrend_edges=display.get("trim_detrend_edges", True),
            circadian=display.get("circadian", "both"),
            xtick_hours=display.get("xtick_hours", 24.0),
            normalise=display.get("visual_normalise", "minmax"),
            signal=display.get("visual_signal", trace_plot.DEFAULT_SIGNAL),
            columns=display.get("visual_columns", 3),
            title=display.get("visual_title"),
            image_format=display.get("image_format", "png"),
            overwrite=True)
    if (len(results) > 1 and visual_root is not None
            and display.get("visualise", True)
            and display.get("visual_supergraph", True)
            and all(trace_folders)):
        from . import trace_plot

        supergraph_report = trace_plot.traces_to_supergraph(
            trace_folders,
            output_dir=visual_root,
            output_name="all_slices_trace_supergraph",
            well_names=[row["field"] or Path(row["path"]).stem
                        for row in results],
            detrend=display.get("primary", trace_plot.DEFAULT_DETREND),
            regions=display.get("visual_regions", "auto"),
            trim_detrend_edges=display.get("trim_detrend_edges", True),
            xtick_hours=display.get("xtick_hours", 24.0),
            normalise=display.get("visual_normalise", "minmax"),
            signal=display.get("visual_signal", trace_plot.DEFAULT_SIGNAL),
            average=display.get("visual_average", "mean"),
            trim_fraction=display.get("visual_trim_fraction", 0.1),
            time_range=display.get("visual_time_range", "intersection"),
            time_step_hours=display.get("visual_time_step_hours"),
            overlay_alpha=display.get("visual_overlay_alpha", 0.35),
            show_recording_legend=display.get(
                "visual_recording_legend", True),
            title=display.get("visual_title"),
            image_format=display.get("image_format", "png"),
            overwrite=True)
    return {"traced": len(results), "failed": len(failures),
            "trace_images": sum(bool(row["result"].get("visualisation"))
                                for row in results),
            "trace_grids": int(grid_report is not None),
            "trace_grid": grid_report,
            "trace_supergraphs": int(supergraph_report is not None),
            "trace_supergraph": supergraph_report,
            "failures": failures}


# ------------------------------------------------------- the display exports
@dataclass(frozen=True)
class _Export:
    """One display export: where it writes, and whether it writes one file."""

    kind: str            #: the ``visual/`` folder, from :mod:`layout`
    collective: bool     #: one artefact from every recording, not one each
    made: str            #: what the run record calls the number it produced


#: The four stages whose settings are one mapping straight through. Kept as
#: data because five separate places ask the same questions of them — the
#: validator, the opt-in test, the two runners and the command line — and four
#: near-identical stage functions is how the answers start to disagree.
EXPORTS: dict[str, _Export] = {
    "image": _Export(layout.IMAGES, False, "images"),
    "grid": _Export(layout.IMAGES, True, "sheets"),
    "video": _Export(layout.VIDEOS, False, "videos"),
    "video_grid": _Export(layout.VIDEOS, True, "mosaics"),
}


def _stage_options(name: str, options: Mapping[str, Any], *,
                   positional: Sequence[str] = ()) -> dict[str, Any]:
    """``<name>_options``, checked against the stage's own entry point.

    One function for the two stages that take their configuration as a mapping
    rather than through the export table. Never a list of accepted names kept
    here: a copy of somebody's signature drifts, and the way it fails is a
    setting this module refuses and the function would have taken — which reads
    as the feature not existing.
    """
    given = dict(options.get(f"{name}_options") or {})
    accepted = set(inspect.signature(resolve(_stage(name).target)).parameters)
    unknown = sorted(set(given) - accepted)
    if unknown:
        raise TypeError(
            f"{name}_options: {', '.join(unknown)} is not a setting of "
            f"{_stage(name).target.replace(':', '.')}. It takes "
            + ", ".join(sorted(accepted - set(positional))))
    return given


def _trace_settings(options: Mapping[str, Any]) -> dict[str, Any]:
    """``trace_options``, checked against this package's real entry point."""
    return _stage_options("trace", options, positional=("source",))


def _review_settings(options: Mapping[str, Any]) -> dict[str, Any]:
    """``review_options``, checked against :func:`auto_organotypic.review.run_to_review`."""
    return _stage_options("review", options, positional=("folder",))


def _export_signature(kind: str) -> inspect.Signature:
    """What the export actually accepts, asked of the function itself.

    Never a list of accepted names kept here. A copy of somebody's signature is
    a copy that drifts, and the way it fails is a setting that this module
    refuses and the function would have taken — which reads as the feature not
    existing.
    """
    return inspect.signature(resolve(_stage(kind).target))


def _export_settings(kind: str, options: Mapping[str, Any]) -> dict[str, Any]:
    """``<kind>_options``, checked against the real call.

    Every keyword of the four exports is reachable through here — that is the
    whole interface, and it is one mapping per stage rather than a hundred-odd
    keywords for the same reason ``register_options`` and ``trace_options``
    are: the words these use are ``when``, ``columns``, ``frames``, ``fps`` and
    ``label``, and a pipeline that took those at the top level would be
    claiming they mean something about the pipeline. They do not; they mean
    something about one picture or one movie.

    A key the function will not take is refused **here**, before the folder is
    read, rather than as a ``TypeError`` from inside a loop after the outline
    stage has already spent its minutes. Misspelling ``colums`` is the failure
    this catches, and it is the same argument as checking a trim window before
    a run rather than during one.
    """
    given = dict(options.get(f"{kind}_options") or {})
    accepted = set(_export_signature(kind).parameters)
    unknown = sorted(set(given) - accepted)
    if unknown:
        raise TypeError(
            f"{kind}_options: {', '.join(unknown)} is not a setting of "
            f"{_stage(kind).target.replace(':', '.')}. It takes "
            + ", ".join(sorted(accepted - {"sources", "source"})))
    return given


def _export_default(kind: str, name: str) -> Any:
    """One of the export's own defaults, for naming a file after it."""
    parameter = _export_signature(kind).parameters.get(name)
    return None if parameter is None else parameter.default


def _moment_word(when: Any) -> str:
    """``mean``, ``max``, or ``f00042`` — what a still is of, for its filename.

    The vocabulary is :data:`auto_organotypic.image.WHEN_WORDS`, asked of the module
    rather than written out again here.
    """
    from . import image as _image

    text = str(when).strip().lower()
    if text in _image.WHEN_WORDS:
        return text
    try:
        return f"f{int(when):05d}"
    except (TypeError, ValueError):
        return "moment"


def _interval_h(recording: Recording) -> float | None:
    """Hours between frames, from what the recording already knows.

    Handed to the export so the time burned into a picture is right, and so a
    movie asked for in experimental hours per second has the number it needs to
    turn that into a frame rate. The same wiring ``_trace`` does for ``dt_min``.
    Only ever a default: a caller who states the interval outright has said
    something this cannot contradict.
    """
    value = recording.interval_s.value
    return float(value) / 3600.0 if value else None


def _still_label(settings: Mapping[str, Any]) -> str:
    """What a still, or a sheet of stills, is *of* — for its filename."""
    moments = settings.get("moments")
    if moments:
        return f"{int(moments)}moments"
    when = settings.get("when", _export_default("grid", "when"))
    if isinstance(when, (list, tuple)):
        return (f"{len(when)}moments" if len(when) > 3
                else "-".join(_moment_word(one) for one in when))
    return _moment_word(when)


def _speed_label(kind: str, settings: Mapping[str, Any]) -> str:
    """What speed a movie plays at, for its filename.

    Read off what was asked for, and off the export's own signature where the
    caller said nothing — the same rule as everywhere else here, so a default
    changing in ``video/`` changes this without anybody editing it.
    """
    fps = settings.get("fps")
    if fps:
        return f"{float(fps):g}fps"
    asked = settings.get("hours_per_second")
    if asked is None:
        asked = _export_default(kind, "hours_per_second")
    if asked is None:
        from .video.exports import DEFAULT_HOURS_PER_SECOND

        asked = DEFAULT_HOURS_PER_SECOND
    return f"{float(asked):g}hps"


def _export_label(kind: str, settings: Mapping[str, Any]) -> str:
    """The part of a filename that says what this one is, past the well."""
    return (_speed_label(kind, settings) if EXPORTS[kind].kind == layout.VIDEOS
            else _still_label(settings))


def _each_recording(kind: str, state: dict[str, Any],
                    options: Mapping[str, Any]) -> dict[str, Any]:
    """One picture, or one movie, per recording. Display only.

    **The recordings are not touched.** Every measuring stage in this table
    hands the next one what it produced; the display exports hand on exactly
    what they were given. A PNG or an MP4 in ``state["recordings"]`` would
    reach ``trace``, which would then be asked to find cells in a lookup-table
    painted 8-bit picture of a stack — the one failure that would look like a
    result rather than like an error.
    """
    run = resolve(_stage(kind).target)
    root = state.get("output_root")
    named = layout.names(state["recordings"])
    shared = _export_settings(kind, options)
    label = _export_label(kind, shared)
    spec = EXPORTS[kind]

    made: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for one in state["recordings"]:
        settings = dict(shared)
        # ``--force`` in this stage's word for it, and the only reason a file
        # that is already there is drawn twice.
        settings.setdefault("overwrite", bool(options.get("force")))
        interval = _interval_h(one)
        if interval is not None:
            settings.setdefault("frame_interval_h", interval)
        if root is not None:
            settings.setdefault("output_dir",
                                str(layout.visual(root, spec.kind)))
            # Named for the well and for what the file is of, not for the
            # source stem. The stems here run to about 149 characters by the
            # time a recording has been registered, oriented and cropped, and
            # the path they lead to is already past what Windows allows before
            # a picture is added to it. What the filename stops carrying, the
            # sidecar carries.
            settings.setdefault("output_name",
                                f"{named[str(one.path)]}_{label}")
        try:
            made.append({"path": str(one.path), "field": str(one.field),
                         "report": run(str(one.path), **settings)})
        except Exception as exc:          # one bad stack is one picture
            failures.append({"path": str(one.path),
                             "error": f"{type(exc).__name__}: {exc}"})
    return {spec.made: len(made), "failed": len(failures),
            "of": label, "failures": failures,
            # In the run record and not only in each file's own sidecar,
            # because the run record is what somebody reads a month later when
            # deciding which of these a number may come off.
            "display_only": True,
            # Unchanged, and said out loud in the record: a reader who sees
            # ``trace`` run on the same count knows the pictures did not
            # displace the stacks.
            "recordings": len(state["recordings"])}


def _all_recordings(kind: str, state: dict[str, Any],
                    options: Mapping[str, Any]) -> dict[str, Any]:
    """Every recording as one sheet, or one mosaic. Display only.

    The only two stages in this table that produce a single artefact from all
    of the recordings at once, which is why they take them as a list where the
    others loop. Like ``_each_recording``, they hand the recordings on
    untouched.
    """
    run = resolve(_stage(kind).target)
    root = state.get("output_root")
    named = layout.names(state["recordings"])
    recordings = list(state["recordings"])
    spec = EXPORTS[kind]
    settings = _export_settings(kind, options)
    settings.setdefault("overwrite", bool(options.get("force")))

    # The well names, in the order the tiles are laid out, so a sheet of a
    # plate is labelled A1, B2 rather than with 149-character stems. The same
    # mapping the output folders are named from, so the label on a tile and the
    # folder its stack came from cannot disagree.
    settings.setdefault("well_names",
                        [named[str(one.path)] for one in recordings])
    # Only when every recording agrees. Recordings imaged at two intervals have
    # no single interval, and a caption that picked the first one would be
    # quietly wrong about the rest — the mosaic refuses that case by name, so
    # handing it one here would paper over the refusal rather than help.
    intervals = {_interval_h(one) for one in recordings}
    if len(intervals) == 1 and None not in intervals:
        settings.setdefault("frame_interval_h", intervals.pop())

    # A plate where one dish was imaged on a different cadence has no single
    # biological speed to play at, and the mosaic refuses it rather than play
    # the odd one fast. Here the alternative to resampling is no mosaic at all,
    # so a run asks for it: every tile on one grid of experimental hours, its
    # own nearest frame at each instant, and what that cost named per tile in
    # the record. A caller who wants the refusal still gets it by saying
    # ``on_cadence="raise"``.
    if kind == "video_grid":
        settings.setdefault("on_cadence", "resample")

    if root is not None:
        settings.setdefault("output_dir",
                            str(layout.visual(root, spec.kind)))
        settings.setdefault("output_name",
                            f"grid_{_export_label(kind, settings)}")

    try:
        report = run([str(one.path) for one in recordings], **settings)
    except Exception as exc:
        return {spec.made: 0, "failed": 1,
                "failures": [{"error": f"{type(exc).__name__}: {exc}"}],
                "display_only": True,
                "recordings": len(recordings)}
    return {spec.made: 1, "failed": 0,
            "tiles": len(report.get("tiles") or ()),
            "skipped": len(report.get("skipped") or ()),
            "display_only": True,
            "recordings": len(recordings)}


def _review(state: dict[str, Any],
            options: Mapping[str, Any]) -> dict[str, Any]:
    """How well the run went, drawn from what every stage above already wrote.

    The only stage that reads the *run* rather than the recordings. It opens no
    report this module has to interpret and re-judges nothing: a verdict in the
    review is the verdict the stage recorded, given a colour. So it can be run
    on a folder from last month, by somebody who was not there, and say the
    same thing.

    Last in the table, and display only. It hands the recordings on untouched
    for the reason the four exports do — a PNG in ``state["recordings"]`` is a
    picture something downstream would try to measure.

    A run with no output root has nothing to review: every stage wrote beside
    its own source under ``AI_Exports``, and there is no one folder holding the
    outline, the registration and the traces. That is said out loud rather than
    guessed at, because guessing would mean reviewing whichever recording's
    folder was found first.
    """
    run = resolve(_stage("review").target)
    root = state.get("output_root")
    if root is None:
        return {"reviewed": 0, "failed": 0,
                "reason": "a review reads a whole run's output folder, and "
                          "this run has no output root: pass -o/--output-root "
                          "to put every stage under one folder.",
                "display_only": True,
                "recordings": len(state["recordings"])}
    settings = _review_settings(options)
    settings.setdefault("overwrite", True)  # a review of a rerun is a new one
    try:
        report = run(str(root), **settings)
    except Exception as exc:
        return {"reviewed": 0, "failed": 1,
                "failures": [{"error": f"{type(exc).__name__}: {exc}"}],
                "display_only": True,
                "recordings": len(state["recordings"])}
    return {"reviewed": len(report.get("panels") or ()),
            "failed": len(report.get("skipped_panels") or ()),
            "page": report.get("page"),
            "counts": report.get("counts"),
            "skipped_panels": report.get("skipped_panels"),
            "display_only": True,
            "recordings": len(state["recordings"])}


def _export_runner(kind: str):
    """The stage function for one export, bound to its name."""
    body = _all_recordings if EXPORTS[kind].collective else _each_recording

    def run(state: dict[str, Any],
            options: Mapping[str, Any]) -> dict[str, Any]:
        return body(kind, state, options)

    run.__name__ = f"_{kind}"
    run.__doc__ = body.__doc__
    return run


_RUNNERS: dict[str, Callable[[dict[str, Any], Mapping[str, Any]], dict[str, Any]]] = {
    "acquire": _acquire,
    "index": _index,
    "trim_before_crop": _trim_before_crop,
    "broad_crop": _broad_crop,
    "trim": _trim,
    "register": _register,
    "split": _split,
    "outline": _outline,
    **{name: _export_runner(name) for name in EXPORTS},
    "trace": _trace,
    "review": _review,
}


# ------------------------------------------------------------ the pipeline
def run_pipeline(folder=None, *,
                 experiment: Any = None,
                 instrument: str | None = None,
                 output_root=None,
                 channel: str | int = "red",
                 stages: Sequence[str] | None = None,
                 skip: Sequence[str] = (),
                 since: str | None = None,
                 only: Sequence[str] = (),
                 force: bool = False,
                 what_would_run: bool = False,
                 broad_crop: bool = False,
                 image: bool = False,
                 grid: bool = False,
                 video: bool = False,
                 video_grid: bool = False,
                 review: bool = False,
                 allow_pending: bool = False,
                 dry_run: bool = False,
                 run_record: str | None = RUN_RECORD_NAME,
                 on_progress: Callable[[Mapping[str, Any]], None] | None = None,
                 **options: Any) -> dict[str, Any]:
    """The whole sequence, in order, over one folder of recordings.

    ``folder`` is where the recordings are, or where an acquisition will put
    them. Give ``experiment`` and ``instrument`` as well and the run starts by
    pulling: a vessel id for the Incucyte, an experiment name for the LV200.
    Without them the folder is taken as already pulled, and ``acquire`` is
    recorded as not needed rather than skipped, because those are different
    things.

    ``stages`` runs only those, in pipeline order; ``skip`` drops some from the
    full run. Asking for any stage after ``index`` quietly adds ``index``, since
    every one of them works on the recordings it read — unless ``skip`` names it,
    which is the way to say you meant the empty list. ``broad_crop`` opts into
    the coarse box around the tissue before registration, with
    ``broad_crop_mode`` choosing ``tight``, ``standard`` or ``wide``. Off by
    default: it rewrites every stack it crops, and a person who has already
    framed their recordings should not have that happen without asking.
    ``dry_run`` resolves every stage and reports what would run without running
    any of it.

    **Pictures.** ``image=True`` adds a still per recording and ``grid=True``
    adds one tiled sheet of all of them, both after the outline and before the
    trace — so "index it, outline it and show me" is a complete run on a
    machine with no rhythm extra. Opt-in for the reason the broad crop is:
    somebody who wanted traces did not ask for ninety-six PNGs.

    Each is configured by one mapping that goes straight to its own call, and
    **every keyword of that call is reachable through it**::

        run_pipeline(folder, image=True,
                     image_options={"when": "max", "lut": "fire",
                                    "display_range": (0, 4095),
                                    "timestamp_format": "clock"})
        # Six columns over one circadian cycle of each slice -- the best cycle
        # it has, on CT, from its own rise. All three are the default.
        run_pipeline(folder, grid=True,
                     grid_options={"moments": 6, "tile_label": "both"})
        # The same six columns spread over the whole recording instead, which
        # answers how one phase changed over the week rather than how one cycle
        # looks. A column is still a time and the gap divides the period.
        run_pipeline(folder, grid=True,
                     grid_options={"moments": 6, "between": "window",
                                   "time_scale": "ct", "align": "onset"})
        run_pipeline(folder, video_grid=True,
                     video_grid_options={"columns": 4, "outline": "accepted",
                                         "hours_per_second": 24})

    One mapping rather than forty keywords, and passing one is itself the
    opt-in. The words these take — ``when``, ``columns``, ``frames``,
    ``label`` — say something about a picture and nothing about a pipeline, and
    a run that accepted ``columns=4`` at the top level would be claiming
    otherwise. A key the export will not take is **refused by name before the
    run starts**, checked against the function's own signature rather than
    against a list kept here.

    Four settings are filled in from what the run already knows, and each is
    only a default that anything in the mapping overrides: where the file goes
    (``visual/images/`` or ``visual/videos/`` under the output root), what it is
    called (the well and what it is of, because the source stems reach 149
    characters by this point), the frame interval from the recording's own
    metadata, and — for the sheet and the mosaic — the well names for their tile
    labels.

    **Nothing they write may be measured from**, and neither stage touches the
    recordings it was given: a lookup-table painted 8-bit picture reaching
    ``trace`` would be the one failure that looks like a result.

    **The review.** ``review=True`` adds a last display stage that reads what
    every stage above recorded — the crop detector's reason, registration's
    residual against its own threshold, the outline's verification line and
    open questions, the orientation's three top-end cues, the square crop's
    clipped-pixel count, the instrumental control — and draws it: a scorecard
    of recordings against checks, the crop-and-orientation picture strip, and a
    panel per stage, tied together by one ``visual/review/review.html``. It
    re-judges nothing, so it cannot disagree with a report; ``review_options``
    reaches :func:`auto_organotypic.review.run_to_review` the way the export mappings
    reach theirs. It needs an output root, since a review is about a run and
    not about a recording.

    **Hand-drawn regions and given numbers.** Four measured steps will take the
    answer instead, one option each for a folder of drawings and one for a
    value that holds across the run:

    =============  =======================  ================================
    step           drawn                    said
    =============  =======================  ================================
    broad crop     ``broad_crop_rois``      ``broad_crop_region``
    SCN outline    ``outline_rois``         —
    orientation    ``orient_rois``          ``orient_up_deg``, ``orient_flip``
    square crop    ``outline_crop_rois``    ``outline_crop_region``
    =============  =======================  ================================

    **Every one of them names its stage**, because this function runs both
    crops. ``crop_rois`` used to mean the first of them and is now refused by
    name: a spelling that means the broad crop here and the square crop in a
    command that only outlines is a mistake nothing in a finished run would
    show.

    They are separate options and not one because the stages read different
    frames: the broad crop reads the raw recording, the outline reads the
    registered stack's selected plane, the square crop is cut from the oriented
    canvas, and an ImageJ region does not record which frame it was drawn on.
    A drawing for one recording beats a value given for the run.

    **Passing one of them records it.** A drawing is kept beside the results,
    against the recording's contents, with the picture it was made on — so
    running the same command again with **no flags at all** applies it, and so
    does a run next month or on a colleague's machine. That is what makes
    correcting one well and rerunning the folder the whole interface: the
    corrected recording reruns from the corrected stage down, and nothing
    re-registers. ``auto-organotypic correct <folder> --list`` shows what is
    recorded and ``--forget`` removes one.

    A drawing passed *now* beats one recorded before, because the person
    looking at the picture today is the most recent answer.
    ``ignore_corrections=True`` applies none of them for one run — they are
    still read, and still named in the run record, so the run says what it did
    not do.

    A drawing whose recorded frame is not the frame it is about to be applied
    to is **refused by name before the stage starts**, not several minutes into
    one. An ImageJ region records pixel coordinates and not the image they came
    from, so this is the only moment the mistake is catchable.

    ``rois_if_missing`` decides what a recording nobody drew gets — ``auto``
    (the default) leaves it to the automatic method, ``skip`` leaves it alone,
    ``raise`` stops the run. ``skip`` applies to the two stages that can decline
    to act; for the orientation and the square crop it reads as ``auto``, since
    a recording still has to be turned and framed somehow. Giving
    ``broad_crop_rois`` or ``broad_crop_region`` turns the broad crop stage on,
    since asking for a particular crop is asking to be cropped.

    ``allow_pending`` lets a run continue past a stage whose package is not
    installed, which is how "index and outline what I already pulled" works on
    a machine with no instrument client and no rhythm extra. Off by default: a
    pipeline that quietly did
    four of its six steps and reported success is the failure this whole record
    exists to prevent.

    Everything else is passed to the stage that wants it — ``channel``,
    ``workers``, ``scn_time``, ``include_incomplete``, ``registration_channel``,
    and the ``*_options`` mappings that go straight to a package's own call.

    Returns the run record: what each stage did, what it produced, how long it
    took, and whether the run finished.
    """
    # Claim the artefact store for this run before any stage can reach it.
    # The store's location is process-wide and the first caller to ask sets it,
    # so a run that let some other import ask first would put its derived
    # arrays in another project's folder and then stop its own earlier stages
    # resolving. Whoever is driving keeps the store, and here that is this
    # function.
    config.use_project()

    if "crop_rois" in options:
        # Refused rather than accepted as one of the two: the whole point of
        # the rename is that nobody can mean the wrong crop by accident, and a
        # keyword quietly kept working would leave every existing script
        # meaning whichever stage it happened to mean before.
        raise ValueError(
            "crop_rois does not name a stage: this pipeline crops twice. Pass "
            "broad_crop_rois= for the box before registration, or "
            "outline_crop_rois= for the square crop around the outline.")
    if str(options.get("rois_if_missing", "auto")) not in {"auto", "skip",
                                                           "raise"}:
        raise ValueError(
            "rois_if_missing must be 'auto', 'skip' or 'raise'; got "
            f"{options['rois_if_missing']!r}")
    chosen = list(stages) if stages else list(stage_names())
    if not stages:
        if experiment is None:
            chosen.remove("acquire")
        # Asking for a particular crop is asking to be cropped.
        if not broad_crop and not options.get("broad_crop_rois") \
                and options.get("broad_crop_region") is None:
            chosen.remove("broad_crop")
        # And asking for a particular picture is asking for the picture. Same
        # rule as the crop above: a caller who spelled out how the sheet should
        # be laid out has already said they want a sheet, and making them say
        # it twice is a flag to forget.
        asked = {"image": bool(image), "grid": bool(grid),
                 "video": bool(video), "video_grid": bool(video_grid)}
        for kind in EXPORTS:
            if not asked[kind] and not options.get(f"{kind}_options"):
                chosen.remove(kind)
        # The review, by the same rule: naming how it should be drawn is
        # asking for it, so nobody has to pass ``--review`` twice.
        if not review and not options.get("review_options"):
            chosen.remove("review")
    unknown = [name for name in chosen if name not in _RUNNERS]
    if unknown:
        raise KeyError(f"no such stage(s): {', '.join(unknown)}; the pipeline "
                       f"is {', '.join(stage_names())}")
    if since is not None:
        if since not in stage_names():
            raise KeyError(f"no such stage: {since!r}; the pipeline is "
                           f"{', '.join(stage_names())}")
        # This stage and everything after it. ``--from outline`` on a plate
        # that has already been registered is the ordinary way of saying "the
        # method improved, redo the cheap half" without touching the hours.
        after = stage_names()[stage_names().index(since):]
        chosen = [name for name in chosen if name in after]
    chosen = [name for name in stage_names()
              if name in chosen and name not in set(skip)]
    # Every stage after ``index`` works on the recordings it read, so asking for
    # one of them alone can only have meant "and read the folder first". Left
    # out, they run over an empty list and report a run that did nothing.
    if any(name != "acquire" and name != "index" for name in chosen) and             "index" not in chosen and "index" not in set(skip):
        chosen.insert(0 if "acquire" not in chosen else 1, "index")

    # Before the folder is read, and before a single stage runs. These two take
    # their whole configuration as one mapping, so a misspelled key is only
    # discoverable by asking the function — and discovering it from inside the
    # loop would mean finding out after the outline stage had already spent its
    # minutes. Same argument as checking a trim window up front.
    for kind in EXPORTS:
        if kind in chosen:
            _export_settings(kind, options)
    if "trace" in chosen:
        _trace_settings(options)
    if "review" in chosen:
        _review_settings(options)

    root = Path(output_root).resolve() if output_root is not None else None
    if folder is None:
        raise ValueError(
            "run_pipeline needs a folder: where the recordings are, or where "
            "an acquisition should put them")
    state: dict[str, Any] = {"folder": Path(folder).resolve(),
                             "output_root": root, "recordings": []}
    settings = {"experiment": experiment, "instrument": instrument,
                "channel": channel, "only": tuple(only), "force": bool(force),
                **options}

    target = None
    if run_record:
        candidate = Path(run_record)
        target = candidate if candidate.is_absolute() else \
            (root or state["folder"]) / candidate.name
    run = _Run(state["folder"],
               None if (dry_run or what_would_run) else target,
               on_progress)
    run.payload.update({"output_root": str(root) if root else None,
                        "stages_planned": chosen,
                        "stages_not_run": [name for name in stage_names()
                                           if name not in chosen]})

    # Human answers go under the output root too, so a run that was told
    # where to write leaves nothing beside the sources. Restored after,
    # because this is process-global and the next thing in this process
    # may not be a run with an output root -- or may be a run without one,
    # which must write beside its sources as it always has.
    before_decisions = config.use_decisions(
        layout.decisions(root) if root is not None else None)
    try:
        for name in chosen:
            stage = _stage(name)
            started = time.monotonic()
            # ``--only`` narrows the plate the moment the folder has been read, so
            # every stage below sees three recordings rather than ninety-six. That
            # ordering is what makes ``--only B2 --force`` cost one well instead of
            # re-registering the other ninety-five, which is the whole point of it.
            if only and name not in ("acquire", "index") and state["recordings"]:
                state["recordings"] = sources.select(state["recordings"], only)
            if what_would_run and name == "acquire":
                # Never. Asking what would run must not talk to the microscope.
                continue
            if what_would_run and name != "index":
                # After ``index`` and not before: the grid is one column per
                # recording, and there are no recordings until the folder has been
                # read. Reading a manifest writes nothing, which is why it is the
                # one stage this may do.
                grid = _grid(state, settings, chosen)
                run.payload["grid"] = grid.as_dict()
                run.payload["what_would_run"] = True
                if on_progress is not None:
                    # ``what_would_run`` and not ``grid``. This event is the
                    # *staleness* grid — a stage-by-recording table of text —
                    # and there is now a stage called ``grid`` that draws a
                    # picture. While both answered to one name the picture
                    # stage's progress line was printed as the empty string,
                    # because the consumer could not tell them apart.
                    on_progress({"stage": "what_would_run",
                                 "status": "would run", "seconds": 0.0,
                                 "text": grid.render(folder=str(state["folder"]))})
                # Nothing after ``index`` runs, and no record is written: a question
                # about what would happen must not be able to change the answer.
                return run.payload
            if dry_run:
                ready = "chosen at run time" if not stage.target else "ready"
                try:
                    if stage.target:
                        resolve(stage.target)
                except StagePending as exc:
                    ready = f"pending: {exc}"
                run.note(name, "would run", 0.0, owner=stage.owner, ready=ready)
                continue
            try:
                detail = _RUNNERS[name](state, settings)
            except StagePending as exc:
                run.note(name, "pending", time.monotonic() - started,
                         owner=stage.owner, reason=str(exc), install=stage.needs)
                if allow_pending:
                    continue
                raise StagePending(
                    f"{name} ({stage.owner}) is not installed: {exc}"
                    + (f". Install it with 'pip install {stage.needs}'"
                       if stage.needs else "")
                    + ". Pass allow_pending=True to run the stages that are."
                ) from exc
            except Exception as exc:
                run.note(name, "failed", time.monotonic() - started,
                         owner=stage.owner, error=f"{type(exc).__name__}: {exc}")
                run.payload["complete"] = False
                run.write()
                raise
            run.note(name, "ok", time.monotonic() - started,
                     owner=stage.owner, **detail)

        run.payload["complete"] = True
        run.payload["recordings"] = [one.to_dict() for one in state["recordings"]]
        run.write()
        return run.payload
    finally:
        config.use_decisions(before_decisions)
