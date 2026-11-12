"""A crash-safe retry queue kept as one JSON file per job.

    pending/<name>.json   work that has not been accepted downstream yet
    receipts/<name>.json  proof that it was; written when a handler succeeds

A job is on disk before any work starts and stays there until its Receipt is written, so
a crash at any point leaves it to be retried by the next drain. Standard library only.
"""
import json
import os
import pathlib


class DurableQueue:
    def __init__(self, pending_dir, receipts_dir, log=print):
        self.pending_dir = pathlib.Path(pending_dir)
        self.receipts_dir = pathlib.Path(receipts_dir)
        self._log = log

    def enqueue(self, filename, envelope):
        """Persist a job. A job enqueued again under the same filename replaces the old one."""
        return self._write(self.pending_dir / filename, envelope)

    def pending(self):
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        return sorted(self.pending_dir.glob("*.json"))

    def drain(self, handler):
        """Offer every pending job to `handler(envelope)`.

        The handler returns a Receipt dict when the job is done, or None to leave it
        pending. An exception counts as None and does not stop the drain.
        Returns how many jobs are still pending.
        """
        still_pending = 0
        for path in self.pending():
            try:
                receipt = handler(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:
                self._log(f"ERROR job {path.name} kept pending: {exc!r}")
                receipt = None
            if receipt is None:
                still_pending += 1
                continue
            self._write(self.receipts_dir / path.name, receipt)
            path.unlink()
        return still_pending

    @staticmethod
    def _write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return path
