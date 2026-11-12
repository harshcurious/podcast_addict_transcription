#!/usr/bin/env python3
"""Durable downstream stage: AI-clean a transcript, then retain it in Hindsight.

A pending job is written before work begins. Transcription remains successful if either external
AI or Hindsight is unavailable; the pending job is retried on service startup and after each new
transcript. The stage fails closed: an uncleaned transcript is never sent to Hindsight.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys

import hindsight_save
from clean_transcript_ai import CleanerConfig, clean, hermes_ask, is_reusable, write_outputs
from durable_queue import DurableQueue
from transcript import Transcript

ROOT = pathlib.Path(os.environ.get("PA_ROOT", "~/podcast-transcripts")).expanduser()
PENDING = ROOT / "postprocess-pending"
RECEIPTS = ROOT / "postprocess-receipts"
LOG_PATH = ROOT / "logs" / "postprocess.log"


def log(message):
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


QUEUE = DurableQueue(PENDING, RECEIPTS, log)


def queue(transcript_json):
    source = pathlib.Path(transcript_json).resolve()
    data = json.loads(source.read_text(encoding="utf-8"))
    sha = str(data.get("meta", {}).get("audio_sha256") or
              hashlib.sha256(str(source).encode()).hexdigest())
    envelope = {"queued_at": dt.datetime.now().isoformat(timespec="seconds"),
                "audio_sha256": sha, "transcript_json": str(source)}
    path = QUEUE.enqueue(f"{sha}.json", envelope)
    log(f"queued audio={sha[:12]} transcript={source}")
    return path


def process(job, *, ask_model=None, post=None):
    """Handle one pending job. Returns its Receipt, or None to keep it pending.

    `ask_model` and `post` default to the Hermes CLI and a real HTTP POST.
    Fails closed: if cleaning does not succeed, nothing is sent to Hindsight.
    """
    name = job["audio_sha256"][:12]
    source = pathlib.Path(job["transcript_json"])
    if not source.exists():
        log(f"ERROR transcript missing; kept pending: {source}")
        return None
    config = CleanerConfig.from_env()
    transcript = Transcript.load(source)

    if not is_reusable(transcript, config):
        try:
            result = clean(transcript, ask_model or hermes_ask(config), config)
            write_outputs(transcript, result, config)
        except Exception as exc:
            log(f"AI cleaning failed; kept pending audio={name}: {exc}")
            return None

    if not hindsight_save.retain(transcript.clean_path, transcript.json_path,
                                 post=post or hindsight_save.http_post):
        log(f"Hindsight submission failed; kept pending audio={name}")
        return None

    log(f"complete audio={name} clean={transcript.clean_path}")
    return {**job, "completed_at": dt.datetime.now().isoformat(timespec="seconds"),
            "clean_transcript": str(transcript.clean_path),
            "ads_manifest": str(transcript.ads_path),
            "hindsight_bank": hindsight_save.BANK_ID}


def flush(*, ask_model=None, post=None):
    total = len(QUEUE.pending())
    pending = QUEUE.drain(lambda job: process(job, ask_model=ask_model, post=post))
    log(f"flush complete processed={total - pending} pending={pending}")
    return 1 if pending else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript_json", nargs="?")
    ap.add_argument("--flush", action="store_true")
    args = ap.parse_args()
    if args.transcript_json:
        queue(args.transcript_json)
    if args.transcript_json or args.flush:
        return flush()
    ap.error("provide TRANSCRIPT_JSON or --flush")


if __name__ == "__main__":
    sys.exit(main())
