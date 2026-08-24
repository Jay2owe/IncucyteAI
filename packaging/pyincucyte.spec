# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the PyIncucyte desktop app.

Build to a local directory, never into this checkout — the repository lives
inside Dropbox and the build set would be synchronised:

    $B = "$env:TEMP\\pyincucyte-build"
    pyinstaller packaging/pyincucyte.spec --noconfirm --distpath $B\\dist --workpath $B\\build

One-folder, not one-file: a one-file build unpacks the whole bundle to a
temporary directory on every launch and trips antivirus heuristics.

Differences from the Circadian Workbench recipe this was copied from:

* **Tkinter is the interface, so it must not be excluded.** The window is
  ``pyincucyte.gui.app``; excluding ``tkinter`` here would freeze an app with no
  way to draw itself.
* **pythonnet is needed, but the assembly it loads is not bundled.**
  ``engine.encrypt_password`` does ``import clr`` and then
  ``clr.AddReference("Essen")`` against whatever Incucyte install the machine
  already has. The bridge ships; the vendor DLL is discovered at runtime and
  stays the user's.
* **No data files read by path.** Unlike the workbench there is no ``static/``
  tree and no Alembic directory, so ``datas`` stays empty.

Settings live under ``%APPDATA%\\PyIncucyte`` via ``engine.default_app_dir``,
which falls through to that when no repository-relative ``.tmp`` exists — so a
frozen build lands in the right place without a code change.
"""

from pathlib import Path

APP_NAME = "PyIncucyte"
SPEC_DIR = Path(SPECPATH)
ROOT = SPEC_DIR.parent

hiddenimports = [
    # Loaded inside a function, so the import graph does not reach it.
    "clr",
    # Tk file dialogs and the themed widget set.
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.ttk",
]

# Verified unnecessary by importing pyincucyte and pyincucyte.gui.app with each
# name blocked at the import hook. Re-run that check after a dependency change.
excludes = [
    "matplotlib",
    "PyQt5", "PyQt6", "PySide2", "PySide6",
    "pandas", "scipy", "polars", "pyarrow", "fsspec",
    "botocore", "boto3", "openpyxl", "lxml", "numba", "llvmlite",
    "tensorflow", "onnxruntime", "torch", "sklearn", "cv2",
    "pytest", "IPython", "notebook", "jupyter",
]

a = Analysis(
    # entry.py, not gui/app.py: PyInstaller runs the entry script as ``__main__``
    # with no parent package, so relative imports in a frozen module fail.
    [str(SPEC_DIR / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-packed binaries are a common antivirus false positive
    console=False,      # no console window behind the app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(SPEC_DIR / "app.ico") if (SPEC_DIR / "app.ico").is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
