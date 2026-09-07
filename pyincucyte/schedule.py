"""Register one-pass PyIncucyte downloads with Windows Task Scheduler.

Continuous collection belongs to a watcher left running - the desktop app, or
``pyincucyte watch`` in a terminal. A scheduled download is the same job with
nothing resident: Windows starts one ``pyincucyte watch --once`` process, that
process asks whether a chunk is due, writes it or does not, and exits. There is
no state between firings to lose, which is what lets a schedule outlive the
app, the logon session and the power being turned off.

**The holding rules already survive a power cut, and cost nothing to keep.**
``batch_frames`` waits until a number of new timepoints exist and counts the
source against the on-disk ledger; ``batch_after`` waits until the oldest
waiting timepoint is old enough and measures that from the timepoint's OWN
acquisition time on the microscope. Neither stores anything. A cold process
started after a reboot therefore reaches exactly the decision the killed one
would have reached, which is why one process per firing is safe at all.

**What does not survive is the task, unless it is registered deliberately.**
Measured on a real Windows 11 machine by creating a task from the plain
``schtasks`` flags and reading back what Windows filled in:

===========================  ==================  =========================
setting                      Windows default     what it costs after a
                                                 power-off
===========================  ==================  =========================
``LogonType``                InteractiveToken    runs only while that user
                                                 is logged ON - reboot to
                                                 the login screen and
                                                 nothing is collected, and
                                                 nothing says so
``StartWhenAvailable``       absent (false)      a firing missed while the
                                                 machine was off is never
                                                 caught up
``DisallowStartIfOnBatteries`` true              never starts unplugged
``StopIfGoingOnBatteries``   true                killed mid-download when
                                                 the power goes
``WakeToRun``                absent (false)      a sleeping PC sleeps
                                                 through every firing
===========================  ==================  =========================

None of those five can be set through ``schtasks`` flags, so the task is
registered from a definition instead: :func:`definition` writes all five and
:func:`verify` reads them back off the registered task afterwards. A task that
was created is not yet a task that will run - the same rule ``setup.verify``
follows, for the same reason.

**A logged-out task needs the account's own credential, and there is exactly
one way to supply it that keeps it off every disk this package owns.**
``schtasks /create ... /ru <account>`` with no ``/rp`` asks for it at the
console itself and hands the answer straight to Windows, which keeps it in the
task credential store. So it is never an argument, never in a log and never in
config - and the command must run with a real console attached and nothing
captured, or it reads end-of-file and fails without ever asking. That is the
same trap ``setup.apply_plan`` carries for ``New-LocalUser``, and it is marked
the same way: :data:`PROMPTS`.

**A schedule holds a recipe, not a command line.** :class:`Job` is one
``ExportOptions``, and :meth:`Job.argv` is the single place that becomes
``pyincucyte watch ... --once``. Everything here accepts either, so a front end
that already has argv still works - but a caller should hand over the recipe,
because a recipe can be saved, compared and put in a preset, and a list of
tokens can only be re-typed. This module used to take the argv list first,
which quietly made the command line the real interface and the Python one a
wrapper that had to spell its request in flags.

Windows' other way of running a task logged out - the "do not store it"
option, an S4U logon - is deliberately not offered. It has no access to the
user's credential store, which is where ``pyincucyte login`` put the credential
for the microscope's share, so every firing would fail with access denied.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace as _replace
from datetime import datetime, timedelta
from pathlib import Path
from inspect import Parameter as _Parameter, signature as _signature
from typing import Sequence

from .errors import IncucyteError
from .options import parse_span


#: Every task this package owns starts with this, so a listing can be scoped to
#: what PyIncucyte created and `remove` can refuse to delete anything else.
TASK_PREFIX = "PyIncucyte scheduled download"

#: The cadences the desktop app offers, each as the duration a person would
#: type at a prompt. One spelling for all three front ends: the window passes
#: the same `--every 6h` the command line takes, so a schedule made by clicking
#: and one made by typing cannot drift apart.
CADENCES = {
    "Every 10 minutes": "10m",
    "Every 30 minutes": "30m",
    "Every hour": "1h",
    "Every 6 hours": "6h",
    "Every day": "1d",
}
DEFAULT_CADENCE = "Every hour"

#: Bounded on purpose. `IgnoreNew` means a firing that is still running blocks
#: the next one, so an unlimited run that hangs over a dead share would silence
#: the schedule for ever; long enough that a whole-run rewrite over SMB fits.
DEFAULT_TIME_LIMIT = "PT12H"

#: The one composed command that asks a human for a credential, and therefore
#: the one that must be run with the console attached and nothing captured.
#: Same marker, and the same reason, as `setup.Action.prompts`.
PROMPTS = "credential"

#: What `verify` insists the registered task actually says.
_VERIFIED = ("LogonType", "StartWhenAvailable", "DisallowStartIfOnBatteries",
             "StopIfGoingOnBatteries", "WakeToRun")

#: **Windows omits a setting whose value is the schema default**, so reading a
#: tag back as absent is an answer, not a failure to answer - and treating it
#: as a mismatch made `verify` reject a task Windows had taken perfectly.
#: Measured here: a task asked for `WakeToRun` false came back with no
#: `WakeToRun` at all, while one asked for true carried it. The four defaults
#: below are from the same measurements; `LogonType` is deliberately absent
#: from the table, because it is always written and an absent one is a task
#: nothing can vouch for.
_ABSENT_MEANS = {
    "StartWhenAvailable": "false",
    "WakeToRun": "false",
    "DisallowStartIfOnBatteries": "true",
    "StopIfGoingOnBatteries": "true",
}


class ScheduleError(IncucyteError):
    """Windows refused a scheduled download, or created the wrong one."""


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------

#: What a scheduled download must be able to say for itself. A task fires with
#: no current directory worth the name - Windows starts it in system32 - so a
#: relative output folder would write a whole plate somewhere nobody would ever
#: look.
MUST_BE_ABSOLUTE = ("output",)


@dataclass(frozen=True)
class Job:
    """What one firing downloads: the recipe, and nothing else.

    This is what a schedule is *about*. The command line is one rendering of
    it, produced here and nowhere else - so a schedule made from Python and one
    made at a prompt cannot express different things, and neither front end has
    to know what the other's flags are called.

    Everything in this module accepts a `Job` or an argv list already built.
    The list is what the desktop app and the migration path still hand over;
    the `Job` is what a caller should reach for, because a recipe can be saved,
    compared and put in a preset and a list of tokens cannot.
    """

    #: The recipe. ``None`` means the built-in defaults, which is refused -
    #: a scheduled download with no output folder has nowhere to write.
    options: object = None

    def recipe(self):
        """The recipe as a scheduled task can safely carry it.

        Windows starts a task in system32, so a relative output folder writes a
        whole plate somewhere nobody would ever look. This used to live in the
        command line, which meant a schedule made from Python skipped it.
        """
        from .options import ExportOptions

        options = self.options if self.options is not None else ExportOptions()
        if not options.output:
            raise ScheduleError(
                "A scheduled download needs the folder to write into: "
                "-o <dir>. Windows starts a task with no useful current "
                "directory, so there is nowhere sensible to default to.")
        changed = {}
        for field in MUST_BE_ABSOLUTE:
            value = getattr(options, field, None)
            if value and not Path(value).is_absolute():
                changed[field] = str(Path(value).resolve())
        return _replace(options, **changed) if changed else options

    def label(self):
        """What to call the task when nobody named it - the plates it covers."""
        options = self.recipe()
        return ", ".join(str(v) for v in options.vessels) or "download"

    def argv(self):
        """The command line one firing runs.

        Through the recipe's own `cli_args`, never assembled by hand: the
        scheduled command line is the only record of what a task downloads, and
        a second way of writing it is a second thing to keep in step.
        """
        return [*self.recipe().cli_args("watch"), "--once"]


def argv_of(job):
    """The argv a `Job` renders to, or an argv list handed over as it is."""
    if isinstance(job, Job):
        return job.argv()
    return [str(one) for one in (job or ())]


def task_name(label):
    """Prefix every owned task so it cannot be confused with another tool."""
    label = str(label or "").strip()
    if label.startswith(TASK_PREFIX):
        return label
    return "%s - %s" % (TASK_PREFIX, label) if label else TASK_PREFIX


def default_account():
    """The account a scheduled download runs as, unless one is named.

    The credential for the microscope's share is in THIS user's credential
    store, put there by `pyincucyte login`, so the task has to be this user or the
    share is unreachable however the task is registered.
    """
    return os.environ.get("USERNAME") or ""


# ---------------------------------------------------------------------------
# the definition
# ---------------------------------------------------------------------------

def cadence_of(spec):
    """``'6h'`` -> ``('hourly', 6)``, the pair Task Scheduler thinks in.

    One flag rather than two. `--every hourly --mo 6` is schtasks' own
    vocabulary and nothing else in this package speaks it; every other period
    here is written `30m` / `48h`, and two spellings for one idea is how
    somebody sets a cadence they did not mean.
    """
    # Through `parse_span`, which is this package's own unsigned duration -
    # the same thing `batch_after` takes. A cadence has no direction, so the
    # signed `parse_duration` would be the wrong parser for it.
    span = parse_span(spec)
    seconds = span.total_seconds() if span else None
    if seconds is None or seconds <= 0:
        raise ScheduleError(
            "Read %r as no cadence at all. Write it as a period - 10m, 1h, "
            "6h, 1d." % (spec,))
    if seconds % 60:
        raise ScheduleError(
            "Windows schedules whole minutes; %s is not one." % (spec,))
    minutes = int(seconds // 60)
    if minutes < 60:
        return "minute", minutes
    if minutes == 1440:
        return "daily", None
    if minutes % 60 or minutes > 1440:
        raise ScheduleError(
            "A cadence is whole minutes under an hour, whole hours up to a "
            "day, or 1d. %s is none of those." % (spec,))
    return "hourly", minutes // 60


def interval_of(every="hourly", modifier=None):
    """The repetition an ISO 8601 duration, or None for a plain daily run."""
    every = str(every or "hourly").lower()
    if every == "minute":
        return "PT%dM" % int(modifier or 1)
    if every == "hourly":
        return "PT%dH" % int(modifier or 1)
    if every == "daily":
        if modifier and int(modifier) != 1:
            raise ScheduleError(
                "A daily schedule repeats once a day; ask for hours instead.")
        return None
    raise ScheduleError(
        "Unknown cadence %r. Use minute, hourly or daily." % every)


def runner_command(job, runner=None, frozen=None):
    """What Windows should start, as (executable, argument list).

    A scheduled download must be one poll: `--once` is appended rather than
    required, because a task that polled for ever would still be running when
    the next firing came round and `IgnoreNew` would skip it silently.

    Windowless where it can be. A task firing every ten minutes on the machine
    somebody is working at flashes a console each time otherwise, and a flashing
    window is how a schedule gets deleted.
    """
    cli_args = argv_of(job)
    if not cli_args or cli_args[0] != "pyincucyte":
        raise ScheduleError("Scheduled arguments must begin with pyincucyte.")
    remainder = cli_args[1:]
    # `watch` need not come first: a pyincucyte command line carries its
    # globals - `--host`, `--json` - ahead of the verb. Insisting on position
    # would refuse exactly the command the desktop app builds.
    if "watch" not in remainder:
        raise ScheduleError("A scheduled download must run pyincucyte watch.")
    if "--once" not in remainder:
        remainder = [*remainder, "--once"]

    # `runner` names the executable and nothing else. It used to drop the
    # prefix with it, which turned `python.exe watch ...` into a command that
    # runs the interpreter on a file called "watch" - a task that fails on
    # every firing, hours after anybody was watching.
    frozen = bool(getattr(sys, "frozen", False) if frozen is None else frozen)
    if frozen:
        executable, prefix = str(runner or sys.executable), ["--scheduled-cli"]
    else:
        executable = str(runner) if runner else _windowless(sys.executable)
        prefix = ["-m", "pyincucyte"]
    return executable, [*prefix, *remainder]


def _windowless(executable):
    """`pythonw.exe` beside the interpreter, when there is one."""
    path = Path(str(executable))
    if path.name.lower() == "python.exe":
        quiet = path.with_name("pythonw.exe")
        if quiet.exists():
            return str(quiet)
    return str(executable)


def self_command(cli_args, console=False):
    """This package's own CLI as a runnable command, for a front end.

    The window has no console, and the registration asks for a credential, so
    it cannot simply call the command function the way it calls every other
    one - it has to start a real process with a real console. `console=True`
    is the inverse of `_windowless`: a `pythonw.exe` given CREATE_NEW_CONSOLE
    still has no standard input, so the prompt would appear and immediately
    read end-of-file.
    """
    cli_args = [str(one) for one in (cli_args or ())]
    if not cli_args or cli_args[0] != "pyincucyte":
        raise ScheduleError("A pyincucyte command must begin with pyincucyte.")
    if getattr(sys, "frozen", False):
        return [str(sys.executable), "--scheduled-cli", *cli_args[1:]]
    executable = str(sys.executable)
    if console:
        path = Path(executable)
        if path.name.lower() == "pythonw.exe":
            loud = path.with_name("python.exe")
            if loud.exists():
                executable = str(loud)
    return [executable, "-m", "pyincucyte", *cli_args[1:]]


def _xml_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def expected_settings(*, logged_out=True, wake=False):
    """What the registered task must say, once Windows has taken it."""
    return {
        # `Password` is what Windows records for a task registered with an
        # account and a credential; it is the whole difference between a
        # schedule that runs on a locked, freshly rebooted machine and one that
        # waits for somebody to log in.
        "LogonType": "Password" if logged_out else "InteractiveToken",
        "StartWhenAvailable": "true",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "WakeToRun": "true" if wake else "false",
    }


def definition(job, *, every="hourly", modifier=None, runner=None,
               frozen=None, logged_out=True, account=None, wake=False,
               start=None, start_after=None,
               time_limit=DEFAULT_TIME_LIMIT, description=None):
    """The task definition XML, without touching Windows.

    Every setting the plain flags get wrong is written here explicitly, and
    `verify` reads each one back afterwards.
    """
    executable, arguments = runner_command(job, runner=runner, frozen=frozen)
    interval = interval_of(every, modifier)
    account = str(account or default_account())
    if logged_out and not account:
        raise ScheduleError(
            "A download that runs while nobody is logged in needs the account "
            "it should run as, and this machine did not say who that is. "
            "Name one with --account.")
    # The next whole minute by default, and it must be in the FUTURE. A
    # boundary in the past is a firing `StartWhenAvailable` considers missed,
    # so Windows starts the task the instant it is registered - measured here,
    # on a task created with the boundary rounded down: it was already
    # `Running` by the time the registration returned. Which turns "set up a
    # schedule" into "start a download now", possibly of a whole nine-day run.
    # One minute ahead still gives a first check straight away, without a
    # surprise; `start_after` puts it off longer, which is how a week of
    # acquisition is left alone rather than polled hourly for six days to be
    # told "nothing was due".
    if start is None:
        delay = (timedelta(minutes=1) if start_after is None
                 else _span(start_after))
        start = datetime.now().replace(second=0, microsecond=0) + delay

    repetition = ("""
      <Repetition>
        <Interval>%s</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>""" % interval) if interval else ""
    principal = ("""
      <UserId>%s</UserId>
      <LogonType>%s</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>""" % (
        _xml_escape(account),
        "Password" if logged_out else "InteractiveToken"))

    return _TEMPLATE % {
        "description": _xml_escape(
            description or "One PyIncucyte download check. Nothing stays running "
                           "between firings."),
        "start": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "repetition": repetition,
        "principal": principal,
        "wake": "true" if wake else "false",
        "limit": _xml_escape(time_limit),
        "command": _xml_escape(executable),
        "arguments": _xml_escape(subprocess.list2cmdline(arguments)),
    }


def _span(value):
    """``'7d'`` as a timedelta, for a first firing put off until later."""
    if isinstance(value, timedelta):
        return value
    text = str(value or "").strip()
    seconds = (lambda d: d.total_seconds() if d else None)(parse_span(text))
    if not seconds or seconds <= 0:
        raise ScheduleError(
            "Read %r as no length of time. Write it as 12h, 7d, 2w." % (value,))
    return timedelta(seconds=seconds)


#: `RunOnlyIfNetworkAvailable` stays false on purpose: it is unreliable over
#: SMB and skips firings, and `watch --once` already exits 2 for a source it
#: could not reach - which Windows records as the task's Last Result. A poll
#: that says "unreachable" is worth more than one that never happened.
_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>%(description)s</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>%(start)s</StartBoundary>
      <Enabled>true</Enabled>%(repetition)s
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">%(principal)s
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>%(wake)s</WakeToRun>
    <ExecutionTimeLimit>%(limit)s</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(command)s</Command>
      <Arguments>%(arguments)s</Arguments>
    </Exec>
  </Actions>
</Task>
"""


