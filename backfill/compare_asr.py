#!/usr/bin/env python3
"""Compare local speech-recognition engines on real Episodes from a backup.

Usage (from the repository root):
    backfill/.venv/bin/python backfill/compare_asr.py BACKUP --episode-id N [--episode-id M ...]
        [--engines whisper,parakeet,qwen3-asr]

For each Episode the audio is downloaded once (kept in $PA_ROOT/work/bench/). Every engine
transcribes it, and if the publisher has a transcript online, each result is scored
against it by word error rate (WER). The publisher transcript is only a reference: it may
itself be machine-made, and ads that were inserted into the audio count as errors for
every engine alike, so compare engines with each other rather than reading WER as
absolute. Results are printed and saved to work/bench/results.json.

`qwen3-asr` (mlx-audio) is benchmark-only and opt-in: it returns one timestamp per
20-minute chunk, too coarse to cut ad reads out, and it has looped for hours on a 55-minute
Episode, so the backfill itself does not use it.
"""
import argparse
import json
import os
import pathlib
import re
import sys
import time
import unicodedata

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

import envfile  # noqa: E402

envfile.load()  # before the pipeline modules read PA_* at import
import backfill  # noqa: E402
import publisher_transcripts as online  # noqa: E402
from mac_asr import PARAKEET, PARAKEET_LANGUAGES, WHISPER, Recognizer, download  # noqa: E402
from pa_backup import database_path, listened_episodes  # noqa: E402

QWEN3 = "qwen3-asr"
QWEN3_MODEL = os.environ.get("PA_QWEN3_ASR_MODEL", "mlx-community/Qwen3-ASR-1.7B-8bit")
QWEN3_LANGUAGES = {"en": "English", "hi": "Hindi", "de": "German", "fr": "French",
                   "es": "Spanish", "it": "Italian", "pt": "Portuguese", "ru": "Russian",
                   "ja": "Japanese", "ko": "Korean", "zh": "Chinese"}
BENCH = backfill.WORK / "bench"


def normalize(text):
    """Lowercase, punctuation and symbols removed. Only Unicode P*/S* characters go, so
    combining marks (Devanagari vowel signs, virama, nukta) stay inside their words."""
    text = text.lower().replace("’", "'")
    text = "".join(" " if unicodedata.category(ch)[0] in "PS" and ch != "'" else ch
                   for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def wer(reference, hypothesis):
    import jiwer
    return jiwer.wer(normalize(reference), normalize(hypothesis))


def reference_text(ep, feeds):
    """The publisher's transcript as plain words, or None."""
    for kind, _, url in online.candidates(ep, feeds):
        try:
            body, content_type = online.http_get(url, max_bytes=20 * 1024 * 1024)
            body = body.decode("utf-8", "replace")
            kind = kind or online.kind_of(content_type, url, body)
            cues = online.PARSERS[kind](body)
            if online.is_plausible(cues, ep.duration_seconds)[0]:
                return " ".join(c["text"] for c in cues), url
        except Exception as exc:  # a bad link only means no reference from it
            print(f"  reference {url} unusable: {exc}")
    return None, None


class Qwen3:
    def __init__(self):
        self.model = None

    def transcribe(self, path, language):
        from mlx_audio.stt import load
        if self.model is None:
            self.model = load(QWEN3_MODEL)
        started = time.time()
        result = self.model.generate(str(path), language=QWEN3_LANGUAGES.get(language),
                                     max_tokens=200_000)  # default 8192 truncates an hour
        return result.text, time.time() - started


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("backup")
    ap.add_argument("--episode-id", type=int, action="append", required=True)
    ap.add_argument("--engines", default=f"{WHISPER},{PARAKEET}")
    opts = ap.parse_args()
    engines = [e.strip() for e in opts.engines.split(",") if e.strip()]

    episodes = {e.episode_id: e for e in listened_episodes(database_path(opts.backup,
                                                                         backfill.WORK))}
    feeds = online.FeedIndex(backfill.WORK / "feeds", log=print)
    recognizers = {e: Recognizer(e) for e in engines if e != QWEN3}
    qwen3 = Qwen3()
    results = []
    for episode_id in opts.episode_id:
        ep = episodes[episode_id]
        language = backfill.language_code(ep)
        print(f"\n== {ep.podcast} — {ep.title} ({ep.duration_seconds / 60:.0f} min, "
              f"{language or '?'})")
        cached = sorted((BENCH / "audio").glob(f"{ep.key}.*"))
        audio = cached[0] if cached else download(ep.audio_url, BENCH / "audio", ep.key,
                                                  max_bytes=backfill.MAX_AUDIO_BYTES)[0]
        reference, ref_url = reference_text(ep, feeds)
        print(f"  reference: {ref_url or 'none'}")
        for engine in engines:
            if engine == PARAKEET and language not in PARAKEET_LANGUAGES:
                print(f"  {engine:10s} skipped: does not support {language or 'unknown'}")
                continue
            if engine == QWEN3:
                text, wall = qwen3.transcribe(audio, language)
                model = QWEN3_MODEL
            else:
                segments, info = recognizers[engine].transcribe(audio, language)
                text, wall, model = " ".join(s["text"] for s in segments), info["wall_seconds"], \
                    info["model"]
            row = {"episode_id": episode_id, "title": ep.title, "language": language,
                   "engine": engine, "model": model, "seconds": round(wall, 1),
                   "realtime": round(ep.duration_seconds / wall, 1) if wall else None,
                   "words": len(text.split()),
                   "wer": round(wer(reference, text), 4) if reference else None}
            results.append(row)
            (BENCH / f"{ep.key}.{engine}.txt").write_text(text, encoding="utf-8")
            score = f"WER {row['wer']:.1%}" if row["wer"] is not None else "no reference"
            print(f"  {engine:10s} {score:12s} {row['realtime']:>6}x realtime  "
                  f"{row['words']} words  {model}")
    BENCH.mkdir(parents=True, exist_ok=True)
    (BENCH / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"\nsaved {BENCH / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
