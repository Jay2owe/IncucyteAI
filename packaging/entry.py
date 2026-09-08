"""Frozen entry point for the PyIncucyte desktop app.

PyInstaller runs its entry script as ``__main__`` with no parent package, so
freezing a module that uses relative imports fails on its first line. This shim
imports the package properly and calls into it.
"""

import sys


def run(argv=None):
    """Open the window, or run a scheduled CLI pass from the frozen app."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--scheduled-cli"]:
        from pyincucyte.cli import main as cli_main
        return cli_main(argv[1:])
    from pyincucyte.gui import main as gui_main
    return gui_main()

if __name__ == "__main__":
    raise SystemExit(run())