# ---------------------------------------------------------------------------
# composing the commands
# ---------------------------------------------------------------------------

def compose(job, *, name, xml_path, every="hourly", modifier=None,
            logged_out=True, account=None, replace=False, **_ignored):
    """Build the ``schtasks /create`` arguments without changing Windows.

    ``cli_args`` is validated here as well as in `definition`, so a caller that
    only ever composes still gets told it handed over something other than a
    one-pass watch.
    """
    runner_command(job)
    interval_of(every, modifier)
    command = ["schtasks", "/create", "/tn", task_name(name),
               "/xml", str(xml_path)]
    if logged_out:
        # No /rp. schtasks asks at the console and gives the answer straight to
        # Windows, so the credential is never an argument to anything.
        command += ["/ru", str(account or default_account())]
    if replace:
        command.append("/f")
    return command


def render(command: Sequence[str]):
    """Return a composed command as one copyable Windows line."""
    return subprocess.list2cmdline(list(command))


# ---------------------------------------------------------------------------
# talking to Windows
# ---------------------------------------------------------------------------

def _run(command, prompts=False):
    """Run a schtasks command, capturing unless it has to ask a human.

    A command that prompts under captured pipes reads end-of-file and fails
    without the question ever reaching a screen - `setup.apply_plan` learned
    that from `New-LocalUser`, and it is the same here.
    """
    try:
        if prompts:
            done = subprocess.run(list(command), timeout=300)
            return done.returncode, "", ""
        done = subprocess.run(list(command), capture_output=True, text=True,
                              timeout=120)
    except FileNotFoundError as exc:
        raise ScheduleError(
            "Windows Task Scheduler is not available on this machine.") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScheduleError(
            "The scheduled download could not be created: %s" % exc) from exc
    return done.returncode, done.stdout or "", done.stderr or ""


