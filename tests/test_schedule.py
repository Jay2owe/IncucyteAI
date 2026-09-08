"""A schedule that survives the computer being turned off.

`watch` collects while a process is running. This is the same job with no
process: Windows starts one `watch --once`, it decides whether a chunk is due,
and it exits. Nothing is held between firings, which is what makes it safe -
`batch_after` measures from each frame's own acquisition time and
`batch_frames` counts the source against the ledger, so a poll after a reboot
decides exactly what the poll before it would have decided.

What is NOT free is the task. Measured on a real Windows 11 machine, a task
created from the plain `schtasks` flags comes back `InteractiveToken` with
`StartWhenAvailable` off and both battery guards on - so it runs only while
somebody is logged on, never catches up a firing missed while the machine was
off, and refuses to start unplugged. Four of those five fail silently: the
schedule sits there collecting nothing and says so nowhere. Every assertion
about the definition below is guarding one of them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pyincucyte.errors import IncucyteError
from pyincucyte.options import ExportOptions
from pyincucyte.schedule import (CADENCES, DEFAULT_CADENCE, Job, ScheduleError,
                              cadence_of, compose, default_account,
                              definition, expected_settings, interval_of,
                              plan, register, remove, runner_command,
                              self_command, task_name, tasks, verify)


ARGV = ["pyincucyte", "--host", "10.0.0.1", "watch", "-v", "38",
        "-o", "D:/images", "--batch-frames", "6"]


def _fake_run(answers=None, record=None):
    """A stand-in for the schtasks caller, recording what it was asked."""
    answers = list(answers or [])

    def run(command, prompts=False):
        if record is not None:
            record.append({"command": list(command), "prompts": prompts})
        return answers.pop(0) if answers else (0, "SUCCESS", "")
    return run


def _registered_xml(*, logon="Password", wake="false", available="true"):
    """What `schtasks /query /xml` hands back for a registered task."""
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task><Principals><Principal>
  <LogonType>%s</LogonType>
</Principal></Principals><Settings>
  <StartWhenAvailable>%s</StartWhenAvailable>
  <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
  <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
  <WakeToRun>%s</WakeToRun>
</Settings></Task>""" % (logon, available, wake)


# ---------------------------------------------------------------------------
# the five settings that decide whether it survives a power-off
# ---------------------------------------------------------------------------

def test_the_definition_sets_every_default_that_would_stop_it_running():
    """Each of these is a way for a created task to collect nothing quietly."""
    xml = definition(ARGV, every="hourly", modifier=6, account="labuser",
                     runner="C:/Python/python.exe", frozen=False)
    # Runs on a rebooted, locked machine rather than waiting for a logon.
    assert "<LogonType>Password</LogonType>" in xml
    assert "<UserId>labuser</UserId>" in xml
    # Catches up a firing missed while the computer was off.
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    # Neither refuses to start on battery nor is killed when the power goes.
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml
    # A slow poll must not have a second one started on top of it.
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    # Bounded, or one hung poll silences the schedule for ever.
    assert "<ExecutionTimeLimit>PT12H</ExecutionTimeLimit>" in xml


def test_at_logon_is_the_only_thing_that_changes_when_it_is_asked_for():
    xml = definition(ARGV, logged_out=False, account="labuser")
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml


def test_waking_a_sleeping_computer_is_opt_in_and_reaches_the_definition():
    assert "<WakeToRun>false</WakeToRun>" in definition(ARGV, account="a")
    assert "<WakeToRun>true</WakeToRun>" in definition(ARGV, account="a",
                                                       wake=True)


def test_expected_settings_is_what_the_definition_actually_says():
    """One table, or `verify` passes a task the definition never asked for."""
    for logged_out in (True, False):
        for wake in (True, False):
            xml = definition(ARGV, account="a", logged_out=logged_out,
                             wake=wake)
            for tag, value in expected_settings(logged_out=logged_out,
                                                wake=wake).items():
                assert "<%s>%s</%s>" % (tag, value, tag) in xml


