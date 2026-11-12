import json
import threading

import pytest

import hindsight_save
import ingest
import postprocess
from durable_queue import DurableQueue
from transcript import Transcript


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Both retry queues on a private directory, and one queued Transcript."""
    logs = []
    monkeypatch.setattr(postprocess, "log", logs.append)
    monkeypatch.setattr(hindsight_save, "log", logs.append)
    monkeypatch.setattr(postprocess, "QUEUE", DurableQueue(
        tmp_path / "pp-pending", tmp_path / "pp-receipts", logs.append))
    monkeypatch.setattr(hindsight_save, "QUEUE", DurableQueue(
        tmp_path / "hs-pending", tmp_path / "hs-receipts", logs.append))
    monkeypatch.setenv("PA_CLEAN_RETRIES", "0")
    monkeypatch.setenv("PA_CLEAN_FALLBACK_MODELS", "")

    segments = [{"start": float(i), "end": float(i + 1), "text": text} for i, text in
                enumerate(["Welcome back.", "Buy MegaWidget today!", "Now, the interview."])]
    transcript = Transcript(tmp_path / "out" / "ep.json",
                            {"title": "Ep", "show": "Show", "audio_sha256": "f" * 64}, segments)
    transcript.save_raw()
    postprocess.queue(transcript.json_path)
    return tmp_path, transcript


def names(directory):
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


def model_says(ids):
    return lambda prompt, model: json.dumps({"ad_segment_ids": ids})


def model_down(prompt, model):
    raise RuntimeError("endpoint down")


class Hindsight:
    def __init__(self, status=200, body='{"success": true, "operation_id": "op-1"}'):
        self.status, self.body, self.requests = status, body, []

    def __call__(self, url, body, timeout):
        self.requests.append((url, json.loads(body)))
        return self.status, self.body


def test_transcript_is_cleaned_then_retained_and_both_receipts_written(pipeline):
    root, transcript = pipeline
    hindsight = Hindsight()
    assert postprocess.flush(ask_model=model_says([1]), post=hindsight) == 0

    (url, request), = hindsight.requests
    assert url.endswith("/banks/podcast-addict/memories")
    item = request["items"][0]
    assert item["document_id"] == "podcast:" + "f" * 64
    assert "Welcome back. Now, the interview." in item["content"]
    assert "MegaWidget" not in item["content"]
    assert item["content"].startswith("EXTERNAL SOURCE MATERIAL")
    assert item["metadata"]["ads_segments_removed"] == "1"
    assert item["metadata"]["transcript_path"] == str(transcript.clean_path)

    assert names(root / "pp-pending") == [] and names(root / "hs-pending") == []
    receipt = json.loads((root / "pp-receipts" / ("f" * 64 + ".json")).read_text())
    assert receipt["clean_transcript"] == str(transcript.clean_path)
    assert receipt["ads_manifest"] == str(transcript.ads_path)
    assert len(names(root / "hs-receipts")) == 1


def test_nothing_is_sent_to_hindsight_when_cleaning_fails(pipeline):
    root, transcript = pipeline
    hindsight = Hindsight()
    assert postprocess.flush(ask_model=model_down, post=hindsight) == 1
    assert hindsight.requests == []
    assert not transcript.clean_path.exists()
    assert names(root / "hs-pending") == []
    assert len(names(root / "pp-pending")) == 1
    assert names(root / "pp-receipts") == []


def test_hindsight_outage_keeps_the_job_and_the_retry_reuses_the_clean_transcript(pipeline):
    root, transcript = pipeline
    assert postprocess.flush(ask_model=model_says([1]), post=Hindsight(status=500)) == 1
    assert transcript.clean_path.exists()
    assert len(names(root / "pp-pending")) == 1
    assert len(names(root / "hs-pending")) == 1

    hindsight = Hindsight()
    assert postprocess.flush(ask_model=model_down, post=hindsight) == 0  # model not needed
    assert len(hindsight.requests) == 1
    assert names(root / "pp-pending") == [] and names(root / "hs-pending") == []


@pytest.mark.parametrize("reply", [(200, "not json"), (200, '{"success": false}')])
def test_unconvincing_hindsight_reply_is_not_a_receipt(pipeline, reply):
    root, _ = pipeline
    assert postprocess.flush(ask_model=model_says([]), post=Hindsight(*reply)) == 1
    assert names(root / "hs-receipts") == [] and names(root / "pp-receipts") == []


def test_unreachable_hindsight_keeps_the_job(pipeline):
    root, _ = pipeline

    def refused(url, body, timeout):
        raise ConnectionRefusedError("no hindsight")

    assert postprocess.flush(ask_model=model_says([]), post=refused) == 1
    assert len(names(root / "hs-pending")) == 1


def test_missing_transcript_keeps_the_job_pending(pipeline):
    root, transcript = pipeline
    transcript.json_path.unlink()
    hindsight = Hindsight()
    assert postprocess.flush(ask_model=model_says([]), post=hindsight) == 1
    assert hindsight.requests == []
    assert len(names(root / "pp-pending")) == 1


def test_raw_transcript_files_are_never_rewritten(pipeline):
    _, transcript = pipeline
    raw = {p: p.read_bytes() for p in (transcript.json_path, transcript.md_path,
                                       transcript.srt_path)}
    postprocess.flush(ask_model=model_says([1]), post=Hindsight())
    assert {p: p.read_bytes() for p in raw} == raw


def test_a_drain_requested_while_one_is_running_runs_once_more():
    started, release, runs = threading.Event(), threading.Event(), []

    def slow_flush():
        runs.append(1)
        started.set()
        release.wait(5)
        return 0

    first = threading.Thread(target=ingest.run_postprocess, args=(slow_flush,))
    first.start()
    assert started.wait(5)
    ingest.run_postprocess(slow_flush)  # returns at once: a drain is already running
    assert runs == [1]
    release.set()
    first.join(5)
    assert runs == [1, 1]
