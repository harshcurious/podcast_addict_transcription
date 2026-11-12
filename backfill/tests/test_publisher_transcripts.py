import json

import publisher_transcripts as pt
import pytest
from pa_backup import ListenedEpisode

VTT = """WEBVTT

NOTE
Transcription provided by someone

1
00:00:01.000 --> 00:00:04.500
<v Alice> Hello and welcome to the show.

2
00:00:04.500 --> 00:00:09.000
<v Bob> Thanks for having me.

3
01:00:09.000 --> 01:00:12.250
<v Bob> Bye.
"""

SRT = """1
00:00:01,000 --> 00:00:02,000
First line

2
00:00:02,000 --> 00:00:03,500
second <i>line</i>.
"""

PODHOME_HTML = ("<cite>Alice:</cite><time>00:00:14</time><p>Welcome back.</p>"
                "<time>00:01:05</time><p>Today&#39;s topic.</p>")

PAGE_HTML = """<html><head><title>T</title><script>var x = "00:00 nope";</script></head>
<body><nav>Home Episodes</nav><div class="badge">35:07</div><p>Play on YouTube</p>
<p class="seg"><a href="#">00:00</a> Hello and welcome.</p>
<p class="seg"><a href="#">00:05</a> This is episode 409.</p>
<footer>Copyright</footer></body></html>"""


def episode(**kw):
    base: dict = dict(episode_id=1, podcast="Show", feed_url="https://ex.com/feed",
                      language="English", title="Ep One", guid="g-1", episode_url="",
                      audio_url="https://cdn.ex.com/1.mp3", published_at="2024-01-01",
                      played_at="2024-02-01T00:00:00+00:00", duration_seconds=0,
                      transcript_urls=(), description="")
    base.update(kw)
    return ListenedEpisode(**base)


def test_vtt_keeps_times_and_marks_speaker_changes():
    cues = pt.parse_cues(VTT)
    assert [c["speaker"] for c in cues] == ["Alice", "Bob", "Bob"]
    assert cues[2]["start"] == 3609.0 and cues[2]["end"] == 3612.25
    segments, timing = pt.to_segments(cues)
    assert timing == "exact"
    assert segments[0]["text"] == "Alice: Hello and welcome to the show."
    assert segments[1]["text"] == "Bob: Thanks for having me. Bye."


def test_srt_strips_markup():
    cues = pt.parse_cues(SRT)
    assert [c["text"] for c in cues] == ["First line", "second line ."]
    assert cues[1]["end"] == 3.5


def test_podcastindex_json():
    body = json.dumps({"version": "1.0.0", "segments": [
        {"speaker": "Ann", "startTime": 0.5, "endTime": 1.5, "body": "Hi"},
        {"speaker": "Ann", "startTime": 1.5, "endTime": 2.0, "body": "there."}]})
    segments, _ = pt.to_segments(pt.parse_json(body))
    assert segments == [{"start": 0.5, "end": 2.0, "text": "Ann: Hi there."}]


def test_html_time_and_cite_blocks_apply_to_the_next_paragraph():
    cues = pt.parse_html(PODHOME_HTML)
    assert [(c["start"], c["speaker"], c["text"]) for c in cues] == [
        (14, "Alice", "Welcome back."), (65, "", "Today's topic.")]


def test_html_page_chrome_is_dropped_when_the_transcript_is_timed():
    cues = pt.parse_html(PAGE_HTML)
    assert [(c["start"], c["text"]) for c in cues] == [
        (0, "Hello and welcome."), (5, "This is episode 409.")]


def test_untimed_text_gets_estimated_times_spread_over_the_duration():
    cues = pt.parse_text("one two three four.\n\nfive six seven eight.")
    segments, timing = pt.to_segments(cues, duration=80, min_chars=1)
    assert timing == "estimated"
    assert [(s["start"], s["end"]) for s in segments] == [(0, 40), (40, 80)]


def test_long_cue_runs_are_split_into_segments():
    cues = [{"start": i, "end": i + 1, "text": "word " * 30, "speaker": ""}
            for i in range(10)]  # no sentence ends: merged up to max_chars
    segments, _ = pt.to_segments(cues)
    assert 1 < len(segments) < 10
    assert all(len(s["text"]) < 400 + 160 for s in segments)
    assert (segments[0]["start"], segments[-1]["end"]) == (0, 10)


def test_a_summary_is_not_a_transcript():
    short = [{"start": 0, "end": 1, "text": "word " * 100}]
    assert pt.is_plausible(short, duration=0)[0]
    assert not pt.is_plausible(short, duration=3600)[0]


def test_kind_from_declared_type_url_or_body():
    assert pt.kind_of("application/x-subrip") == "srt"
    assert pt.kind_of("", "https://x/a.vtt?x=1") == "vtt"
    assert pt.kind_of("", "https://x/a", "WEBVTT\n\n") == "vtt"
    assert pt.kind_of("text/html") == "html"


FEED = b"""<?xml version="1.0"?>
<rss xmlns:podcast="https://podcastindex.org/namespace/1.0"><channel>
<item><title>Other</title><guid>g-0</guid>
  <podcast:transcript url="https://ex.com/0.vtt" type="text/vtt"/></item>
<item><title>Ep One</title><guid>g-1</guid><enclosure url="https://cdn.ex.com/1.mp3"/>
  <podcast:transcript url="https://ex.com/1.html" type="text/html"/>
  <podcast:transcript url="https://ex.com/1.srt" type="application/x-subrip"/></item>
</channel></rss>"""


class Web:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url, **_):
        self.calls.append(url)
        if url not in self.pages:
            raise OSError(f"404 {url}")
        return self.pages[url], ""


def test_feed_item_is_matched_by_guid_and_cached(tmp_path):
    web = Web({"https://ex.com/feed": FEED})
    feeds = pt.FeedIndex(tmp_path, get=web)
    links = feeds.transcript_links(episode())
    assert [u for _, u in links] == ["https://ex.com/1.html", "https://ex.com/1.srt"]
    feeds.transcript_links(episode(guid="nope"))  # matched by enclosure instead
    assert pt.FeedIndex(tmp_path, get=web).transcript_links(episode(guid="", audio_url=""))
    assert web.calls == ["https://ex.com/feed"]


def test_find_prefers_timed_formats_and_falls_back_on_bad_ones(tmp_path):
    srt = "\n\n".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n" + "talk " * 10
                      for i in range(1, 8))
    web = Web({"https://ex.com/feed": FEED, "https://ex.com/1.srt": srt.encode(),
               "https://ex.com/1.html": b"<p>" + b"words " * 80 + b"</p>"})
    found = pt.find(episode(), pt.FeedIndex(tmp_path, get=web), get=web, log=lambda m: None)
    assert found["format"] == "srt" and found["timing"] == "exact" and found["words"] == 70

    web.pages.pop("https://ex.com/1.srt")
    found = pt.find(episode(), pt.FeedIndex(tmp_path, get=web), get=web, log=lambda m: None)
    assert found["format"] == "html" and found["timing"] == "estimated"


def test_no_transcript_anywhere(tmp_path):
    web = Web({"https://ex.com/feed": FEED})
    with pytest.raises(pt.NoTranscript):
        pt.find(episode(guid="g-9", audio_url="", title="Missing"),
                pt.FeedIndex(tmp_path, get=web), get=web, log=lambda m: None)


def test_unreachable_feed_means_no_links(tmp_path):
    feeds = pt.FeedIndex(tmp_path, get=Web({}), log=lambda m: None)
    assert feeds.transcript_links(episode()) == []