def _read_back(name, run=None):
    """The registered task's own definition, as text. None if there is none."""
    code, out, _err = (run or _run)(
        ["schtasks", "/query", "/tn", task_name(name), "/xml"])
    return out if code == 0 and out.strip() else None


def _value(xml, tag):
    found = re.search(r"<%s>(.*?)</%s>" % (tag, tag), xml or "",
                      re.IGNORECASE | re.DOTALL)
    return found.group(1).strip() if found else None


def verify(name, *, logged_out=True, wake=False, run=None):
    """Re-ask Windows what it actually registered, and raise if it differs.

    "It was created" is not "it will run": every one of these five is a way for
    a task that reported success to sit there collecting nothing, and four of
    them do it silently.
    """
    xml = _read_back(name, run=run)
    if xml is None:
        raise ScheduleError(
            "Windows reported the schedule created, but %s is not there."
            % task_name(name))
    want = expected_settings(logged_out=logged_out, wake=wake)
    read = {tag: (_value(xml, tag) or _ABSENT_MEANS.get(tag))
            for tag in _VERIFIED}
    wrong = {tag: (read[tag], want[tag]) for tag in _VERIFIED
             if (read[tag] or "").lower() != want[tag].lower()}
    if wrong:
        raise ScheduleError(
            "The schedule was created but Windows did not take %d of its "
            "settings, so it would not run when you expect:\n%s"
            % (len(wrong), "\n".join(
                "  %s is %s, should be %s" % (tag, got or "absent", expected)
                for tag, (got, expected) in sorted(wrong.items()))))
    return read


