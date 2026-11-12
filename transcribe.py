#!/usr/bin/env python3
"""
Transcribe one podcast audio file locally with faster-whisper (CTranslate2, CPU int8).

Usage:
    transcribe.py AUDIO [--show NAME] [--model NAME] [--lang en] [--beam 1]
                         [--outdir DIR] [--keep-wav] [--source-meta JSON]

Writes into <outdir>/<show-slug>/:
    <date>-<slug>.md    timestamped readable transcript (+ YAML front matter)
    <date>-<slug>.srt   subtitles
    <date>-<slug>.json  full segment data / metadata
and appends one line to <outdir>/index.jsonl
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

DEFAULT_MODEL = os.environ.get("PA_MODEL", "base.en")
DEFAULT_OUT = os.environ.get("PA_OUTDIR", os.path.expanduser("~/podcast-transcripts/out"))
WORK = os.environ.get("PA_WORK", os.path.expanduser("~/podcast-transcripts/work"))
BEAM = int(os.environ.get("PA_BEAM", "1"))
THREADS = int(os.environ.get("PA_THREADS", str(os.cpu_count() or 4)))


def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slugify(text, maxlen=60):
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:maxlen].strip("-") or "episode"


def ffprobe_info(path):
    """Return duration and media tags supplied by the publisher."""
    try:
        raw = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags",
             "-of", "json", path], capture_output=True, text=True, check=True).stdout
        fmt = json.loads(raw).get("format", {})
        tags = {str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()}
        return float(fmt.get("duration") or 0), tags
    except Exception:
        return 0.0, {}


def plain_text(value, maxlen=12000):
    value = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"\s+", " ", value).strip()[:maxlen]


def decode_to_pcm(path):
    """Decode any audio container to 16 kHz mono int16 via system ffmpeg -> numpy float32."""
    import numpy as np
    cmd = ["ffmpeg", "-nostdin", "-threads", "0", "-i", path,
           "-f", "s16le", "-ac", "1", "-acodec", "pcm_s16le", "-ar", "16000", "-"]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(proc.stdout, np.int16).flatten().astype(np.float32) / 32768.0


def ts_md(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def ts_srt(seconds):
    ms = int(round((seconds - int(seconds)) * 1000))
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_markdown(segments, meta):
    lines = ["---"]
    for k in ("title", "show", "source_file", "audio_duration", "model",
              "language", "transcribed_at", "rtf", "audio_sha256"):
        if meta.get(k) not in (None, ""):
            val = str(meta[k]).replace('"', "'")
            lines.append(f'{k}: "{val}"')
    lines += ["---", "", f"# {meta.get('title') or 'Transcript'}", ""]
    if meta.get("show"):
        lines.append(f"*{meta['show']}* — {ts_md(meta.get('audio_duration') or 0)}")
        lines.append("")
    # merge consecutive segments into readable paragraphs of ~600 chars
    para, para_start, body = [], None, []
    for seg in segments:
        if para_start is None:
            para_start = seg["start"]
        para.append(seg["text"].strip())
        if sum(len(p) + 1 for p in para) > 600 or (seg["end"] - para_start) > 75:
            body.append(f"**[{ts_md(para_start)}]** " + " ".join(para))
            body.append("")
            para, para_start = [], None
    if para:
        body.append(f"**[{ts_md(para_start)}]** " + " ".join(para))
    lines += body
    return "\n".join(lines) + "\n"


def build_srt(segments):
    out = []
    for i, seg in enumerate(segments, 1):
        out.append(str(i))
        out.append(f"{ts_srt(seg['start'])} --> {ts_srt(seg['end'])}")
        out.append(seg["text"].strip())
        out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--show", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--device", default=os.environ.get("PA_DEVICE", "cpu"),
                    help="cpu | cuda")
    ap.add_argument("--compute", default=os.environ.get("PA_COMPUTE", "int8"),
                    help="int8 (CPU) | float16 (GPU) | float32")
    ap.add_argument("--lang", default=os.environ.get("PA_LANG", ""))
    ap.add_argument("--beam", type=int, default=BEAM)
    ap.add_argument("--outdir", default=DEFAULT_OUT)
    ap.add_argument("--keep-wav", action="store_true")
    ap.add_argument("--source-meta", default="")
    args = ap.parse_args()

    src = os.path.abspath(args.audio)
    if not os.path.exists(src):
        log(f"ERROR audio not found: {src}")
        return 2

    meta = {}
    if args.source_meta and os.path.exists(args.source_meta):
        try:
            meta = json.load(open(args.source_meta))
        except Exception:
            pass

    duration, media_tags = ffprobe_info(src)
    show = args.show or meta.get("show") or media_tags.get("album") or ""
    title = args.title or meta.get("title") or media_tags.get("title") \
        or os.path.splitext(os.path.basename(src))[0]
    description = plain_text(media_tags.get("description") or media_tags.get("lyrics-eng")
                             or media_tags.get("lyrics") or media_tags.get("comment"))

    os.makedirs(WORK, exist_ok=True)
    log(f"decoding {os.path.basename(src)} ({duration/60:.1f} min) with ffmpeg")
    t_dec = time.time()
    audio = decode_to_pcm(src)
    log(f"decoded {len(audio)/16000:.1f}s audio in {time.time()-t_dec:.1f}s")

    from faster_whisper import WhisperModel
    t0 = time.time()
    log(f"loading model {args.model} ({args.device} {args.compute}, {THREADS} threads)")
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute,
                         cpu_threads=THREADS)
    log(f"model loaded in {time.time()-t0:.1f}s")

    t1 = time.time()
    segments_iter, info = model.transcribe(
        audio,
        beam_size=args.beam,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        condition_on_previous_text=False,
        language=args.lang or None,
    )
    segments = []
    for seg in segments_iter:
        segments.append({"start": round(seg.start, 2), "end": round(seg.end, 2),
                         "text": seg.text.strip()})
    elapsed = time.time() - t1
    rtf = (info.duration / elapsed) if elapsed else 0

    date = dt.datetime.now().strftime("%Y-%m-%d")
    folder = os.path.join(args.outdir, slugify(show) if show else "unknown-show")
    os.makedirs(folder, exist_ok=True)
    base = os.path.join(folder, f"{date}-{slugify(title)}")

    out_meta = {
        "title": title,
        "show": show,
        "description": description,
        "publisher_artist": media_tags.get("artist") or media_tags.get("album_artist") or "",
        "publisher_date": media_tags.get("date") or media_tags.get("year") or "",
        "source_file": os.path.basename(src),
        "source_path": meta.get("source_path", ""),
        "source_app": meta.get("source_app", ""),
        "source_device": meta.get("source_device", ""),
        "received_at": meta.get("received_at", ""),
        "completed_at": meta.get("completed_at", ""),
        "published_at": meta.get("published_at", ""),
        "feed_url": meta.get("feed_url", ""),
        "episode_url": meta.get("episode_url", ""),
        "file_bytes": meta.get("bytes"),
        "audio_duration": round(info.duration, 1),
        "model": args.model,
        "language": info.language,
        "language_probability": round(getattr(info, "language_probability", 0) or 0, 3),
        "transcribed_at": dt.datetime.now().isoformat(timespec="seconds"),
        "rtf": round(rtf, 2),
        "wall_seconds": round(elapsed, 1),
        "audio_sha256": meta.get("sha256", ""),
        "segments": len(segments),
        "words": sum(len(s["text"].split()) for s in segments),
    }

    with open(base + ".md", "w") as f:
        f.write(build_markdown(segments, out_meta))
    with open(base + ".srt", "w") as f:
        f.write(build_srt(segments))
    with open(base + ".json", "w") as f:
        json.dump({"meta": out_meta, "segments": segments}, f, indent=1)
    if args.source_meta:
        try:
            meta.update({"transcript_md": base + ".md", "transcript_srt": base + ".srt",
                         "transcript_json": base + ".json", "title": title, "show": show})
            with open(args.source_meta, "w") as f:
                json.dump(meta, f, indent=2)
        except Exception as exc:
            log(f"WARNING could not update source metadata: {exc}")
    with open(os.path.join(args.outdir, "index.jsonl"), "a") as f:
        f.write(json.dumps({"md": base + ".md", **{k: v for k, v in out_meta.items()
                                                   if k not in ("segments",)}}) + "\n")

    log(f"done: {base}.md | {out_meta['words']} words | {elapsed:.1f}s "
        f"for {info.duration/60:.1f} min audio ({rtf:.2f}x realtime) | lang={info.language}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
