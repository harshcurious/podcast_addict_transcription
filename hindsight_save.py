#!/usr/bin/env python3
"""Queue completed podcast transcripts for durable retention in local Hindsight.

The transcript is explicitly labelled as external source material. A stable document_id
based on the audio SHA-256 makes retries idempotent; a content-derived operation UUID
makes an identical async submission idempotent while allowing a revised transcript to
replace the same Hindsight document.

Pending requests are persisted before network I/O. Accepted requests move to receipts/;
Hindsight then owns durable processing and retries in its database.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
import uuid

from durable_queue import DurableQueue
from transcript import Transcript, is_clean_path, json_path_for

ROOT = pathlib.Path(os.environ.get("PA_ROOT", "~/podcast-transcripts")).expanduser()
API_URL = os.environ.get("PA_HINDSIGHT_URL", "http://127.0.0.1:8888").rstrip("/")
BANK_ID = os.environ.get("PA_HINDSIGHT_BANK", "podcast-addict")
TIMEOUT = int(os.environ.get("PA_HINDSIGHT_TIMEOUT", "30"))
PENDING = ROOT / "hindsight-pending"
RECEIPTS = ROOT / "hindsight-receipts"
LOG_PATH = ROOT / "logs" / "hindsight.log"


def log(message):
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def load_transcript(md_path, json_path=None):
    md_path = pathlib.Path(md_path).resolve()
    content = md_path.read_text(encoding="utf-8")
    meta = {}
    sidecar = pathlib.Path(json_path).resolve() if json_path else json_path_for(md_path)
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            meta = data.get("meta", data) if isinstance(data, dict) else {}
        except Exception as exc:
            log(f"WARNING could not parse sidecar {sidecar}: {exc}")

    audio_sha = str(meta.get("audio_sha256") or "").strip()
    content_sha = hashlib.sha256(content.encode()).hexdigest()
    source_key = audio_sha or content_sha
    document_id = f"podcast:{source_key}"
    operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                  f"hindsight://{BANK_ID}/{document_id}/{content_sha}"))

    title = str(meta.get("title") or md_path.stem)
    show = str(meta.get("show") or "")
    provenance = (
        "EXTERNAL SOURCE MATERIAL — AUTOMATIC PODCAST TRANSCRIPT.\n"
        "The following words were spoken by podcast participants and transcribed by ASR. "
        "They are not statements by the user or Hermes. Preserve claims as attributed, "
        "potentially fallible external-source claims; do not convert them into user facts.\n\n"
    )
    context = (
        f"Externally sourced podcast transcript. Show: {show or 'unknown'}. "
        f"Episode/title: {title}. ASR model: {meta.get('model', 'unknown')}. "
        "Speakers are podcast participants, not the user and not Hermes."
    )
    metadata = {
        "source": "podcast_transcript",
        "source_app": str(meta.get("source_app") or "Podcast Addict"),
        "source_device": str(meta.get("source_device") or ""),
        "source_path": str(meta.get("source_path") or ""),
        "title": title,
        "show": show,
        "description": str(meta.get("description") or "")[:12000],
        "publisher_artist": str(meta.get("publisher_artist") or ""),
        "publisher_date": str(meta.get("publisher_date") or ""),
        "source_file": str(meta.get("source_file") or ""),
        "audio_sha256": audio_sha,
        "file_bytes": str(meta.get("file_bytes") or ""),
        "audio_duration_seconds": str(meta.get("audio_duration") or ""),
        "received_at": str(meta.get("received_at") or ""),
        "completed_at": str(meta.get("completed_at") or ""),
        "published_at": str(meta.get("published_at") or ""),
        "feed_url": str(meta.get("feed_url") or ""),
        "episode_url": str(meta.get("episode_url") or ""),
        "transcript_path": str(md_path),
        "asr_model": str(meta.get("model") or ""),
        "language": str(meta.get("language") or ""),
        "transcribed_at": str(meta.get("transcribed_at") or ""),
    }
    ads_path = Transcript(sidecar).ads_path
    if is_clean_path(md_path) and ads_path.exists():
        try:
            ads = json.loads(ads_path.read_text(encoding="utf-8"))
            metadata.update({
                "transcript_cleaner": f"{ads.get('provider', '')}/{ads.get('model', '')}".strip("/"),
                "ads_segments_removed": str(ads.get("segments_removed", "")),
                "ads_seconds_removed": str(ads.get("seconds_removed", "")),
                "ads_manifest_path": str(ads_path),
            })
        except Exception as exc:
            log(f"WARNING could not parse ad manifest {ads_path}: {exc}")
    return {
        "items": [{
            "content": provenance + content,
            "context": context,
            "timestamp": "unset",
            "document_id": document_id,
            "update_mode": "replace",
            "tags": ["source:podcast", "source:podcast-addict"],
            "observation_scopes": "shared",
            "metadata": metadata,
        }],
        "async": True,
        "operation_id": operation_id,
    }, document_id, operation_id, content_sha


QUEUE = DurableQueue(PENDING, RECEIPTS, log)


def pending_name(document_id):
    return hashlib.sha256(document_id.encode()).hexdigest()[:24] + ".json"


def queue(md_path, json_path=None):
    payload, document_id, operation_id, content_sha = load_transcript(md_path, json_path)
    envelope = {
        "queued_at": dt.datetime.now().isoformat(timespec="seconds"),
        "document_id": document_id,
        "operation_id": operation_id,
        "content_sha256": content_sha,
        "payload": payload,
    }
    path = QUEUE.enqueue(pending_name(document_id), envelope)
    log(f"queued document={document_id} operation={operation_id} pending={path.name}")
    return path


def http_post(url, body, timeout):
    """POST JSON bytes; returns (status, response text). The only network call here."""
    request = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def submit(envelope, post=http_post):
    """Send one pending retain request. Returns its Receipt, or None to keep it pending."""
    body = json.dumps(envelope["payload"], ensure_ascii=False).encode("utf-8")
    url = f"{API_URL}/v1/default/banks/{BANK_ID}/memories"
    try:
        status, raw = post(url, body, TIMEOUT)
        result = json.loads(raw)
        if status != 200 or not result.get("success"):
            raise RuntimeError(f"unexpected response {status}: {raw[:500]}")
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        log(f"ERROR retain submission kept pending document={envelope['document_id']}: {exc}")
        return None

    log(f"accepted document={envelope['document_id']} operation="
        f"{result.get('operation_id') or result.get('operation_ids')}")
    return {
        "accepted_at": dt.datetime.now().isoformat(timespec="seconds"),
        "document_id": envelope["document_id"],
        "content_sha256": envelope["content_sha256"],
        "request_operation_id": envelope["operation_id"],
        "response": result,
    }


def retain(md_path, json_path=None, *, post=http_post):
    """Queue one transcript for Retention and try to deliver it (and anything older).
    True when this transcript was accepted; False leaves it queued for a later flush."""
    path = queue(md_path, json_path)
    flush(post)
    return not path.exists()


def flush(post=http_post):
    total = len(QUEUE.pending())
    pending = QUEUE.drain(lambda envelope: submit(envelope, post))
    log(f"flush complete submitted={total - pending} pending={pending}")
    return 1 if pending else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript", nargs="?")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--flush", action="store_true")
    args = ap.parse_args()
    if args.transcript:
        queue(args.transcript, args.json_path)
    if args.transcript or args.flush:
        return flush()
    ap.error("provide TRANSCRIPT or --flush")


if __name__ == "__main__":
    sys.exit(main())