#: Which keyword belongs to which builder. Taken from the signatures, the way
#: `cli._owner_of` asks the parser which subcommand owns a flag, rather than
#: kept by hand - a list maintained by hand is one that goes stale silently.
def _keywords(function, *, without=()):
    """The keywords a builder really accepts, `**kwargs` excluded.

    `compose` tolerates the settings meant for `definition` so a dry run can
    hand it the whole dict; counting that `**_ignored` as a keyword would make
    "which builder owns this?" answer "both".
    """
    return frozenset(
        name for name, p in _signature(function).parameters.items()
        if p.kind is not _Parameter.VAR_KEYWORD and name not in without)


_DEFINITION_KEYS = _keywords(definition, without=("job",))
_COMPOSE_KEYS = _keywords(compose, without=("job", "name", "xml_path"))


def plan(job, *, name, xml_path="<the definition>", **settings):
    """Everything a schedule IS, without changing Windows.

    One place splits the settings between the two builders. Doing it at each
    call site is how `replace` reached `definition` twice - a TypeError that
    appears only when a real schedule is made, so a dry run that composed
    happily still crashed the moment somebody meant it.
    """
    unknown = set(settings) - _DEFINITION_KEYS - _COMPOSE_KEYS
    if unknown:
        raise ScheduleError(
            "Unknown scheduling setting(s): %s" % ", ".join(sorted(unknown)))
    # Rendered ONCE. A `Job` builds its argv on demand, and asking it three
    # times would let a recipe that resolves differently between calls put one
    # command in the definition and another in what the caller is shown.
    cli_args = argv_of(job)
    executable, arguments = runner_command(
        cli_args, **{k: v for k, v in settings.items()
                     if k in ("runner", "frozen")})
    return {
        "task": task_name(name),
        "command": compose(cli_args, name=name, xml_path=xml_path,
                           **{k: v for k, v in settings.items()
                              if k in _COMPOSE_KEYS}),
        "definition": definition(cli_args,
                                 **{k: v for k, v in settings.items()
                                    if k in _DEFINITION_KEYS}),
        # What Windows will actually start, as one line. It is the only record
        # of what a schedule collects, so it is shown before anything is made
        # and emitted afterwards.
        "runs": render([executable, *arguments]),
    }


