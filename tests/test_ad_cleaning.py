import json
import os

import pytest

from clean_transcript_ai import CleanerConfig, clean, is_reusable, write_outputs
from transcript import Transcript

CONFIG = CleanerConfig(provider="prov", model="primary", fallback_models=("backup",),
                       retries=1, context_seconds=0)


def make_transcript(tmp_path, n=6):
    segments = [{"start": float(i), "end": float(i + 1), "text": f"segment {i}"}
                for i in range(n)]
    meta = {"title": "Ep", "show": "Show", "source_path": "/phone/secret/path.mp3"}
    t = Transcript(tmp_path / "ep.json", meta, segments)
    t.save_raw()
    return t


def answer(ids):
    return "Sure! " + json.dumps({"ad_segment_ids": ids, "groups": [{"segment_ids": ids}]})


class Script:
    """A scripted model: replies are consumed in order; an Exception reply is raised."""
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, prompt, model):
        self.calls.append((prompt, model))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def no_sleep(seconds):
    pass


def test_no_ads_keeps_every_segment(tmp_path):
    t = make_transcript(tmp_path)
    result = clean(t, Script(answer([])), CONFIG, sleep=no_sleep)
    assert result.kept == t.segments
    assert (result.ad_ids, result.ranges, result.models_used) == ([], [], ["primary"])


def test_ad_segments_are_removed_and_grouped_into_intervals(tmp_path):
    t = make_transcript(tmp_path)
    result = clean(t, Script(answer([4, 1, 2, 2])), CONFIG, sleep=no_sleep)
    assert result.ad_ids == [1, 2, 4]
    assert [s["text"] for s in result.kept] == ["segment 0", "segment 3", "segment 5"]
    assert result.ranges == [
        {"start_index": 1, "end_index": 2, "start": 1.0, "end": 3.0},
        {"start_index": 4, "end_index": 4, "start": 4.0, "end": 5.0}]


def test_long_transcript_is_classified_in_chunks_and_merged(tmp_path):
    t = make_transcript(tmp_path)
    config = CleanerConfig(model="primary", fallback_models=(), context_seconds=0,
                           max_core_chars=110)  # two ~50-char segments per chunk
    model = Script(answer([1]), answer([2]), answer([]))
    result = clean(t, model, config, sleep=no_sleep)
    assert [c["core"] for c in result.chunks] == [[0, 2], [2, 4], [4, 6]]
    assert result.ad_ids == [1, 2]
    assert result.ranges == [{"start_index": 1, "end_index": 2, "start": 1.0, "end": 3.0}]
    assert "CORE IDS: 2,3" in model.calls[1][0]


def test_fallback_model_answers_when_primary_misbehaves(tmp_path):
    t = make_transcript(tmp_path)
    model = Script(answer([99]), answer([3]))  # primary names a segment that does not exist
    result = clean(t, model, CONFIG, sleep=no_sleep)
    assert [m for _, m in model.calls] == ["primary", "backup"]
    assert result.ad_ids == [3]
    assert result.models_used == ["backup"]


@pytest.mark.parametrize("bad", ["no json here", '{"ad_segment_ids": "1,2"}',
                                 RuntimeError("endpoint down")])
def test_unusable_reply_is_retried_on_the_next_model(tmp_path, bad):
    result = clean(make_transcript(tmp_path), Script(bad, answer([0])), CONFIG, sleep=no_sleep)
    assert result.ad_ids == [0]


def test_all_models_failing_raises_after_every_retry_and_writes_nothing(tmp_path):
    t = make_transcript(tmp_path)
    before = sorted(os.listdir(tmp_path))
    model = Script(*[RuntimeError("down")] * 4)
    slept = []
    with pytest.raises(RuntimeError, match="all cleaner models failed"):
        clean(t, model, CONFIG, sleep=slept.append)
    assert [m for _, m in model.calls] == ["primary", "backup", "primary", "backup"]
    assert slept == [2]
    assert sorted(os.listdir(tmp_path)) == before


def test_only_segment_text_reaches_the_model(tmp_path):
    t = make_transcript(tmp_path)
    model = Script(answer([]))
    clean(t, model, CONFIG, sleep=no_sleep)
    prompt = model.calls[0][0]
    assert "segment 5" in prompt
    assert "/phone/secret/path.mp3" not in prompt


def test_outputs_name_the_models_that_answered_and_leave_raw_files_alone(tmp_path):
    t = make_transcript(tmp_path)
    raw = {p: p.read_bytes() for p in (t.json_path, t.md_path, t.srt_path)}
    result = clean(t, Script(answer([99]), answer([1, 2])), CONFIG, sleep=no_sleep)
    out, manifest = write_outputs(t, result, CONFIG)

    assert (out, manifest) == (t.clean_path, t.ads_path)
    text = out.read_text()
    assert 'ai_cleaner: "prov/backup"' in text
    assert 'ad_intervals_removed: "1"' in text
    assert "segment 0 segment 3" in text and "segment 1" not in text
    ads = json.loads(manifest.read_text())
    assert ads["provider"] == "prov" and ads["model"] == "primary"
    assert ads["models_used"] == ["backup"]
    assert ads["fallback_models"] == ["backup"]
    assert ads["segments_total"] == 6 and ads["segments_removed"] == 2
    assert ads["seconds_removed"] == 2.0
    assert ads["ad_segment_ids"] == [1, 2]
    assert {p: p.read_bytes() for p in raw} == raw


def test_existing_clean_output_is_reused_only_for_the_same_model_and_source(tmp_path):
    t = make_transcript(tmp_path)
    assert not is_reusable(t, CONFIG)
    write_outputs(t, clean(t, Script(answer([])), CONFIG, sleep=no_sleep), CONFIG)
    assert is_reusable(t, CONFIG)
    assert not is_reusable(t, CleanerConfig(provider="prov", model="another"))

    newer = t.clean_path.stat().st_mtime + 10
    os.utime(t.json_path, (newer, newer))
    assert not is_reusable(t, CONFIG)


def test_config_from_env():
    config = CleanerConfig.from_env({"PA_CLEAN_MODEL": "m", "PA_CLEAN_FALLBACK_MODELS": "a, m,b,",
                                     "PA_CLEAN_RETRIES": "0"})
    assert config.models == ["m", "a", "b"]
    assert config.retries == 0
    assert CleanerConfig.from_env({}).models[0] == "deepseek-v4.1-flash-free"
