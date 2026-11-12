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
import subprocess
import sys

from clean_transcript_ai import CleanerConfig, is_reusable
from transcript import Transcript

ROOT = pathlib.Path(os.environ.get("PA_ROOT", "~/podcast-transcripts")).expanduser()
PYTHON = pathlib.Path(sys.executable)
PENDING = ROOT / "postprocess-pending"
RECEIPTS = ROOT / "postprocess-receipts"
LOG_PATH = ROOT / "logs" / "postprocess.log"


def log(message):
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def queue(transcript_json):
    source = pathlib.Path(transcript_json).resolve()
    data = json.loads(source.read_text(encoding="utf-8"))
    sha = str(data.get("meta", {}).get("audio_sha256") or
              hashlib.sha256(str(source).encode()).hexdigest())
    PENDING.mkdir(parents=True, exist_ok=True)
    path = PENDING / f"{sha}.json"
    envelope = {"queued_at": dt.datetime.now().isoformat(timespec="seconds"),
                "audio_sha256": sha, "transcript_json": str(source)}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    log(f"queued audio={sha[:12]} transcript={source}")
    return path


def run_logged(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stdout:
        log(proc.stdout.strip()[-1500:])
    if proc.returncode and proc.stderr:
        log("ERROR " + proc.stderr.strip()[-1500:])
    return proc.returncode


def process(path):
    job = json.loads(path.read_text(encoding="utf-8"))
    source = pathlib.Path(job["transcript_json"])
    if not source.exists():
        log(f"ERROR transcript missing; kept pending: {source}")
        return False
    paths = Transcript(source)
    clean_md, ads_json = paths.clean_path, paths.ads_path

    reusable = is_reusable(paths, CleanerConfig.from_env())
    if not reusable:
        rc = run_logged([str(PYTHON), str(ROOT / "clean_transcript_ai.py"), str(source)])
        if rc:
            log(f"AI cleaning failed rc={rc}; kept pending {path.name}")
            return False

    rc = run_logged([str(PYTHON), str(ROOT / "hindsight_save.py"), str(clean_md),
                     "--json", str(source)])
    if rc:
        log(f"Hindsight submission failed rc={rc}; kept pending {path.name}")
        return False

    RECEIPTS.mkdir(parents=True, exist_ok=True)
    receipt = {**job, "completed_at": dt.datetime.now().isoformat(timespec="seconds"),
               "clean_transcript": str(clean_md), "ads_manifest": str(ads_json),
               "hindsight_bank": os.environ.get("PA_HINDSIGHT_BANK", "podcast-addict")}
    dest = RECEIPTS / path.name
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    os.replace(tmp, dest)
    path.unlink()
    log(f"complete audio={job['audio_sha256'][:12]} clean={clean_md}")
    return True


def flush():
    PENDING.mkdir(parents=True, exist_ok=True)
    failed = 0
    for path in sorted(PENDING.glob("*.json")):
        if not process(path):
            failed += 1
    log(f"flush complete processed={len(list(PENDING.glob('*.json'))) if failed else 'all'} pending={failed}")
    return 1 if failed else 0


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