# ---------------------------------------------------------------------------
# it was created is not it will run
# ---------------------------------------------------------------------------

def test_verify_accepts_a_task_windows_really_took():
    read = verify("run7", run=_fake_run([(0, _registered_xml(), "")]))
    assert read["LogonType"] == "Password"
    assert read["StartWhenAvailable"] == "true"


def test_verify_names_the_setting_windows_did_not_take():
    """A silently dropped setting is the whole failure mode here."""
    run = _fake_run([(0, _registered_xml(available="false"), "")])
    with pytest.raises(ScheduleError) as caught:
        verify("run7", run=run)
    assert "StartWhenAvailable" in str(caught.value)


def test_verify_catches_a_task_that_would_wait_for_a_logon():
    run = _fake_run([(0, _registered_xml(logon="InteractiveToken"), "")])
    with pytest.raises(ScheduleError) as caught:
        verify("run7", run=run)
    assert "LogonType" in str(caught.value)


def test_verify_says_so_when_the_task_is_not_there_at_all():
    with pytest.raises(ScheduleError) as caught:
        verify("run7", run=_fake_run([(1, "", "ERROR: cannot find")]))
    assert "not there" in str(caught.value)


def test_register_verifies_rather_than_trusting_the_exit_code():
    """schtasks reporting success is not the same as a task that will run."""
    run = _fake_run([(0, "SUCCESS", ""),
                     (0, _registered_xml(available="false"), "")])
    with pytest.raises(ScheduleError):
        register(ARGV, name="run7", account="labuser", run=run)


# ---------------------------------------------------------------------------
# the credential
# ---------------------------------------------------------------------------

def test_no_credential_ever_reaches_a_command_line():
    """schtasks asks for it itself and hands it straight to Windows.

    House rule 11 with a second front door: an argument would be in the
    process list, in a shell history and in this package's own transcript.
    """
    seen = []
    register(ARGV, name="run7", account="labuser",
             run=_fake_run([(0, "SUCCESS", ""),
                            (0, _registered_xml(), "")], record=seen))
    created = seen[0]["command"]
    assert "/rp" not in created
    assert created[created.index("/ru") + 1] == "labuser"
    assert not any("/rp" in str(part) for part in created)


def test_the_asking_command_gets_a_console_and_nothing_captured():
    """Captured pipes turn the question into an end-of-file and a failure.

    The same trap `setup.apply_plan` carries for `New-LocalUser`: under
    captured pipes the prompt is never seen and the command fails for a reason
    nothing on screen explains.
    """
    seen = []
    register(ARGV, name="run7", account="labuser",
             run=_fake_run([(0, "SUCCESS", ""),
                            (0, _registered_xml(), "")], record=seen))
    assert seen[0]["prompts"] is True
    # Reading the task back asks nobody anything, so it stays captured.
    assert seen[1]["prompts"] is False


def test_an_at_logon_schedule_asks_for_nothing():
    seen = []
    register(ARGV, name="run7", logged_out=False, account="labuser",
             run=_fake_run([(0, "SUCCESS", ""),
                            (0, _registered_xml(logon="InteractiveToken"), "")],
                           record=seen))
    assert seen[0]["prompts"] is False
    assert "/ru" not in seen[0]["command"]


def test_a_logged_out_schedule_with_no_account_to_run_as_refuses(monkeypatch):
    """Better to ask than to register a task under nobody in particular."""
    monkeypatch.delenv("USERNAME", raising=False)
    with pytest.raises(ScheduleError) as caught:
        definition(ARGV)
    assert "--account" in str(caught.value)
    # At-logon needs no account, so it is not asked for one.
    assert definition(ARGV, logged_out=False)


def test_the_definition_file_is_removed_even_when_windows_refuses():
    """It is a temporary file with a task definition in it, not a leftover."""
    seen = []
    with pytest.raises(ScheduleError):
        register(ARGV, name="run7", account="labuser",
                 run=_fake_run([(1, "", "ERROR: access is denied")],
                               record=seen))
    path = seen[0]["command"][seen[0]["command"].index("/xml") + 1]
    from pathlib import Path
    assert not Path(path).exists()


