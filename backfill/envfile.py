"""Load `backfill/config.env` into the environment. Standard library only.

Entry points call this before importing the pipeline modules, which read PA_* at import.
"""
import os
import pathlib

DEFAULT = pathlib.Path(__file__).resolve().parent / "config.env"


def load(path=DEFAULT):
    """KEY=VALUE lines; variables already set in the environment win."""
    path = pathlib.Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
