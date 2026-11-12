#!/usr/bin/env python3
"""Use an AI endpoint to remove ad reads from a timestamped podcast transcript.

Only transcript text is sent to the endpoint; audio remains local. The original transcript
artifacts are untouched. Output is a cleaned Markdown derivative plus an auditable JSON manifest.
The classifier must return segment IDs, so the model cannot rewrite or summarize editorial text.

The model is reached through one function, `ask_model(prompt, model) -> str`. Production
uses the Hermes CLI (`hermes_ask`); tests pass a scripted function.
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
from dataclasses import dataclass

from transcript import Transcript, render_markdown

ROOT = pathlib.Path(os.environ.get("PA_ROOT", "~/podcast-transcripts")).expanduser()
LOG_PATH = ROOT / "logs" / "cleaner.log"
CLEANER_ID = "ai-segment-classifier-v1"


def log(message):
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


@dataclass(frozen=True)
class CleanerConfig:
    provider: str = "opencode-go"
    model: str = "deepseek-v4.1-flash-free"
    fallback_models: tuple = ("mimo-v2.6-flash-free", "beam-free", "mimo-v2.6-flash")
    timeout: int = 240
    max_core_chars: int = 18000
    context_seconds: float = 75.0
    retries: int = 2
    hermes_path: str = str(pathlib.Path.home() / ".local/bin/hermes")

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        d = cls()
        fallbacks = env.get("PA_CLEAN_FALLBACK_MODELS")
        return cls(
            provider=env.get("PA_CLEAN_PROVIDER", d.provider),
            model=env.get("PA_CLEAN_MODEL", d.model),
            fallback_models=(d.fallback_models if fallbacks is None else
                             tuple(m.strip() for m in fallbacks.split(",") if m.strip())),
            timeout=int(env.get("PA_CLEAN_TIMEOUT", d.timeout)),
            max_core_chars=int(env.get("PA_CLEAN_CHUNK_CHARS", d.max_core_chars)),
            context_seconds=float(env.get("PA_CLEAN_CONTEXT_SECONDS", d.context_seconds)),
            retries=int(env.get("PA_CLEAN_RETRIES", d.retries)),
            hermes_path=env.get("PA_CLEAN_HERMES", d.hermes_path),
        )

    @property
    def models(self):
        """Primary model first, then fallbacks, without repeats."""
        return list(dict.fromkeys([self.model, *self.fallback_models]))


@dataclass(frozen=True)
class CleanResult:
    kept: list          # Segments that are not ads, in order
    ad_ids: list        # indexes of removed Segments
    ranges: list        # contiguous removed intervals
    chunks: list        # per-chunk audit records
    models_used: list   # models that actually produced an accepted answer


def _make_cores(segments, max_core_chars):
    cores, start, chars = [], 0, 0
    for i, seg in enumerate(segments):
        size = len(seg.get("text", "")) + 40
        if i > start and chars + size > max_core_chars:
            cores.append((start, i))
            start, chars = i, 0
        chars += size
    if start < len(segments):
        cores.append((start, len(segments)))
    return cores


def _build_prompt(segments, core_start, core_end, context_seconds):
    lo_time = max(0, segments[core_start]["start"] - context_seconds)
    hi_time = segments[core_end - 1]["end"] + context_seconds
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


def _parse_json_output(raw):
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI response contained no JSON object")
    return json.loads(raw[start:end + 1])


def _classify_chunk(prompt, core_start, core_end, chunk_no, ask_model, config, sleep):
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    last_error = None
    for attempt in range(1, config.retries + 2):
        for model in config.models:
            try:
                data = _parse_json_output(ask_model(prompt, model))
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
                        "attempt": attempt, "model": model}
            except Exception as exc:
                last_error = exc
                log(f"WARNING chunk={chunk_no} attempt={attempt} model={model} failed: {exc}")
        if attempt <= config.retries:
            sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"all cleaner models failed for chunk {chunk_no}: {last_error}")


def _contiguous_ranges(ids, segments):
    if not ids:
        return []
    ranges, start, prev = [], ids[0], ids[0]
    for i in ids[1:] + [None]:
        if i is not None and i == prev + 1:
            prev = i
            continue
        ranges.append({"start_index": start, "end_index": prev,
                       "start": segments[start]["start"], "end": segments[prev]["end"]})
        start = prev = i
    return ranges


def clean(transcript, ask_model, config, *, sleep=time.sleep):
    """Classify every Segment of a Transcript and return what to keep and what was removed.

    `ask_model(prompt, model)` returns the model's raw text or raises. Each model in
    `config.models` is tried in order, the whole list up to `config.retries + 1` times.
    Raises RuntimeError if any chunk cannot be classified; nothing is written here.
    """
    segments = transcript.segments
    chunks = []
    for chunk_no, (start, end) in enumerate(_make_cores(segments, config.max_core_chars), 1):
        log(f"classifying chunk={chunk_no} core={start}:{end} via "
            f"{config.provider}/{config.model}")
        prompt = _build_prompt(segments, start, end, config.context_seconds)
        chunks.append(_classify_chunk(prompt, start, end, chunk_no, ask_model, config, sleep))
    ad_ids = sorted(set(x for chunk in chunks for x in chunk["ad_segment_ids"]))
    ad_set = set(ad_ids)
    return CleanResult(
        kept=[seg for i, seg in enumerate(segments) if i not in ad_set],
        ad_ids=ad_ids,
        ranges=_contiguous_ranges(ad_ids, segments),
        chunks=chunks,
        models_used=sorted(set(chunk["model"] for chunk in chunks)),
    )


def hermes_ask(config):
    """The production `ask_model`: one non-interactive Hermes CLI call per prompt."""
    work = ROOT / "work" / "ai-cleaner"

    def ask(prompt, model):
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt",
                                         prefix="prompt-", dir=work, delete=False) as f:
            f.write(prompt)
            prompt_file = pathlib.Path(f.name)
        try:
            cmd = [config.hermes_path, "chat", "--query-file", str(prompt_file), "--oneshot",
                   "-Q", "--provider", config.provider, "-m", model, "--reasoning", "none",
                   "--ignore-rules", "-t", "", "--max-turns", "1",
                   "--run-budget", str(max(30, config.timeout - 10)), "--source", "tool"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=config.timeout)
            if proc.returncode:
                raise RuntimeError(
                    f"Hermes rc={proc.returncode}: {(proc.stderr or proc.stdout)[-800:]}")
            return proc.stdout
        finally:
            try:
                prompt_file.unlink()
            except OSError:
                pass

    return ask


def write_outputs(transcript, result, config, *, out=None, manifest=None):
    """Write the Clean transcript and the Ad manifest. Returns their paths."""
    out = pathlib.Path(out).resolve() if out else transcript.clean_path
    manifest = pathlib.Path(manifest).resolve() if manifest else transcript.ads_path
    used = ",".join(result.models_used) or config.model
    out.write_text(render_markdown(
        result.kept, transcript.meta, heading_suffix=" — cleaned", show_duration=False,
        front_matter_extra=(("ai_cleaner", f"{config.provider}/{used}"),
                            ("ad_intervals_removed", len(result.ranges))),
        notes=("AI-cleaned derivative; the raw transcript is preserved alongside this file.",),
    ), encoding="utf-8")
    manifest.write_text(json.dumps({
        "cleaner": CLEANER_ID, "provider": config.provider, "model": config.model,
        "fallback_models": list(config.fallback_models), "models_used": result.models_used,
        "cleaned_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": str(transcript.json_path), "output": str(out),
        "segments_total": len(transcript.segments), "segments_removed": len(result.ad_ids),
        "seconds_removed": round(sum(r["end"] - r["start"] for r in result.ranges), 2),
        "ad_segment_ids": result.ad_ids, "intervals": result.ranges, "chunks": result.chunks,
    }, indent=2), encoding="utf-8")
    return out, manifest


def is_reusable(transcript, config):
    """True when an existing Clean transcript can stand in for a fresh cleaning run:
    it is at least as new as the Transcript and was made with the configured model."""
    clean_md, ads_json = transcript.clean_path, transcript.ads_path
    try:
        if clean_md.stat().st_mtime < transcript.json_path.stat().st_mtime:
            return False
        ads = json.loads(ads_json.read_text(encoding="utf-8"))
        return ads.get("provider") == config.provider and ads.get("model") == config.model
    except (OSError, ValueError, AttributeError):
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_transcript")
    ap.add_argument("--out")
    ap.add_argument("--manifest")
    args = ap.parse_args()

    config = CleanerConfig.from_env()
    transcript = Transcript.load(args.json_transcript)
    result = clean(transcript, hermes_ask(config), config)
    out, manifest = write_outputs(transcript, result, config, out=args.out,
                                  manifest=args.manifest)
    print(json.dumps({"output": str(out), "manifest": str(manifest),
                      "segments_removed": len(result.ad_ids), "intervals": result.ranges}))


if __name__ == "__main__":
    main()