# ---------------------------------------------------------------------------
# what gets scheduled
# ---------------------------------------------------------------------------

def test_a_schedule_is_always_one_poll():
    """A task that polled for ever would still be running at the next firing,
    and `IgnoreNew` would skip that one without a word."""
    _executable, arguments = runner_command(ARGV, frozen=False)
    assert arguments[-1] == "--once"
    # Already there, and not doubled.
    _e, again = runner_command([*ARGV, "--once"], frozen=False)
    assert again.count("--once") == 1


def test_an_explicit_runner_replaces_the_interpreter_and_nothing_else():
    """Dropping `-m pyincucyte` with it makes a command that runs the interpreter
    on a file called "watch" - failing on every firing, hours later."""
    executable, arguments = runner_command(ARGV, runner="C:/Py/python.exe",
                                           frozen=False)
    assert executable == "C:/Py/python.exe"
    assert arguments[:2] == ["-m", "pyincucyte"]


def test_only_a_watch_can_be_scheduled():
    for wrong in (["pyincucyte", "download", "-v", "38"], ["pyincucyte"],
                  ["watch"]):
        with pytest.raises(ScheduleError):
            runner_command(wrong)


def test_a_frozen_build_schedules_itself_rather_than_an_interpreter():
    executable, arguments = runner_command(ARGV, runner="C:/App/PyIncucyte.exe",
                                           frozen=True)
    assert executable == "C:/App/PyIncucyte.exe"
    assert arguments[0] == "--scheduled-cli"
    assert "-m" not in arguments


def test_the_window_needs_a_console_to_be_asked_a_question_in(monkeypatch):
    """A pythonw.exe given CREATE_NEW_CONSOLE still has no standard input, so
    the prompt would appear and immediately read end-of-file."""
    import sys as _sys
    from pathlib import Path
    monkeypatch.setattr(_sys, "executable", r"C:\Py\pythonw.exe")
    monkeypatch.setattr(Path, "exists", lambda self: True)
    assert self_command(["pyincucyte", "schedule", "a:b"],
                        console=True)[0] == r"C:\Py\python.exe"
    assert self_command(["pyincucyte", "schedule", "a:b"])[0] == \
        r"C:\Py\pythonw.exe"


# ---------------------------------------------------------------------------
# the cadence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec,expected,repetition", [
    ("10m", ("minute", 10), "PT10M"),
    ("30m", ("minute", 30), "PT30M"),
    ("1h", ("hourly", 1), "PT1H"),
    ("6h", ("hourly", 6), "PT6H"),
    ("1d", ("daily", None), None),
])
def test_a_cadence_is_written_the_way_every_other_period_here_is(
        spec, expected, repetition):
    assert cadence_of(spec) == expected
    assert interval_of(*expected) == repetition


@pytest.mark.parametrize("spec", ["90m", "36h", "0h", "banana", "", "-1h"])
def test_a_cadence_windows_cannot_keep_is_refused_rather_than_rounded(spec):
    with pytest.raises(ScheduleError):
        cadence_of(spec)


def test_every_cadence_the_window_offers_is_one_the_command_line_takes():
    """The window passes `--every 6h` to the same parser a person types at.
    Two vocabularies for one idea is how the two front ends start disagreeing
    about how often a nine-day run is collected."""
    for label, spec in CADENCES.items():
        assert cadence_of(spec), label
    assert DEFAULT_CADENCE in CADENCES


# ---------------------------------------------------------------------------
# owning what it created
# ---------------------------------------------------------------------------

def test_every_task_is_named_so_it_can_be_found_again():
    assert task_name("run7").startswith("PyIncucyte scheduled download")
    # Idempotent: a name read back off a listing is not prefixed twice.
    assert task_name(task_name("run7")) == task_name("run7")