def register(job, *, name, run=None, verify_after=True, **settings):
    """Create the scheduled task, then check Windows really took it.

    Returns the task name, the exact command, and the settings read back off
    the registered task - so a caller can show that the five that decide
    whether it survives a power-off are the five Windows now holds.
    """
    logged_out = bool(settings.get("logged_out", True))
    wake = bool(settings.get("wake", False))
    # Rendered once here too, and the same list is used for the definition and
    # for the composed command - see `plan`.
    cli_args = argv_of(job)
    intended = plan(cli_args, name=name, **settings)
    xml = intended["definition"]
    # `delete=False` and a hand-written removal: Windows cannot open a file a
    # second time while NamedTemporaryFile holds it.
    handle = tempfile.NamedTemporaryFile(
        suffix=".xml", prefix="pyincucyte-schedule-", delete=False)
    path = Path(handle.name)
    try:
        handle.close()
        # UTF-16 with a BOM, which is what the declaration says and what
        # Task Scheduler exports; an XML file whose bytes disagree with its own
        # declaration is refused with a message about the file, not the task.
        path.write_text(xml, encoding="utf-16")
        command = plan(cli_args, name=name, xml_path=path,
                       **settings)["command"]
        code, out, err = (run or _run)(command, prompts=logged_out)
        if code != 0:
            raise ScheduleError(
                (err or out
                 or "Task Scheduler exited %d" % code).strip()
                + "\nThe command was: " + render(command))
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    read = (verify(name, logged_out=logged_out, wake=wake, run=run)
            if verify_after else {})
    return {
        "task": task_name(name),
        "command": command,
        "output": (out or "").strip(),
        "settings": read,
        "logged_out": logged_out,
    }


