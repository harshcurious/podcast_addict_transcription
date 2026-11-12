"""Episode intake: everything between an Upload arriving and its audio being transcribed.

Owns the staged/inbox/archive directories, the Sidecar files, the dedupe Ledger
(seen.json) and the state of each queued job. Callers never touch those files directly.

Standard library only: the HTTP server that imports this runs under the system Python.
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

AUDIO_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".mp4", ".m4b",
             ".wma", ".webm"}
_NOT_AUDIO = (".json", ".done", ".part", ".failed")


class UnsupportedAudio(ValueError):
    """The Upload's file name does not have an audio extension."""


@dataclass(frozen=True)
class Accepted:
    status: str  # "queued" | "staged_until_completion" | "duplicate"
    name: str
    show: str
    sha256: str
    existing: dict | None = None  # Ledger entry, for duplicates


@dataclass(frozen=True)
class Completed:
    status: str  # "queued_after_completion" | "already_queued" | "already_processed" | "not_found"
    name: str
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Job:
    name: str
    audio_path: str
    meta_path: str
    meta: dict


def safe_name(raw):
    name = os.path.basename(urllib.parse.unquote(raw or "")).strip()
    name = re.sub(r"[^\w\s.\-()&',\[\]+]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "episode.mp3"
    if not os.path.splitext(name)[1]:
        name += ".mp3"
    return name[:180]


def show_from_name(name):
    """Podcast Addict names files either '<Show> - <Episode>.mp3' or the remote filename."""
    stem = os.path.splitext(name)[0]
    m = re.match(r"^(.+?)\s+[-–]\s+.+$", stem)
    if m and len(m.group(1)) < 80:
        return m.group(1).strip()
    return ""


def show_from_path(phone_path):
    """Derive the show folder from Podcast Addict's .../podcast/<show>/<file> path."""
    if not phone_path:
        return ""
    normalized = urllib.parse.unquote(phone_path).replace("\\", "/").rstrip("/")
    parent = os.path.basename(os.path.dirname(normalized)).strip()
    return parent[:160] if parent.lower() not in ("", "podcast") else ""


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _read_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path, data, indent=None):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
    os.replace(tmp, path)


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


class Intake:
    def __init__(self, root, *, keep_audio=True, require_completed=False, now=_now):
        self.root = str(root)
        self.keep_audio = keep_audio
        self.require_completed = require_completed
        self._now = now
        self._inbox = os.path.join(self.root, "inbox")
        self._staged = os.path.join(self.root, "staged")
        self._archive = os.path.join(self.root, "archive")
        self._ledger = os.path.join(self.root, "seen.json")
        self._lock = threading.Lock()
        for d in (self._inbox, self._staged, self._archive):
            os.makedirs(d, exist_ok=True)

    # -- Uploads ---------------------------------------------------------------------

    def accept(self, payload, *, raw_name, show_hint="", phone_path="", original_name=None,
               fields=None):
        """Store one Upload. Returns Accepted; raises UnsupportedAudio for non-audio names.

        `fields` are extra Sidecar entries the caller extracted from the request
        (source_app, feed_url, ...). They are stored as given.
        """
        name = safe_name(raw_name or "episode.mp3")
        if os.path.splitext(name)[1].lower() not in AUDIO_EXT:
            raise UnsupportedAudio(name)
        show = (show_hint or "").strip() or show_from_path(phone_path) or show_from_name(name)
        digest = hashlib.sha256(payload).hexdigest()
        target = self._staged if self.require_completed else self._inbox
        dest = os.path.join(target, name)
        with self._lock:
            seen = _read_json(self._ledger)
            if digest in seen:
                return Accepted("duplicate", name, show, digest, seen[digest])
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                f.write(payload)
            meta = {"sha256": digest, "show": show, "received_at": self._now(),
                    "bytes": len(payload), "original_name": original_name or name,
                    "source_path": phone_path, **(fields or {})}
            # Sidecar first: the worker only looks for audio, so it never sees an
            # Episode whose Sidecar is missing.
            _write_json(dest + ".json", meta)
            os.replace(tmp, dest)
            seen[digest] = {"name": name, "at": meta["received_at"]}
            _write_json(self._ledger, seen, indent=1)
        status = "staged_until_completion" if self.require_completed else "queued"
        return Accepted(status, name, show, digest)

    def complete(self, raw_name, *, completed_at="", source=""):
        """Release a staged Episode to the queue once the phone reports it was played."""
        name = safe_name(raw_name)
        staged_audio = os.path.join(self._staged, name)
        inbox_audio = os.path.join(self._inbox, name)
        with self._lock:
            if not os.path.exists(staged_audio):
                if os.path.exists(inbox_audio):
                    return Completed("already_queued", name)
                if os.path.exists(os.path.join(self._archive, name)):
                    return Completed("already_processed", name)
                return Completed("not_found", name)
            meta = _read_json(staged_audio + ".json")
            meta["completed_at"] = completed_at.strip() or self._now()
            meta["completion_source"] = (source.strip() or "Tasker")[:100]
            _write_json(inbox_audio + ".json", meta, indent=2)
            os.replace(staged_audio, inbox_audio)
            _unlink(staged_audio + ".json")
        return Completed("queued_after_completion", name, meta)

    # -- Jobs ------------------------------------------------------------------------

    def next_job(self):
        """The oldest-named queued Episode, or None. Does not claim it: one worker only."""
        for name in sorted(os.listdir(self._inbox)):
            if name.endswith(_NOT_AUDIO) or os.path.splitext(name)[1].lower() not in AUDIO_EXT:
                continue
            path = os.path.join(self._inbox, name)
            return Job(name, path, path + ".json", _read_json(path + ".json"))
        return None

    def finish(self, job, ok):
        """Record the outcome of a job.

        Success archives (or deletes) the audio. Failure keeps the audio as `.failed` and
        releases its digest from the Ledger, so the same Episode can be uploaded again.
        """
        with self._lock:
            if ok:
                if self.keep_audio:
                    shutil.move(job.audio_path, os.path.join(self._archive, job.name))
                else:
                    _unlink(job.audio_path)
                _unlink(job.meta_path)
                return
            _unlink(job.meta_path)
            failed_path = job.audio_path + ".failed"
            os.replace(job.audio_path, failed_path)
            digest = job.meta.get("sha256") or _sha256_of(failed_path)
            seen = _read_json(self._ledger)
            if seen.pop(digest, None) is not None:
                _write_json(self._ledger, seen, indent=1)

    def status(self):
        def audio(directory, suffix=""):
            return sorted(f[:len(f) - len(suffix)] for f in os.listdir(directory)
                          if f.endswith(suffix)
                          and os.path.splitext(f[:len(f) - len(suffix)])[1].lower() in AUDIO_EXT)
        return {"queue": audio(self._inbox), "staged": audio(self._staged),
                "failed": audio(self._inbox, ".failed")}
