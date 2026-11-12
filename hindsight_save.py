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


def pending_path(document_id):
    key = hashlib.sha256(document_id.encode()).hexdigest()[:24]
    return PENDING / f"{key}.json"


def queue(md_path, json_path=None):
    payload, document_id, operation_id, content_sha = load_transcript(md_path, json_path)
    PENDING.mkdir(parents=True, exist_ok=True)
    path = pending_path(document_id)
    envelope = {
        "queued_at": dt.datetime.now().isoformat(timespec="seconds"),
        "document_id": document_id,
        "operation_id": operation_id,
        "content_sha256": content_sha,
        "payload": payload,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    log(f"queued document={document_id} operation={operation_id} pending={path.name}")
    return path


def submit(path):
    envelope = json.loads(path.read_text(encoding="utf-8"))
    body = json.dumps(envelope["payload"], ensure_ascii=False).encode("utf-8")
    url = f"{API_URL}/v1/default/banks/{BANK_ID}/memories"
    request = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read().decode("utf-8")
            result = json.loads(raw)
            if response.status != 200 or not result.get("success"):
                raise RuntimeError(f"unexpected response {response.status}: {raw[:500]}")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, RuntimeError) as exc:
        log(f"ERROR retain submission kept pending file={path.name}: {exc}")
        return False

    RECEIPTS.mkdir(parents=True, exist_ok=True)
    receipt = {
        "accepted_at": dt.datetime.now().isoformat(timespec="seconds"),
        "document_id": envelope["document_id"],
        "content_sha256": envelope["content_sha256"],
        "request_operation_id": envelope["operation_id"],
        "response": result,
    }
    destination = RECEIPTS / path.name
    tmp = destination.with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    os.replace(tmp, destination)
    path.unlink()
    log(f"accepted document={envelope['document_id']} operation="
        f"{result.get('operation_id') or result.get('operation_ids')}")
    return True


def flush():
    PENDING.mkdir(parents=True, exist_ok=True)
    paths = sorted(PENDING.glob("*.json"))
    failed = 0
    for path in paths:
        if not submit(path):
            failed += 1
    log(f"flush complete submitted={len(paths) - failed} pending={failed}")
    return 1 if failed else 0


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
