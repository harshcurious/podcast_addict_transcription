"""Find and parse a transcript the publisher already put online for an Episode.

Sources, in order: the transcript URLs Podcast Addict stored in the backup, then the
Podcasting 2.0 `<podcast:transcript>` tags of the Episode's item in its live RSS feed.
Formats: JSON (podcastindex spec), WebVTT, SRT, HTML and plain text. Each becomes a list
of Segments shaped like ASR output, so the ad classifier and renderer treat them alike.

Everything fetched here is untrusted text; it is parsed, never executed. Standard library only.
"""
import hashlib
import html
import html.parser
import json
import pathlib
import re
import time
import urllib.request
import xml.etree.ElementTree as ET

USER_AGENT = "pa-transcript-backfill/1.0"
PREFERENCE = ("json", "vtt", "srt", "html", "text")
WORDS_PER_SECOND = 2.5          # ~150 wpm, used only to estimate missing timings
MIN_WORDS = 50
MIN_COVERAGE = 0.2              # transcript must hold >= 20% of the words a talk of that
                                # length would; shorter files are summaries, not transcripts


class NoTranscript(Exception):
    """No usable transcript was found online for this Episode."""


def http_get(url, *, max_bytes=50 * 1024 * 1024, timeout=30):
    """GET `url`; returns (body bytes, content type). The only network call here."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(f"response larger than {max_bytes} bytes: {url}")
        return body, response.headers.get("Content-Type", "")


def kind_of(declared, url="", body=""):
    """Map a declared MIME type / URL / sniffed body to one of PREFERENCE, or None."""
    for hint in (declared or "", pathlib.PurePosixPath(url.split("?")[0]).suffix):
        hint = hint.lower()
        for needle, kind in (("json", "json"), ("vtt", "vtt"), ("subrip", "srt"),
                             ("srt", "srt"), ("html", "html"), ("plain", "text"),
                             (".txt", "text")):
            if needle in hint:
                return kind
    head = body.lstrip()[:200]
    if head.startswith("WEBVTT"):
        return "vtt"
    if head.startswith("{"):
        return "json"
    if re.match(r"\d+\s*\n[\d:,]+ --> ", head):
        return "srt"
    if head.startswith("<"):
        return "html"
    return "text" if head else None


# ---------------------------------------------------------------------------- parsing

_CUE_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?")
_LEADING_TS = re.compile(r"^\s*[\[(]?((?:\d{1,2}:)?\d{1,2}:\d{2})(?:[.,]\d+)?[\])]?\s*[-–—]?\s*")
_VOICE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>")
_TAG = re.compile(r"<[^>]+>")


def _seconds(text):
    m = _CUE_TIME.fullmatch(text.strip())
    if not m:
        raise ValueError(f"bad timestamp {text!r}")
    h, mnt, s, frac = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(s) + (int(frac.ljust(3, "0")) / 1000
                                                           if frac else 0)


def _clean_text(text):
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", text))).strip()


def _speaker(name):
    name = (name or "").strip().rstrip(":").strip()
    return "" if name.lower() in ("", "unknown", "speaker", "unknown speaker") else name


def parse_cues(body):
    """WebVTT or SRT -> cues [{start, end, text, speaker}]."""
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", body.replace("﻿", "")):
        lines = block.strip().splitlines()
        arrow = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if arrow is None:
            continue
        start, _, rest = lines[arrow].partition("-->")
        try:
            begin, end = _seconds(start), _seconds(rest.strip().split()[0])
        except (ValueError, IndexError):
            continue
        raw = " ".join(lines[arrow + 1:])
        voice = _VOICE.search(raw)
        text = _clean_text(raw)
        if text:
            cues.append({"start": begin, "end": end, "text": text,
                         "speaker": _speaker(voice.group(1)) if voice else ""})
    return cues


def parse_json(body):
    data = json.loads(body)
    rows = data.get("segments") if isinstance(data, dict) else data
    cues = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        text = _clean_text(str(row.get("body") or row.get("text") or ""))
        if text:
            start = float(row.get("startTime", row.get("start", 0)) or 0)
            cues.append({"start": start,
                         "end": float(row.get("endTime", row.get("end", start)) or start),
                         "text": text, "speaker": _speaker(str(row.get("speaker") or ""))})
    return cues


class _Blocks(html.parser.HTMLParser):
    """Visible text of an HTML page split into block-level chunks."""
    SKIP = {"script", "style", "head", "nav", "header", "footer", "noscript", "svg",
            "form", "button", "select", "template"}
    BLOCK = {"p", "div", "li", "br", "tr", "td", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "time", "cite", "dt", "dd", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self._buf, self._skip = [], [], 0

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if text:
            self.blocks.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def parse_blocks(blocks):
    """Text blocks -> cues. A block that is only a timestamp or only `Name:` applies to the
    next block. If any block carries a timestamp, untimed blocks are page chrome and dropped."""
    cues, pending_time, pending_speaker = [], None, ""
    for block in blocks:
        if _CUE_TIME.fullmatch(block.strip("[]() ")):
            pending_time = _seconds(block.strip("[]() "))
            continue
        if len(block) <= 40 and block.endswith(":") and len(block.split()) <= 4:
            pending_speaker = _speaker(block)
            continue
        start = pending_time
        lead = _LEADING_TS.match(block)
        if lead:
            start, block = _seconds(lead.group(1)), block[lead.end():]
        if block.strip():
            cues.append({"start": start, "end": None, "text": block.strip(),
                         "speaker": pending_speaker})
        pending_time, pending_speaker = None, ""
    if any(c["start"] is not None for c in cues):
        cues = [c for c in cues if c["start"] is not None]
        # Page chrome before the transcript (a duration badge, a player) can carry a
        # timestamp too; the transcript itself begins at its earliest time.
        first = min(range(len(cues)), key=lambda i: cues[i]["start"])
        cues = cues[first:]
    return cues


def parse_html(body):
    parser = _Blocks()
    parser.feed(body)
    parser.close()
    return parse_blocks(parser.blocks)


def parse_text(body):
    return parse_blocks([b.strip() for b in re.split(r"\n\s*\n|\r\n\s*\r\n", body)
                         if b.strip()])


PARSERS = {"json": parse_json, "vtt": parse_cues, "srt": parse_cues,
           "html": parse_html, "text": parse_text}


def to_segments(cues, duration=0.0, *, max_chars=400, min_chars=120):
    """Merge short cues into sentence-sized Segments and fill in missing timings.

    Returns (segments, timing) where timing is "exact" or "estimated". A speaker label is
    written into the text where the speaker changes, so the classifier can see it.
    """
    if not cues:
        return [], "exact"
    estimated = any(c["start"] is None for c in cues)
    if estimated:
        total_words = sum(len(c["text"].split()) for c in cues)
        scale = (duration / total_words) if duration and total_words else 1 / WORDS_PER_SECOND
        clock = 0.0
        for c in cues:
            c["start"] = clock
            clock += len(c["text"].split()) * scale
            c["end"] = clock
    for i, c in enumerate(cues):
        if c.get("end") is None or c["end"] < c["start"]:
            nxt = cues[i + 1]["start"] if i + 1 < len(cues) else None
            c["end"] = nxt if nxt is not None and nxt >= c["start"] else \
                c["start"] + len(c["text"].split()) / WORDS_PER_SECOND

    segments, current, last_speaker = [], None, ""
    for c in cues:
        new_speaker = c["speaker"] and c["speaker"] != last_speaker
        if current and (new_speaker or len(current["text"]) >= max_chars or (
                len(current["text"]) >= min_chars
                and re.search(r"[.!?][\"')\]]?$", current["text"]))):
            segments.append(current)
            current = None
        text = f"{c['speaker']}: {c['text']}" if new_speaker else c["text"]
        if c["speaker"]:
            last_speaker = c["speaker"]
        if current is None:
            current = {"start": c["start"], "end": c["end"], "text": text}
        else:
            current["text"] += " " + text
            current["end"] = max(current["end"], c["end"])
    if current:
        segments.append(current)
    for seg in segments:
        seg["start"], seg["end"] = round(seg["start"], 2), round(seg["end"], 2)
    return segments, ("estimated" if estimated else "exact")


def is_plausible(segments, duration):
    words = sum(len(s["text"].split()) for s in segments)
    expected = duration * WORDS_PER_SECOND * MIN_COVERAGE if duration else 0
    return words >= max(MIN_WORDS, expected), words


# ------------------------------------------------------------------------------ feeds

class FeedIndex:
    """Transcript links per feed item, fetched once per feed and cached on disk."""

    def __init__(self, cache_dir, *, max_age_days=7, get=None, log=print):
        self.cache_dir = pathlib.Path(cache_dir)
        self.max_age = max_age_days * 86400
        self._get, self._log = get or http_get, log
        self._items = {}

    def _feed_bytes(self, feed_url):
        path = self.cache_dir / (hashlib.sha256(feed_url.encode()).hexdigest()[:24] + ".xml")
        if path.exists() and time.time() - path.stat().st_mtime < self.max_age:
            return path.read_bytes()
        body, _ = self._get(feed_url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)
        return body

    def _load(self, feed_url):
        items = []
        try:
            root = ET.fromstring(self._feed_bytes(feed_url))
        except Exception as exc:
            self._log(f"WARNING feed unavailable {feed_url}: {exc}")
            return items
        for item in root.iter("item"):
            links = [(t.get("type") or "", t.get("url"))
                     for t in item if isinstance(t.tag, str) and t.tag.endswith("}transcript")
                     and "podcastindex" in t.tag and t.get("url")]
            if not links:
                continue
            enclosure = item.find("enclosure")
            items.append({"guid": (item.findtext("guid") or "").strip(),
                          "audio": enclosure.get("url", "") if enclosure is not None else "",
                          "title": _norm(item.findtext("title") or ""), "links": links})
        return items

    def transcript_links(self, episode):
        if not episode.feed_url.startswith("http"):
            return []
        if episode.feed_url not in self._items:
            self._items[episode.feed_url] = self._load(episode.feed_url)
        items = self._items[episode.feed_url]
        for match in (lambda i: episode.guid and i["guid"] == episode.guid,
                      lambda i: episode.audio_url and i["audio"] == episode.audio_url,
                      lambda i: i["title"] and i["title"] == _norm(episode.title)):
            for item in items:
                if match(item):
                    return item["links"]
        return []


def _norm(text):
    return re.sub(r"[^a-z0-9]+", "", text.lower())


# ------------------------------------------------------------------------------- find

def candidates(episode, feeds):
    """(kind, declared type, url) for every known transcript link, best format first."""
    links = list(episode.transcript_urls)
    if feeds is not None:
        links += feeds.transcript_links(episode)
    seen, out = set(), []
    for declared, url in links:
        if url in seen or not url.startswith("http"):
            continue
        seen.add(url)
        kind = kind_of(declared, url)
        out.append((PREFERENCE.index(kind) if kind in PREFERENCE else len(PREFERENCE),
                    kind, declared, url))
    return [(k, d, u) for _, k, d, u in sorted(out, key=lambda x: x[0])]


def find(episode, feeds, *, get=None, log=print):
    """Return {"segments", "url", "format", "timing", "words"} or raise NoTranscript."""
    get = get or http_get
    reasons = []
    for kind, declared, url in candidates(episode, feeds):
        try:
            raw, content_type = get(url, max_bytes=20 * 1024 * 1024)
            body = raw.decode("utf-8", "replace")
            kind = kind or kind_of(content_type, url, body)
            if kind not in PARSERS:
                raise ValueError(f"unsupported transcript type {declared or content_type!r}")
            segments, timing = to_segments(PARSERS[kind](body), episode.duration_seconds)
            ok, words = is_plausible(segments, episode.duration_seconds)
            if not ok:
                raise ValueError(f"only {words} words; not a full transcript")
            return {"segments": segments, "url": url, "format": kind, "timing": timing,
                    "words": words}
        except Exception as exc:
            reasons.append(f"{url}: {exc}")
            log(f"WARNING transcript rejected {url}: {exc}")
    raise NoTranscript("; ".join(reasons) or "no transcript published")
