"""Isolate tests from any real runtime tree.

Project modules read PA_* at import, so the environment is fixed here, before any of them
is imported.
"""
import os
import pathlib
import sys
import tempfile

for _key in [k for k in os.environ if k.startswith("PA_")]:
    del os.environ[_key]
_ROOT = tempfile.mkdtemp(prefix="pa-test-")
os.environ["PA_ROOT"] = _ROOT
os.environ["PA_OUTDIR"] = os.path.join(_ROOT, "out")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
