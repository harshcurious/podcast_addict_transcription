import json
import pathlib

from tests.fixtures import sample
from transcript import (Transcript, is_clean_path, json_path_for, render_markdown,
                        render_srt, timestamp)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_raw_markdown_matches_reference_output():
    assert render_markdown(sample.SEGMENTS, sample.META) == (FIXTURES / "raw.expected.md").read_text()


def test_srt_matches_reference_output():
    assert render_srt(sample.SEGMENTS) == (FIXTURES / "raw.expected.srt").read_text()


def test_clean_markdown_matches_reference_output():
    kept = [s for i, s in enumerate(sample.SEGMENTS) if i not in set(sample.AD_IDS)]
    got = render_markdown(
        kept, sample.META, heading_suffix=" — cleaned", show_duration=False,
        front_matter_extra=(("ai_cleaner", "opencode-go/deepseek-v4.1-flash-free"),
                            ("ad_intervals_removed", 2)),
        notes=("AI-cleaned derivative; the raw transcript is preserved alongside this file.",))
    assert got == (FIXTURES / "clean.expected.md").read_text()


def test_timestamps():
    assert timestamp(0) == "0:00"
    assert timestamp(59.9) == "0:59"
    assert timestamp(3700) == "1:01:40"
    assert "01:01:40,250 --> 01:02:03,400" in render_srt(sample.SEGMENTS)


def test_empty_transcript_renders_header_only():
    assert render_markdown([], {}).endswith("# Transcript\n\n")
    assert render_srt([]) == ""


def test_save_then_load_round_trips_and_writes_all_raw_files(tmp_path):
    saved = Transcript(tmp_path / "show" / "2026-01-01-ep.json", sample.META, sample.SEGMENTS)
    saved.save_raw()
    assert sorted(p.name for p in (tmp_path / "show").iterdir()) == [
        "2026-01-01-ep.json", "2026-01-01-ep.md", "2026-01-01-ep.srt"]
    assert json.loads(saved.json_path.read_text()) == {"meta": sample.META,
                                                       "segments": sample.SEGMENTS}
    loaded = Transcript.load(saved.json_path)
    assert (loaded.meta, loaded.segments) == (sample.META, sample.SEGMENTS)
    assert loaded.md_path.read_text() == (FIXTURES / "raw.expected.md").read_text()


def test_sibling_paths():
    t = Transcript("/out/show/2026-01-01-ep.v2.json")
    assert str(t.md_path) == "/out/show/2026-01-01-ep.v2.md"
    assert str(t.clean_path) == "/out/show/2026-01-01-ep.v2.clean.md"
    assert str(t.ads_path) == "/out/show/2026-01-01-ep.v2.ads.json"
    assert json_path_for(t.clean_path) == t.json_path
    assert json_path_for(t.md_path) == t.json_path
    assert is_clean_path(t.clean_path) and not is_clean_path(t.md_path)
