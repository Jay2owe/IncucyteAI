"""Old import names, kept alive from inside the package.

Before 0.3 the project shipped two loose modules at the top of the repository,
``incucyte_downloader`` and ``incucyte_gui``, plus a second distribution
package called ``py_incucyte_gui``.  Everything now lives under
:mod:`pyincucyte`, and a public package has no business claiming names that
generic on the import path.

Importing :mod:`pyincucyte` registers the old names as aliases instead, so a
scheduled task or Fiji macro that still does::

    import pyincucyte                       # or anything else that pulls it in
    from incucyte_downloader import download_scan_images

keeps working.  The alias *is* the real module - the same object under two
names - so monkeypatching one patches the other, exactly as the old shim did.

Aliases resolve lazily through a meta path finder, so ``incucyte_gui`` costs
nothing until something actually asks for it and Tk is loaded.
"""

import importlib
import sys
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec

#: Retired top-level name -> where that code lives now.
ALIASES = {
    "incucyte_downloader": "pyincucyte.engine",
    "incucyte_gui": "pyincucyte.gui",
    "py_incucyte_gui": "pyincucyte",
}


def _resolve(target):
    module = importlib.import_module(target)
    if target == "pyincucyte.engine" and not hasattr(module, "main"):
        # The command line used to live in incucyte_downloader itself.
        from . import cli
        module.main = cli.main
    return module


class _AliasLoader(Loader):
    def __init__(self, target):
        self.target = target

    def create_module(self, spec):
        return _resolve(self.target)

    def exec_module(self, module):
        """Nothing to run - the module it aliases is already imported."""


class _AliasFinder(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if path is not None:            # a submodule of something else
            return None
        moved_to = ALIASES.get(name)
        if moved_to is None:
            return None
        return ModuleSpec(name, _AliasLoader(moved_to))


def install():
    """Make the retired names importable.  Safe to call more than once."""
    if not any(isinstance(finder, _AliasFinder) for finder in sys.meta_path):
        sys.meta_path.append(_AliasFinder())
