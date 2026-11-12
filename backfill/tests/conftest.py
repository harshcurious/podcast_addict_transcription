"""Isolate backfill tests from any real runtime tree, config.env or network.

The pipeline modules read PA_* at import, so the environment is fixed here first. When the
repository suite runs in the same session its conftest has already done this; keep its root.
"""
import os
import pathlib
import sys
import tempfile

if "PA_ROOT" not in os.environ or not os.environ["PA_ROOT"].startswith(tempfile.gettempdir()):
    for _key in [k for k in os.environ if k.startswith("PA_")]:
        del os.environ[_key]
    _ROOT = tempfile.mkdtemp(prefix="pa-backfill-test-")
    os.environ["PA_ROOT"] = _ROOT
    os.environ["PA_OUTDIR"] = os.path.join(_ROOT, "out")

_HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