def tasks(*, run=None):
    """Every scheduled download this package owns, and how each one is doing.

    Last Result is the answer to "has it actually been running?", which is the
    whole question a schedule exists to make answerable while nobody watches:
    0 wrote something, 1 nothing was due, 2 the source could not be reached.
    """
    code, out, _err = (run or _run)(["schtasks", "/query", "/fo", "csv", "/v"])
    if code != 0:
        return []
    import csv
    import io as _io

    found = []
    for row in csv.DictReader(_io.StringIO(out)):
        name = (row.get("TaskName") or "").strip()
        if TASK_PREFIX not in name:
            continue
        found.append({
            "task": name.lstrip("\\"),
            "next_run": (row.get("Next Run Time") or "").strip(),
            "last_run": (row.get("Last Run Time") or "").strip(),
            "last_result": (row.get("Last Result") or "").strip(),
            "status": (row.get("Status") or "").strip(),
            "run_as": (row.get("Run As User") or "").strip(),
        })
    return found


def remove(name, *, run=None):
    """Delete one owned scheduled download.

    Through `task_name`, so this can only ever delete a task this package
    named - a mistyped name removes nothing rather than something else's job.
    """
    code, out, err = (run or _run)(
        ["schtasks", "/delete", "/tn", task_name(name), "/f"])
    if code != 0:
        raise ScheduleError((err or out
                             or "Task Scheduler exited %d" % code).strip())
    return task_name(name)


def run_now(name, *, run=None):
    """Fire an owned schedule once, so its first run is not hours away."""
    code, out, err = (run or _run)(
        ["schtasks", "/run", "/tn", task_name(name)])
    if code != 0:
        raise ScheduleError((err or out
                             or "Task Scheduler exited %d" % code).strip())
    return task_name(name)


__all__ = ["CADENCES", "DEFAULT_CADENCE", "DEFAULT_TIME_LIMIT", "PROMPTS",
           "TASK_PREFIX", "ScheduleError", "cadence_of", "compose",
           "default_account",
           "definition", "expected_settings", "interval_of", "plan",
           "register",
           "remove", "render", "run_now", "runner_command", "self_command",
           "task_name",
           "tasks", "verify"]
