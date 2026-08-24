"""Frozen entry point for the PyIncucyte desktop app.

PyInstaller runs its entry script as ``__main__`` with no parent package, so
freezing a module that uses relative imports fails on its first line. This shim
imports the package properly and calls into it.
"""

from pyincucyte.gui import main

if __name__ == "__main__":
    main()
