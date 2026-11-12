#!/usr/bin/env python3
"""Use an AI endpoint to remove ad reads from a timestamped podcast transcript.

Only transcript text is sent to the endpoint; audio remains local. The original transcript
artifacts are untouched. Output is a cleaned Markdown derivative plus an auditable JSON manifest.
The classifier must return segment IDs, so the model cannot rewrite or summarize editorial text.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import time

from transcript import Transcript, render_markdown

ROOT = pathlib.Path(os.environ.get("PA_ROOT", "~/podcast-transcripts")).expanduser()
HERMES = os.environ.get("PA_CLEAN_HERMES", str(pathlib.Path.home() / ".local/bin/hermes"))
PROVIDER = os.environ.get("PA_CLEAN_PROVIDER", "opencode-go")
MODEL = os.environ.get("PA_CLEAN_MODEL", "deepseek-v4.1-flash-free")
FALLBACK_MODELS = [m.strip() for m in os.environ.get(
    "PA_CLEAN_FALLBACK_MODELS", "mimo-v2.6-flash-free,beam-free,mimo-v2.6-flash").split(",")
                   if m.strip()]
TIMEOUT = int(os.environ.get("PA_CLEAN_TIMEOUT", "240"))
MAX_CORE_CHARS = int(os.environ.get("PA_CLEAN_CHUNK_CHARS", "18000"))
CONTEXT_SECONDS = float(os.environ.get("PA_CLEAN_CONTEXT_SECONDS", "75"))
RETRIES = int(os.environ.get("PA_CLEAN_RETRIES", "2"))
LOG_PATH = ROOT / "logs" / "cleaner.log"


def log(message):
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def make_cores(segments):
    cores, start, chars = [], 0, 0
    for i, seg in enumerate(segments):
        size = len(seg.get("text", "")) + 40
        if i > start and chars + size > MAX_CORE_CHARS:
            cores.append((start, i))
            start, chars = i, 0
        chars += size
    if start < len(segments):
        cores.append((start, len(segments)))
    return cores


def build_prompt(segments, core_start, core_end):
    lo_time = max(0, segments[core_start]["start"] - CONTEXT_SECONDS)
    hi_time = segments[core_end - 1]["end"] + CONTEXT_SECONDS
    context_ids = [i for i, s in enumerate(segments)
                   if s["end"] > lo_time and s["start"] < hi_time]
    lines = [
        "You are a precise podcast transcript editor. Identify paid advertisements, sponsor reads,",
        "affiliate promotions, house ads, and commercial calls to action. Keep all editorial",
        "discussion, even when it discusses companies, products, advertising, investing, discounts,",
        "or commercial policy. Do not remove ordinary intros, outros, requests to subscribe to the",
        "podcast itself, or factual discussion unless it is clearly promotional.",
        "",
        "Classify ONLY segment IDs listed under CORE IDS. Surrounding segments are context only.",
        "An ad may begin as conversational banter before naming the product; include the complete ad",
        "read when context establishes that it is promotional. Return JSON only, exactly:",
        '{"ad_segment_ids":[1,2],"groups":[{"segment_ids":[1,2],"advertiser":"name",',
        '"reason":"brief evidence"}]}',
        "Use [] when there are no ads. Never include a segment ID outside CORE IDS.",
        "",
        "CORE IDS: " + ",".join(str(i) for i in range(core_start, core_end)),
        "TRANSCRIPT:",
    ]
    core = set(range(core_start, core_end))
    for i in context_ids:
        marker = "CORE" if i in core else "CONTEXT"
        s = segments[i]
        lines.append(f"[{marker} {i} {s['start']:.2f}-{s['end']:.2f}] {s['text']}")
    return "\n".join(lines)


def parse_json_output(raw):
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI response contained no JSON object")
    return json.loads(raw[start:end + 1])


def classify_chunk(prompt, core_start, core_end, chunk_no):
    work = ROOT / "work" / "ai-cleaner"
    work.mkdir(parents=True, exist_ok=True)
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    models = list(dict.fromkeys([MODEL, *FALLBACK_MODELS]))
    last_error = None
    for attempt in range(1, RETRIES + 2):
        for candidate_model in models:
            prompt_file = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt",
                                                 prefix=f"chunk-{chunk_no:03d}-", dir=work,
                                                 delete=False) as f:
                    f.write(prompt)
                    prompt_file = pathlib.Path(f.name)
                cmd = [HERMES, "chat", "--query-file", str(prompt_file), "--oneshot", "-Q",
                       "--provider", PROVIDER, "-m", candidate_model, "--reasoning", "none",
                       "--ignore-rules", "-t", "", "--max-turns", "1",
                       "--run-budget", str(max(30, TIMEOUT - 10)), "--source", "tool"]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
                if proc.returncode:
                    raise RuntimeError(
                        f"Hermes rc={proc.returncode}: {(proc.stderr or proc.stdout)[-800:]}")
                data = parse_json_output(proc.stdout)
                raw_ids = data.get("ad_segment_ids")
                if not isinstance(raw_ids, list):
                    raise ValueError("ad_segment_ids is not a list")
                ids = sorted(set(int(x) for x in raw_ids))
                invalid = [x for x in ids if x < core_start or x >= core_end]
                if invalid:
                    raise ValueError(f"response included non-core IDs: {invalid[:20]}")
                groups = data.get("groups") if isinstance(data.get("groups"), list) else []
                return {"core": [core_start, core_end], "ad_segment_ids": ids,
                        "groups": groups, "prompt_sha256": prompt_hash,
                        "attempt": attempt, "model": candidate_model}
            except Exception as exc:
                last_error = exc
                log(f"WARNING chunk={chunk_no} attempt={attempt} model={candidate_model} "
                    f"failed: {exc}")
            finally:
                if prompt_file:
                    try:
                        prompt_file.unlink()
                    except OSError:
                        pass
        if attempt <= RETRIES:
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"all cleaner models failed for chunk {chunk_no}: {last_error}")


def contiguous_ranges(ids, segments):
    if not ids:
        return []
    ranges, start, prev = [], ids[0], ids[0]
    for i in ids[1:]:
        if i == prev + 1:
            prev = i
            continue
        ranges.append({"start_index": start, "end_index": prev,
                       "start": segments[start]["start"], "end": segments[prev]["end"]})
        start = prev = i
    ranges.append({"start_index": start, "end_index": prev,
                   "start": segments[start]["start"], "end": segments[prev]["end"]})
    return ranges


def build_markdown(kept, meta, ranges):
    return render_markdown(
        kept, meta, heading_suffix=" — cleaned", show_duration=False,
        front_matter_extra=(("ai_cleaner", f"{PROVIDER}/{MODEL}"),
                            ("ad_intervals_removed", len(ranges))),
        notes=("AI-cleaned derivative; the raw transcript is preserved alongside this file.",))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_transcript")
    ap.add_argument("--out")
    ap.add_argument("--manifest")
    args = ap.parse_args()

    transcript = Transcript.load(args.json_transcript)
    source, segments, meta = transcript.json_path, transcript.segments, transcript.meta
    results = []
    for chunk_no, (start, end) in enumerate(make_cores(segments), 1):
        log(f"classifying chunk={chunk_no} core={start}:{end} via {PROVIDER}/{MODEL}")
        results.append(classify_chunk(build_prompt(segments, start, end), start, end, chunk_no))

    ad_ids = sorted(set(x for result in results for x in result["ad_segment_ids"]))
    ad_set = set(ad_ids)
    kept = [seg for i, seg in enumerate(segments) if i not in ad_set]
    ranges = contiguous_ranges(ad_ids, segments)
    out = pathlib.Path(args.out).resolve() if args.out else transcript.clean_path
    manifest = (pathlib.Path(args.manifest).resolve() if args.manifest
                else transcript.ads_path)
    out.write_text(build_markdown(kept, meta, ranges), encoding="utf-8")
    models_used = sorted(set(result["model"] for result in results))
    manifest_data = {
        "cleaner": "ai-segment-classifier-v1", "provider": PROVIDER,
        "model": MODEL, "fallback_models": FALLBACK_MODELS, "models_used": models_used,
        "cleaned_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": str(source), "output": str(out), "segments_total": len(segments),
        "segments_removed": len(ad_ids),
        "seconds_removed": round(sum(r["end"] - r["start"] for r in ranges), 2),
        "ad_segment_ids": ad_ids, "intervals": ranges, "chunks": results,
    }
    manifest.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(out), "manifest": str(manifest),
                      "segments_removed": len(ad_ids), "intervals": ranges}))


if __name__ == "__main__":
    main()