def test_removing_goes_through_the_prefix_so_it_cannot_delete_anything_else():
    seen = []
    remove("run7", run=_fake_run(record=seen))
    deleted = seen[0]["command"][seen[0]["command"].index("/tn") + 1]
    assert deleted == task_name("run7")


def test_a_listing_is_scoped_to_what_this_package_created():
    csv = ('"TaskName","Next Run Time","Status","Last Run Time",'
           '"Last Result","Run As User"\n'
           '"\\PyIncucyte scheduled download - run7","06/09/2026 23:00:00",'
           '"Ready","06/09/2026 22:00:00","1","Owner"\n'
           '"\\Somebody Else\'s Backup","06/09/2026 23:00:00","Ready",'
           '"06/09/2026 22:00:00","0","Owner"\n')
    found = tasks(run=_fake_run([(0, csv, "")]))
    assert [one["task"] for one in found] == \
        ["PyIncucyte scheduled download - run7"]
    # Last Result is the answer to "has it been running while I was away?"
    assert found[0]["last_result"] == "1"


def test_the_account_defaults_to_this_user(monkeypatch):
    """The credential for the share is in THIS user's store, put there by
    `pyincucyte login`, so any other account cannot reach the microscope."""
    monkeypatch.setenv("USERNAME", "labuser")
    assert default_account() == "labuser"


def test_compose_refuses_before_windows_is_asked_anything():
    with pytest.raises(IncucyteError):
        compose(["pyincucyte", "download", "-v", "38"], name="n",
                xml_path="t.xml")


def test_registering_a_schedule_does_not_start_a_download_on_the_spot():
    """`StartWhenAvailable` treats a boundary in the past as a missed firing.

    Measured on a real machine with the boundary rounded DOWN to the minute:
    the task was already `Running` by the time registration returned, which
    turns "set up a schedule" into "start collecting a nine-day run now".
    """
    from datetime import datetime
    import re as _re
    xml = definition(ARGV, account="labuser")
    boundary = _re.search(r"<StartBoundary>(.*?)</StartBoundary>", xml).group(1)
    assert datetime.fromisoformat(boundary) > datetime.now()


def test_a_setting_meant_for_one_builder_is_never_handed_to_the_other():
    """`replace` belongs to the command and `wake` to the definition.

    Forwarding the whole dict to both is a TypeError that appears only when a
    real schedule is made - on the machine, never in a test that composes.
    """
    seen = []
    register(ARGV, name="run7", account="labuser", every="minute", modifier=30,
             wake=True, replace=True, logged_out=False,
             run=_fake_run([(0, "SUCCESS", ""),
                            (0, _registered_xml(logon="InteractiveToken",
                                                wake="true"), "")],
                           record=seen))
    assert "/f" in seen[0]["command"]


def test_a_scheduling_setting_nobody_defined_is_refused_by_name():
    with pytest.raises(ScheduleError) as caught:
        register(ARGV, name="run7", account="a", cadence="6h",
                 run=_fake_run())
    assert "cadence" in str(caught.value)


def test_an_unexpected_crash_does_not_look_like_a_quiet_poll():
    """Exit 1 is this package's "nothing was due" - a completely normal poll.

    A scheduled task records the exit code and nothing else, so a schedule
    crashing on every firing and one with nothing to collect would read
    identically in the only column anybody looks at.
    """
    from pyincucyte import cli

    def explode(_args):
        raise ZeroDivisionError("something nobody predicted")

    original = cli.COMMANDS["vessels"]
    cli.COMMANDS["vessels"] = explode
    try:
        assert cli.main(["vessels"]) == 2
    finally:
        cli.COMMANDS["vessels"] = original


