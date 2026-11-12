"""The Transcript of one Episode: its files on disk and how it is rendered.

A Transcript is stored as `<base>.json` (metadata + Segments). Everything else lives
beside it and is derived from that path:

    <base>.md        raw Markdown        <base>.srt       subtitles
    <base>.clean.md  Clean transcript    <base>.ads.json  Ad manifest

No other module derives these names or formats timestamps and paragraphs.
Standard library only.
"""
import json
import pathlib
from dataclasses import dataclass, field

FRONT_MATTER_KEYS = ("title", "show", "source_file", "audio_duration", "model", "language",
                     "transcribed_at", "rtf", "audio_sha256")
PARAGRAPH_CHARS = 600
PARAGRAPH_SECONDS = 75
_CLEAN_SUFFIX = ".clean.md"


def timestamp(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _srt_timestamp(seconds):
    ms = int(round((seconds - int(seconds)) * 1000))
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _paragraphs(segments):
    """Merge consecutive Segments into readable, timestamped paragraphs."""
    para, start, out = [], None, []
    for seg in segments:
        if start is None:
            start = seg["start"]
        para.append(seg["text"].strip())
        if (sum(len(p) + 1 for p in para) > PARAGRAPH_CHARS
                or seg["end"] - start > PARAGRAPH_SECONDS):
            out += [f"**[{timestamp(start)}]** " + " ".join(para), ""]
            para, start = [], None
    if para:
        out.append(f"**[{timestamp(start)}]** " + " ".join(para))
    return out


def render_markdown(segments, meta, *, heading_suffix="", front_matter_extra=(),
                    show_duration=True, notes=()):
    """Markdown with YAML front matter. `front_matter_extra` is (key, value) pairs
    appended after the standard keys; `notes` are italic lines under the heading."""
    def entry(key, value):
        return f'{key}: "{str(value).replace(chr(34), chr(39))}"'

    lines = ["---"]
    lines += [entry(k, meta[k]) for k in FRONT_MATTER_KEYS if meta.get(k) not in (None, "")]
    lines += [entry(k, v) for k, v in front_matter_extra]
    lines += ["---", "", f"# {meta.get('title') or 'Transcript'}{heading_suffix}", ""]
    if meta.get("show"):
        duration = f" — {timestamp(meta.get('audio_duration') or 0)}" if show_duration else ""
        lines += [f"*{meta['show']}*{duration}", ""]
    for note in notes:
        lines += [f"*{note}*", ""]
    return "\n".join(lines + _paragraphs(segments)) + "\n"


def render_srt(segments):
    out = []
    for i, seg in enumerate(segments, 1):
        out += [str(i), f"{_srt_timestamp(seg['start'])} --> {_srt_timestamp(seg['end'])}",
                seg["text"].strip(), ""]
    return "\n".join(out)


def json_path_for(markdown_path):
    """The Transcript `.json` that a raw or clean Markdown file belongs to."""
    path = pathlib.Path(markdown_path)
    if path.name.endswith(_CLEAN_SUFFIX):
        return path.with_name(path.name[:-len(_CLEAN_SUFFIX)] + ".json")
    return path.with_suffix(".json")


def is_clean_path(markdown_path):
    return pathlib.Path(markdown_path).name.endswith(_CLEAN_SUFFIX)


@dataclass
class Transcript:
    json_path: pathlib.Path
    meta: dict = field(default_factory=dict)
    segments: list = field(default_factory=list)

    def __post_init__(self):
        self.json_path = pathlib.Path(self.json_path)

    @classmethod
    def load(cls, json_path):
        path = pathlib.Path(json_path).resolve()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(path, data.get("meta", {}), data["segments"])

    @property
    def md_path(self):
        return self.json_path.with_suffix(".md")

    @property
    def srt_path(self):
        return self.json_path.with_suffix(".srt")

    @property
    def clean_path(self):
        return self.json_path.with_suffix(_CLEAN_SUFFIX)

    @property
    def ads_path(self):
        return self.json_path.with_suffix(".ads.json")

    def save_raw(self):
        """Write the raw `.md`, `.srt` and `.json`. Called once, by transcription."""
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        self.md_path.write_text(render_markdown(self.segments, self.meta), encoding="utf-8")
        self.srt_path.write_text(render_srt(self.segments), encoding="utf-8")
        self.json_path.write_text(
            json.dumps({"meta": self.meta, "segments": self.segments}, indent=1),
            encoding="utf-8")
