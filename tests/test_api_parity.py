"""Every verb the command line has, and the Python that does the same job.

The rule has always said user-visible behaviour must be reachable from both
front ends. Only one half of it could ever fail a test: a missing subcommand
breaks the parser tests, and a missing `IncucyteClient` method broke nothing
anywhere - so the API half was not wrong when it was skipped, it was invisible.
That is how `schedule` came to ship as a subcommand with no `client.schedule`
until somebody read the code and noticed.

The mechanism is one table. Every verb is named here beside the Python that
does the same job, and a verb with no Python answer has to be written into
`NO_API` with a reason. Adding a subcommand and forgetting the API therefore
fails a test rather than passing quietly.

`NO_API` is the part that rots: keep it short, keep the reasons real, and treat
a growing list as the sign the rule is being worked around rather than obeyed.
The sister package PyLV200 carries the identical file, house rule 9.
"""
from __future__ import annotations

import importlib

import pytest

from pyincucyte import cli

#: Verb -> the dotted name that does the same job from Python. An
#: `IncucyteClient` method wherever the verb talks to the instrument, which is
#: nearly all of them; a module function where it reads a file this package
#: wrote.
API = {
    # the instrument itself
    "probe": "pyincucyte.IncucyteClient.probe",
    "login": "pyincucyte.IncucyteClient.login",
    "logout": "pyincucyte.IncucyteClient.logout",
    "status": "pyincucyte.IncucyteClient.device_state",
    "scan-now": "pyincucyte.IncucyteClient.begin_scan",
    "unmix": "pyincucyte.IncucyteClient.save_unmix",
    # what is there
    "vessels": "pyincucyte.IncucyteClient.vessels",
    "find": "pyincucyte.IncucyteClient.find_vessels",
    "scans": "pyincucyte.IncucyteClient.scan_times_between",
    "plan": "pyincucyte.IncucyteClient.plan",
    "protocol": "pyincucyte.IncucyteClient.protocol",
    "preview": "pyincucyte.IncucyteClient.preview",
    "preview-probe": "pyincucyte.IncucyteClient.probe_preview_tiles",
    "timeline": "pyincucyte.IncucyteClient.timeline",
    # getting it
    "download": "pyincucyte.IncucyteClient.download",
    "watch": "pyincucyte.IncucyteClient.watch",
    "schedule": "pyincucyte.IncucyteClient.schedule",
    # what was written, and what to do again
    "manifest": "pyincucyte.load_manifest",
    "preset": "pyincucyte.ExportOptions.load",
}

#: Verbs with no Python counterpart, and why each is allowed not to have one.
#: One entry, and it should stay that way.
NO_API = {
    "gui": "it is a third front end, not a capability. The window builds argv "
           "and calls the CLI, so everything it can do is already in this "
           "table under the verb it builds.",
}


def _resolve(dotted):
    """The object a dotted name points at, importing as much of it as needed.

    A walk rather than `import pyincucyte; getattr(...)`, because a submodule
    nothing imports at package level is not an attribute until it has been
    imported.
    """
    parts = dotted.split(".")
    module, index = None, 0
    for index in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:index]))
            break
        except ImportError:
            continue
    assert module is not None, "nothing importable in %r" % dotted
    found = module
    for attribute in parts[index:]:
        found = getattr(found, attribute, None)
        assert found is not None, "%s has no %s" % (dotted, attribute)
    return found


def test_every_verb_names_the_python_that_does_the_same_job():
    """The gate. A new subcommand with no line in either table fails here."""
    verbs = set(cli.COMMANDS)
    covered = set(API) | set(NO_API)
    missing = verbs - covered
    assert not missing, (
        "no Python counterpart is named for %s. Add the method (or the "
        "function), name it in API, or write it into NO_API with the reason it "
        "does not need one." % ", ".join(sorted(missing)))
    assert not covered - verbs, (
        "%s is in the parity table and is not a verb any more"
        % ", ".join(sorted(covered - verbs)))


def test_no_verb_is_both_provided_and_exempt():
    assert not set(API) & set(NO_API)


@pytest.mark.parametrize("verb", sorted(API))
def test_the_named_python_exists_and_is_callable(verb):
    assert callable(_resolve(API[verb])), "%s is not callable" % API[verb]


@pytest.mark.parametrize("verb", sorted(NO_API))
def test_an_exemption_says_why(verb):
    """A reason, not a placeholder. Whoever reads this in a year is deciding
    whether the exemption still holds, and 'n/a' cannot be argued with."""
    assert len(NO_API[verb].split()) >= 8, verb


def _verbs_naming_a_vessel():
    """Subcommands whose first argument is a plate.

    Those act on instrument data, and instrument data is what `IncucyteClient`
    is for - so their Python answer belongs on it rather than in a loose
    function somewhere.
    """
    subs = [action for action in cli.build_parser()._actions
            if hasattr(action, "choices") and isinstance(action.choices, dict)]
    return {name for name, parser in (subs[0].choices if subs else {}).items()
            if any(a.dest == "name" for a in parser._actions
                   if not a.option_strings)}


@pytest.mark.parametrize("verb", sorted(_verbs_naming_a_vessel() - set(NO_API)))
def test_a_verb_that_names_a_vessel_is_on_the_client(verb):
    assert API[verb].startswith("pyincucyte.IncucyteClient."), (
        "%s names a vessel, so its Python counterpart belongs on "
        "IncucyteClient, not at %s" % (verb, API[verb]))


def test_the_client_answers_every_verb_that_names_it():
    """A rename that kept the CLI working and moved a method is invisible to
    every other test here; this is the one that notices."""
    from pyincucyte import IncucyteClient

    for verb, dotted in sorted(API.items()):
        if dotted.startswith("pyincucyte.IncucyteClient."):
            attribute = dotted.rsplit(".", 1)[1]
            assert hasattr(IncucyteClient, attribute), (verb, attribute)
