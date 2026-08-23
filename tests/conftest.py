"""Keep tests off the real device and out of the user's config folder."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

# Any accidental use of the default paths lands in a scratch folder.
os.environ.setdefault("PYINCUCYTE_HOME",
                      tempfile.mkdtemp(prefix="pyincucyte-tests-"))

# Registers the retired import names (incucyte_downloader and friends).
from pyincucyte import compat  # noqa: E402

compat.install()
