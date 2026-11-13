#!/usr/bin/env python3
"""Backfill: a Clean transcript in Hindsight for every Episode listened to in Podcast Addict.

Usage (from the repository root, on the Mac):
    backfill/.venv/bin/python backfill/backfill.py BACKUP [--limit N] [--podcast TEXT]...
    backfill/.venv/bin/python backfill/backfill.py BACKUP --dry-run
    backfill/.venv/bin/python backfill/backfill.py --status
    backfill/.venv/bin/python backfill/backfill.py --flush

For each listened Episode in the backup, most recently played first:
  1. use the publisher's transcript if one is online (the backup's transcript URLs, then the
     feed's <podcast:transcript> tags); otherwise
  2. download the audio and transcribe it on this Mac (mlx-whisper, or faster-whisper);
then remove ad reads with the pipeline's AI classifier and retain the Clean transcript in
Hindsight. Transcription and cleaning/retention overlap: one thread each.

Every Episode has a state file in backfill-state/, so a run can stop at any point and the next
run carries on. Cleaning fails closed exactly as on the Pi: an uncleaned transcript is never
retained. The Pi pipeline's modules are imported, never modified.

Settings come from the environment, after `backfill/config.env` (see config.env.example).
Runtime tree under PA_ROOT (default ~/podcast-backfill):
    out/<show>/<date>-<slug>.{json,md,srt,clean.md,ads.json}   work/   logs/backfill.log
    backfill-state/<episode-key>.json   hindsight-pending/   hindsight-receipts/
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import queue
import re
import sys
import threading
import time
import uuid
from collections import Counter

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
LANG_CODES = {"english": "en", "hindi": "hi", "spanish": "es", "french": "fr", "german": "de",
              "italian": "it", "portuguese": "pt", "dutch": "nl", "japanese": "ja",
              "chinese": "zh", "korean": "ko", "russian": "ru", "arabic": "ar"}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("backup", nargs="?", help="Podcast Addict .backup file (or its .db)")
    ap.add_argument("--env-file", default=str(HERE / "config.env"))
    ap.add_argument("--dry-run", action="store_true",
                    help="look up online transcripts and report the plan; change nothing")
    ap.add_argument("--status", action="store_true", help="summarise progress so far")
    ap.add_argument("--flush", action="store_true",
                    help="finish transcribed Episodes and resend queued Hindsight requests")
    ap.add_argument("--limit", type=int, default=0, help="stop after N Episodes with new work")
    ap.add_argument("--podcast", action="append", default=[],
                    help="only podcasts whose name contains TEXT (repeatable)")
    ap.add_argument("--played-since", default="", help="only Episodes played on/after YYYY-MM-DD")
    ap.add_argument("--episode-id", type=int, action="append", default=[],
                    help="only this Podcast Addict episode _id (repeatable)")
    ap.add_argument("--online-only", action="store_true",
                    help="never transcribe locally; Episodes without a transcript wait")
    ap.add_argument("--no-online", action="store_true", help="skip the online lookup")
    ap.add_argument("--no-postprocess", action="store_true",
                    help="only produce raw transcripts; clean and retain later with --flush")
    ap.add_argument("--retry-failed", action="store_true",
                    help="retry Episodes that already failed --max-attempts times")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--force", action="store_true",
                    help="redo Episodes already retained (same Hindsight document is replaced)")
    ap.add_argument("--keep-audio", action="store_true", help="keep downloaded audio in work/")
    return ap.parse_args(argv)


sys.path[:0] = [str(HERE), str(REPO)]
ARGS = parse_args() if __name__ == "__main__" else None
if ARGS is not None:
    import envfile
    envfile.load(ARGS.env_file)
os.environ.setdefault("PA_ROOT", str(pathlib.Path("~/podcast-backfill").expanduser()))
os.environ.setdefault("PA_OUTDIR", str(pathlib.Path(os.environ["PA_ROOT"]) / "out"))

# The Pi pipeline's modules read PA_* at import, so they are imported only now.
import ai_backends  # noqa: E402
import hindsight_save  # noqa: E402
import publisher_transcripts as online  # noqa: E402
from clean_transcript_ai import (  # noqa: E402
    CleanerConfig,
    clean,
    is_reusable,
    write_outputs,
)
from pa_backup import ListenedEpisode, database_path, listened_episodes  # noqa: E402
from transcribe import plain_text, slugify  # noqa: E402
from transcript import Transcript  # noqa: E402

ROOT = pathlib.Path(os.environ["PA_ROOT"]).expanduser()
OUT = pathlib.Path(os.environ["PA_OUTDIR"]).expanduser()
WORK = ROOT / "work"
STATE_DIR = ROOT / "backfill-state"
LOG_PATH = ROOT / "logs" / "backfill.log"
MAX_AUDIO_BYTES = int(os.environ.get("PA_MAX_MB", "800")) * 1024 * 1024
MAX_CLEAN_FAILURES = int(os.environ.get("PA_BACKFILL_MAX_CLEAN_FAILURES", "5"))
HINDSIGHT_BACKOFF = 600
_log_lock = threading.Lock()


def log(message):
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {message}"
    with _log_lock:
        print(line, flush=True)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------------------ state

class State:
    """One JSON record per Episode: what has been done to it and where its files are.

    status: transcribed (raw Transcript exists) | retained (accepted by Hindsight)
            | failed (no transcript could be made) | waiting (--online-only, none online)
    """

    def __init__(self, directory):
        self.dir = pathlib.Path(directory)

    def get(self, key) -> dict:
        path = self.dir / f"{key}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return dict(key=key)

    def put(self, record):
        record["updated_at"] = now()
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{record['key']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def all(self):
        if not self.dir.exists():
            return []
        out = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        return out


def episode_from(record):
    data = dict(record["episode"])
    data["transcript_urls"] = tuple(tuple(x) for x in data.get("transcript_urls", ()))
    return ListenedEpisode(**data)


def episode_record(ep):
    data = {k: getattr(ep, k) for k in ListenedEpisode.__dataclass_fields__}
    data["transcript_urls"] = [list(x) for x in ep.transcript_urls]
    return data


# ------------------------------------------------------------------------- transcripts

def language_code(ep):
    if os.environ.get("PA_LANG"):
        return os.environ["PA_LANG"]
    lang = ep.language.strip().lower()
    return LANG_CODES.get(lang) or (lang if len(lang) == 2 else "")


def transcript_path(ep):
    """out/<show>/<published>-<title>.json, made unique if another Episode already has it."""
    folder = OUT / (slugify(ep.podcast) if ep.podcast else "unknown-show")
    base = f"{ep.published_at or 'undated'}-{slugify(ep.title)}"
    path = folder / f"{base}.json"
    if path.exists():
        try:
            other = json.loads(path.read_text(encoding="utf-8"))["meta"].get("episode_key")
        except (OSError, ValueError, KeyError):
            other = None
        if other != ep.key:
            path = folder / f"{base}-{ep.key[:8]}.json"
    return path


def base_meta(ep):
    return {
        "title": ep.title, "show": ep.podcast, "description": plain_text(ep.description),
        "publisher_artist": "", "publisher_date": ep.published_at,
        "source_file": pathlib.PurePosixPath(ep.audio_url.split("?")[0]).name,
        "source_path": "", "source_app": "Podcast Addict backup", "source_device": "",
        "received_at": "", "completed_at": ep.played_at, "published_at": ep.published_at,
        "feed_url": ep.feed_url, "episode_url": ep.episode_url or ep.audio_url,
        "episode_key": ep.key, "podcast_addict_episode_id": ep.episode_id,
        "played_at": ep.played_at,
    }


def from_publisher(ep, found):
    segments = found["segments"]
    meta = {**base_meta(ep),
            "file_bytes": None, "audio_duration": ep.duration_seconds or segments[-1]["end"],
            "model": f"publisher transcript ({found['format']})", "language": language_code(ep),
            "transcribed_at": now(), "audio_sha256": "", "segments": len(segments),
            "words": found["words"], "transcript_source": "publisher",
            "transcript_url": found["url"], "transcript_format": found["format"],
            "timing": found["timing"]}
    return meta, segments


def from_audio(ep, recognizer, keep_audio):
    if not ep.audio_url.startswith("http"):
        raise RuntimeError("Episode has no downloadable audio URL")
    from mac_asr import download
    audio, sha, size = download(ep.audio_url, WORK / "audio", ep.key, max_bytes=MAX_AUDIO_BYTES)
    language = language_code(ep)
    try:
        engine, model = recognizer.describe(language)
        log(f"  transcribing {size / 2**20:.0f} MB ({language or 'language unknown'}) "
            f"with {engine} {model}")
        segments, info = recognizer.transcribe(audio, language)
    finally:
        if not keep_audio:
            audio.unlink(missing_ok=True)
    words = sum(len(s["text"].split()) for s in segments)
    if words < online.MIN_WORDS:
        raise RuntimeError(f"speech recognition produced only {words} words")
    meta = {**base_meta(ep),
            "file_bytes": size, "audio_duration": info["duration"], "model": info["model"],
            "language": info["language"], "transcribed_at": now(), "rtf": info["rtf"],
            "wall_seconds": info["wall_seconds"], "audio_sha256": sha,
            "segments": len(segments), "words": words, "transcript_source": "asr",
            "asr_backend": info["engine"], "timing": "exact"}
    return meta, segments


def obtain(ep, record, feeds, recognizer, opts):
    """Make the raw Transcript for `ep`. Updates and returns `record`."""
    found, reason = None, "online lookup disabled"
    if not opts.no_online:
        try:
            found = online.find(ep, feeds, log=log)
        except online.NoTranscript as exc:
            reason = str(exc)
    if found:
        meta, segments = from_publisher(ep, found)
        log(f"  publisher transcript ({found['format']}, {found['words']} words) {found['url']}")
    elif opts.online_only:
        record.update(status="waiting", error=f"no online transcript: {reason}"[:500])
        return record
    else:
        meta, segments = from_audio(ep, recognizer(), opts.keep_audio)
        log(f"  transcribed {meta['words']} words, {meta['rtf']}x realtime")
    path = transcript_path(ep)
    Transcript(path, meta, segments).save_raw()
    with (OUT / "index.jsonl").open("a") as f:
        f.write(json.dumps({"md": str(path.with_suffix(".md")), **meta}) + "\n")
    record.update(status="transcribed", transcript_json=str(path), error="",
                  source=meta["transcript_source"], transcript_model=meta["model"],
                  transcribed_at=meta["transcribed_at"])
    return record


# ---------------------------------------------------------------- cleaning + retention

def cleaner_backend(config):
    """(name, ask_model) for the configured AI cleaner; see ai_backends."""
    return ai_backends.choose(config, WORK)


def queue_retain(transcript, record):
    """Queue the Clean transcript for Retention under a document ID stable per Episode."""
    payload, _, _, content_sha = hindsight_save.load_transcript(transcript.clean_path,
                                                                transcript.json_path)
    meta = transcript.meta
    document_id = f"podcast:episode:{meta['episode_key']}"
    operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hindsight://{hindsight_save.BANK_ID}/"
                                                      f"{document_id}/{content_sha}"))
    item = payload["items"][0]
    item["document_id"] = document_id
    item["tags"] = [*item["tags"], "source:backfill"]
    item["metadata"].update({
        "transcript_source": str(meta.get("transcript_source") or ""),
        "transcript_url": str(meta.get("transcript_url") or ""),
        "timing": str(meta.get("timing") or ""),
        "played_at": str(meta.get("played_at") or ""),
        "episode_key": str(meta["episode_key"]),
        "podcast_addict_episode_id": str(meta.get("podcast_addict_episode_id") or ""),
    })
    if meta.get("transcript_source") == "publisher":
        item["content"] = item["content"].replace(
            "and transcribed by ASR", "and transcribed by the podcast's publisher", 1)
    payload["operation_id"] = operation_id
    name = hindsight_save.pending_name(document_id)
    hindsight_save.QUEUE.enqueue(name, {
        "queued_at": now(), "document_id": document_id, "operation_id": operation_id,
        "content_sha256": content_sha, "payload": payload})
    record["hindsight_pending"] = name
    record["document_id"] = document_id
    return name


def reconcile(state):
    """Mark Episodes retained once their queued Hindsight request has a Receipt."""
    for record in state.all():
        name = record.get("hindsight_pending")
        if (record.get("status") == "transcribed" and name
                and (hindsight_save.QUEUE.receipts_dir / name).exists()
                and not (hindsight_save.QUEUE.pending_dir / name).exists()):
            record.update(status="retained", retained_at=now(), error="")
            state.put(record)


class Finisher:
    """Cleans and retains Transcripts on a worker thread while transcription continues."""

    def __init__(self, state, *, ask_model=None, post=None):
        self.state, self.post = state, post or hindsight_save.http_post
        self.config = CleanerConfig.from_env()
        self._ask = ask_model
        self.jobs = queue.Queue()
        self.stop = threading.Event()
        self.clean_failures = 0
        self.cleaning_disabled = False
        self.hindsight_retry_at = 0.0
        self.counts = Counter()
        self.thread = threading.Thread(target=self._run, name="finisher", daemon=True)

    def ask(self):
        if self._ask is None:
            backend, self._ask = cleaner_backend(self.config)
            log(f"ad cleaner: {backend} {self.config.provider}/{','.join(self.config.models)}")
        return self._ask

    def start(self):
        self.thread.start()
        return self

    def submit(self, record):
        self.jobs.put(record)

    def close(self):
        """Finish everything queued, then deliver what Hindsight has not accepted yet."""
        self.jobs.put(None)
        self.thread.join()
        if not self.stop.is_set():
            hindsight_save.flush(self.post)
        reconcile(self.state)

    def _run(self):
        while not self.stop.is_set():
            record = self.jobs.get()
            if record is None:
                return
            try:
                self.finish(record)
            except Exception as exc:  # one Episode must not stop the rest
                log(f"ERROR finishing {record.get('key')}: {exc!r}")

    def finish(self, record):
        transcript = Transcript.load(record["transcript_json"])
        label = f"{transcript.meta.get('show')} — {transcript.meta.get('title')}"
        if not is_reusable(transcript, self.config):
            if self.cleaning_disabled:
                return
            try:
                result = clean(transcript, self.ask(), self.config)
                write_outputs(transcript, result, self.config)
                self.clean_failures = 0
                log(f"  cleaned {label}: removed {len(result.ranges)} ad interval(s)")
            except Exception as exc:
                self.clean_failures += 1
                record["error"] = f"ad cleaning failed: {exc}"[:500]
                self.state.put(record)
                self.counts["clean_failed"] += 1
                log(f"ERROR ad cleaning failed; not retained: {label}: {exc}")
                if self.clean_failures >= MAX_CLEAN_FAILURES:
                    self.cleaning_disabled = True
                    log(f"ERROR {self.clean_failures} cleaning failures in a row; no more "
                        "cleaning this run. Transcription continues; finish later with --flush.")
                return
        self.counts["cleaned"] += 1
        name = queue_retain(transcript, record)
        self.state.put(record)
        if time.time() < self.hindsight_retry_at:
            return  # Hindsight was unreachable recently; it stays queued for the final flush
        hindsight_save.flush(self.post)
        if (hindsight_save.QUEUE.pending_dir / name).exists():
            self.hindsight_retry_at = time.time() + HINDSIGHT_BACKOFF
            log(f"Hindsight unavailable; queued and retrying in {HINDSIGHT_BACKOFF // 60} min")
            return
        record.update(status="retained", retained_at=now(), error="")
        self.state.put(record)
        self.counts["retained"] += 1


# ------------------------------------------------------------------------- selection

def already_in_hindsight():
    """(show, title) pairs the Pi pipeline already retained, to avoid a second copy."""
    seen, offset = set(), 0
    try:
        while True:
            url = (f"{hindsight_save.API_URL}/v1/default/banks/{hindsight_save.BANK_ID}"
                   f"/documents?limit=100&offset={offset}")
            body, _ = online.http_get(url, timeout=hindsight_save.TIMEOUT)
            page = json.loads(body)
            for doc in page.get("items", []):
                meta = (doc.get("retain_params") or {}).get("metadata") or {}
                if doc.get("id", "").startswith("podcast:") and \
                        not doc["id"].startswith("podcast:episode:"):
                    seen.add((online._norm(meta.get("show", "")),
                              online._norm(meta.get("title", ""))))
            offset += len(page.get("items", []))
            if not page.get("items") or offset >= page.get("total", 0):
                return seen
    except Exception as exc:
        log(f"WARNING could not list Hindsight documents ({exc}); no Pi duplicates skipped")
        return seen


def select(episodes, opts):
    wanted = [p.lower() for p in opts.podcast]
    for ep in episodes:
        if wanted and not any(w in ep.podcast.lower() for w in wanted):
            continue
        if opts.played_since and ep.played_at[:10] < opts.played_since:
            continue
        if opts.episode_id and ep.episode_id not in opts.episode_id:
            continue
        yield ep


def needs_work(record, opts):
    status = record.get("status")
    if opts.force or status is None:
        return True
    if status == "retained" or status == "pi-pipeline":
        return False
    if status == "failed":
        return opts.retry_failed or record.get("attempts", 0) < opts.max_attempts
    if status == "waiting":
        return not opts.online_only or opts.retry_failed
    return True  # transcribed: still to be cleaned/retained


# ----------------------------------------------------------------------------- modes

def run(opts):
    db = database_path(opts.backup, WORK)
    episodes = list(select(listened_episodes(db), opts))
    log(f"{len(episodes)} listened Episode(s) selected from {pathlib.Path(opts.backup).name}")
    state = State(STATE_DIR)
    feeds = None if opts.no_online else online.FeedIndex(WORK / "feeds", log=log)
    pi_done = set() if opts.force or opts.dry_run else already_in_hindsight()
    recognizer_box = []

    def recognizer():
        if not recognizer_box:
            from mac_asr import Recognizer
            recognizer_box.append(Recognizer(os.environ.get("PA_ASR_BACKEND", "auto"),
                                              beam=int(os.environ.get("PA_BEAM", "1"))))
        return recognizer_box[0]

    if opts.dry_run:
        return dry_run(episodes, state, feeds, opts)

    finisher = None if opts.no_postprocess else Finisher(state).start()
    counts, started = Counter(), 0
    try:
        for ep in episodes:
            record = state.get(ep.key)
            if not needs_work(record, opts):
                counts["skipped"] += 1
                continue
            record["episode"] = episode_record(ep)
            if (online._norm(ep.podcast), online._norm(ep.title)) in pi_done:
                record.update(status="pi-pipeline", error="")
                state.put(record)
                counts["already_from_pi"] += 1
                continue
            if opts.limit and started >= opts.limit:
                break
            started += 1
            log(f"[{started}] {ep.podcast} — {ep.title} (played {ep.played_at[:10]})")
            have = record.get("transcript_json") and pathlib.Path(
                record["transcript_json"]).exists()
            if opts.force or not have:
                try:
                    obtain(ep, record, feeds, recognizer, opts)
                except Exception as exc:
                    record.update(status="failed", error=str(exc)[:500],
                                  attempts=record.get("attempts", 0) + 1)
                    log(f"ERROR no transcript: {exc}")
                state.put(record)
                if record["status"] != "transcribed":
                    counts[record["status"]] += 1
                    continue
                counts[f"transcript_{record['source']}"] += 1
            if finisher:
                finisher.submit(record)
    except KeyboardInterrupt:
        log("interrupted; finishing the Episode being cleaned, the rest resume next run")
        if finisher:
            finisher.stop.set()
    finally:
        if finisher:
            finisher.close()
            counts.update(finisher.counts)
    log("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def dry_run(episodes, state, feeds, opts):
    todo = [ep for ep in episodes if needs_work(state.get(ep.key), opts)]
    with_links, asr_hours = 0, 0.0
    for ep in todo[:opts.limit or None]:
        links = online.candidates(ep, feeds) if feeds else []
        if links:
            with_links += 1
        else:
            asr_hours += ep.duration_seconds / 3600
        print(f"{'online' if links else 'asr   '}  {ep.played_at[:10]}  {ep.podcast} — "
              f"{ep.title}" + (f"  [{links[0][1] or links[0][0]}]" if links else ""))
    shown = len(todo[:opts.limit or None])
    print(f"\n{len(episodes)} listened, {len(episodes) - len(todo)} already done, "
          f"{shown} planned: {with_links} with an online transcript link, "
          f"{shown - with_links} to transcribe locally (~{asr_hours:.0f} h of audio)")
    return 0


def status():
    reconcile(State(STATE_DIR))
    records = State(STATE_DIR).all()
    by_status = Counter(r.get("status") for r in records)
    by_source = Counter(r.get("source") for r in records if r.get("source"))
    print(f"runtime tree: {ROOT}")
    print("episodes: " + (", ".join(f"{k}={v}" for k, v in by_status.most_common()) or "none"))
    print("transcript source: " + ", ".join(f"{k}={v}" for k, v in by_source.most_common()))
    print(f"Hindsight requests still queued: {len(hindsight_save.QUEUE.pending())}")
    errors = Counter(re.sub(r"https?://\S+", "<url>", r.get("error", ""))[:120]
                     for r in records if r.get("error"))
    if errors:
        print("most common errors:")
        for error, n in errors.most_common(8):
            print(f"  {n:5d}  {error}")
    return 0


def flush():
    state = State(STATE_DIR)
    finisher = Finisher(state)
    for record in state.all():
        if record.get("status") == "transcribed" and record.get("transcript_json"):
            finisher.finish(record)
    hindsight_save.flush(finisher.post)
    reconcile(state)
    status()
    return 0


def main(opts):
    if opts.status:
        return status()
    if opts.flush:
        return flush()
    if not opts.backup:
        print("error: BACKUP is required (or use --status / --flush)", file=sys.stderr)
        return 2
    try:
        database_path(opts.backup, WORK)  # check the backup before any other work
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return run(opts)


if __name__ == "__main__":
    sys.exit(main(ARGS))