def test_a_setting_windows_left_out_is_read_as_its_own_default():
    """Windows omits a tag whose value IS the schema default.

    Measured here: a task asked for `WakeToRun` false comes back with no
    `WakeToRun` at all, and one asked for true carries it. Treating absent as
    a mismatch made `verify` reject a task Windows had taken perfectly - the
    same family as "an unknown answer is not a no", arriving from the other
    side.
    """
    minimal = """<?xml version="1.0"?>
<Task><Principals><Principal>
  <LogonType>InteractiveToken</LogonType>
</Principal></Principals><Settings>
  <StartWhenAvailable>true</StartWhenAvailable>
  <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
  <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
</Settings></Task>"""
    read = verify("run7", logged_out=False,
                  run=_fake_run([(0, minimal, "")]))
    assert read["WakeToRun"] == "false"
    # And an absent one is still caught when it was ASKED for.
    with pytest.raises(ScheduleError) as caught:
        verify("run7", logged_out=False, wake=True,
               run=_fake_run([(0, minimal, "")]))
    assert "WakeToRun" in str(caught.value)


def test_the_schedule_the_window_builds_is_one_the_command_line_parses():
    """The window has no schedule of its own: it builds argv and hands it to
    the real parser. `parse_args` takes argv WITHOUT the program name and
    `self_command` takes it WITH one - two readings of one list, and handing
    either the other's is a button that does nothing."""
    from pyincucyte import cli as cli_mod
    from pyincucyte.options import ExportOptions

    options = ExportOptions(vessels=(38,), output="D:/run")
    argv = [*options.cli_args("schedule"),
            "--name", "v38", "--every", "6h", "--wake"]

    parsed = cli_mod.parse_args(argv[1:])
    assert parsed.command == "schedule"
    assert parsed.every == "6h"
    assert parsed.wake is True

    assert self_command(argv, console=True)[-len(argv) + 1:] == argv[1:]


def test_the_first_check_can_be_put_off_until_a_chunk_could_exist():
    """A week of acquisition left alone: nothing polled, nothing fetched, and
    no column of "nothing was due" to read back through afterwards."""
    from datetime import datetime, timedelta
    import re as _re
    xml = definition(ARGV, account="labuser", start_after="7d")
    boundary = _re.search(r"<StartBoundary>(.*?)</StartBoundary>",
                          xml).group(1)
    assert datetime.fromisoformat(boundary) > datetime.now() + timedelta(
        days=6, hours=23)
    with pytest.raises(ScheduleError):
        definition(ARGV, account="labuser", start_after="next tuesday")


# ==========================================================================
# the recipe is the interface; the command line is one rendering of it
# ==========================================================================

def test_a_job_is_a_recipe_and_renders_to_the_command_a_person_would_type():
    """The inversion. `schedule.py` took an argv list first, which made the
    command line the real interface and every other front end a thing that had
    to spell its request in flags."""
    job = Job(ExportOptions(host="10.0.0.1", vessels=[38], output="D:/images",
                            batch_frames=6))
    argv = job.argv()
    assert argv[0] == "pyincucyte"
    assert "watch" in argv
    assert argv[-1] == "--once"
    assert job.label() == "38"


def test_a_job_and_the_argv_it_renders_to_build_the_same_task():
    """Both are accepted, and they must not be able to disagree - the argv
    path is what the desktop app still hands over."""
    job = Job(ExportOptions(host="10.0.0.1", vessels=[38], output="D:/images"))
    from_recipe = plan(job, name="38", account="labuser")
    from_argv = plan(job.argv(), name="38", account="labuser")
    assert from_recipe["runs"] == from_argv["runs"]
    assert from_recipe["command"] == from_argv["command"]


def test_a_recipe_with_no_output_is_refused_wherever_it_comes_from():
    """Windows starts a task in system32, so "write it here" means nothing.

    The check lived in `cli._schedulable`, which is why it only ever ran for
    the command line: a schedule made from Python could be registered with no
    output folder at all.
    """
    with pytest.raises(ScheduleError):
        Job(ExportOptions(host="10.0.0.1", vessels=[38])).argv()


def test_a_relative_output_is_made_absolute_before_windows_sees_it(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    options = Job(ExportOptions(host="10.0.0.1", vessels=[38],
                                output="images")).recipe()
    assert Path(options.output).is_absolute()
