import json
import os
import threading

import pytest

from intake import Intake, UnsupportedAudio, safe_name, show_from_name, show_from_path

PHONE = "/storage/emulated/0/Android/media/com.bambuna.podcastaddict/podcast/Demo Show/ep.mp3"


def ledger(root):
    path = root / "seen.json"
    return json.loads(path.read_text()) if path.exists() else {}


def test_name_and_show_helpers():
    assert safe_name("../Morning Brew - Episode?.mp3") == "Morning Brew - Episode_.mp3"
    assert safe_name("") == "episode.mp3"
    assert safe_name("no-extension") == "no-extension.mp3"
    assert show_from_path(PHONE) == "Demo Show"
    assert show_from_path("/x/podcast/ep.mp3") == ""
    assert show_from_name("Some Show - Episode 1.mp3") == "Some Show"
    assert show_from_name("abc123.mp3") == ""


def test_accept_queues_audio_with_sidecar(tmp_path):
    intake = Intake(tmp_path, now=lambda: "2026-01-01T00:00:00")
    got = intake.accept(b"audio", raw_name="ep.mp3", phone_path=PHONE,
                        fields={"source_device": "Pixel"})
    assert (got.status, got.name, got.show) == ("queued", "ep.mp3", "Demo Show")
    assert (tmp_path / "inbox" / "ep.mp3").read_bytes() == b"audio"
    meta = json.loads((tmp_path / "inbox" / "ep.mp3.json").read_text())
    assert meta["sha256"] == got.sha256
    assert meta["bytes"] == 5
    assert meta["source_path"] == PHONE
    assert meta["source_device"] == "Pixel"
    assert meta["received_at"] == "2026-01-01T00:00:00"
    assert ledger(tmp_path) == {got.sha256: {"name": "ep.mp3", "at": "2026-01-01T00:00:00"}}
    assert intake.status() == {"queue": ["ep.mp3"], "staged": [], "failed": []}


def test_same_bytes_are_a_duplicate_even_under_another_name(tmp_path):
    intake = Intake(tmp_path)
    first = intake.accept(b"audio", raw_name="a.mp3")
    again = intake.accept(b"audio", raw_name="b.mp3")
    assert again.status == "duplicate"
    assert again.existing["name"] == "a.mp3"
    assert again.sha256 == first.sha256
    assert intake.status()["queue"] == ["a.mp3"]


def test_non_audio_name_is_rejected_and_leaves_nothing(tmp_path):
    intake = Intake(tmp_path)
    with pytest.raises(UnsupportedAudio):
        intake.accept(b"x", raw_name="notes.txt")
    assert os.listdir(tmp_path / "inbox") == []
    assert ledger(tmp_path) == {}


def test_show_falls_back_from_hint_to_path_to_name(tmp_path):
    intake = Intake(tmp_path)
    assert intake.accept(b"1", raw_name="a.mp3", show_hint=" Hinted ",
                         phone_path=PHONE).show == "Hinted"
    assert intake.accept(b"2", raw_name="b.mp3", phone_path=PHONE).show == "Demo Show"
    assert intake.accept(b"3", raw_name="My Show - Ep.mp3").show == "My Show"


def test_staged_episode_waits_for_completion(tmp_path):
    intake = Intake(tmp_path, require_completed=True, now=lambda: "T")
    got = intake.accept(b"audio", raw_name="ep.mp3", show_hint="S")
    assert got.status == "staged_until_completion"
    assert intake.next_job() is None
    assert intake.status()["staged"] == ["ep.mp3"]

    done = intake.complete("ep.mp3", completed_at="2026-02-02", source="")
    assert done.status == "queued_after_completion"
    assert done.meta["completed_at"] == "2026-02-02"
    assert done.meta["completion_source"] == "Tasker"
    assert done.meta["sha256"] == got.sha256
    assert intake.next_job().name == "ep.mp3"
    assert os.listdir(tmp_path / "staged") == []

    assert intake.complete("ep.mp3").status == "already_queued"
    assert intake.complete("never-seen.mp3").status == "not_found"


def test_complete_after_processing_reports_already_processed(tmp_path):
    intake = Intake(tmp_path)
    intake.accept(b"audio", raw_name="ep.mp3")
    intake.finish(intake.next_job(), ok=True)
    assert intake.complete("ep.mp3").status == "already_processed"


def test_jobs_come_in_name_order_with_their_sidecar(tmp_path):
    intake = Intake(tmp_path)
    intake.accept(b"2", raw_name="b.mp3", show_hint="B")
    intake.accept(b"1", raw_name="a.mp3", show_hint="A")
    job = intake.next_job()
    assert (job.name, job.meta["show"]) == ("a.mp3", "A")
    assert job.audio_path == str(tmp_path / "inbox" / "a.mp3")
    assert job.meta_path == job.audio_path + ".json"


@pytest.mark.parametrize("keep", [True, False])
def test_successful_job_is_archived_or_deleted(tmp_path, keep):
    intake = Intake(tmp_path, keep_audio=keep)
    got = intake.accept(b"audio", raw_name="ep.mp3")
    intake.finish(intake.next_job(), ok=True)
    assert os.listdir(tmp_path / "inbox") == []
    assert (tmp_path / "archive" / "ep.mp3").exists() is keep
    assert intake.next_job() is None
    # A transcribed Episode stays deduplicated.
    assert got.sha256 in ledger(tmp_path)
    assert intake.accept(b"audio", raw_name="ep.mp3").status == "duplicate"


def test_failed_episode_can_be_uploaded_again(tmp_path):
    intake = Intake(tmp_path)
    intake.accept(b"audio", raw_name="ep.mp3")
    intake.accept(b"other", raw_name="other.mp3")
    intake.finish(intake.next_job(), ok=False)

    assert (tmp_path / "inbox" / "ep.mp3.failed").read_bytes() == b"audio"
    assert not (tmp_path / "inbox" / "ep.mp3.json").exists()
    assert intake.status() == {"queue": ["other.mp3"], "staged": [], "failed": ["ep.mp3"]}
    assert [e["name"] for e in ledger(tmp_path).values()] == ["other.mp3"]

    retry = intake.accept(b"audio", raw_name="ep.mp3")
    assert retry.status == "queued"
    assert intake.next_job().name == "ep.mp3"


def test_failure_releases_digest_even_when_sidecar_was_unreadable(tmp_path):
    intake = Intake(tmp_path)
    intake.accept(b"audio", raw_name="ep.mp3")
    (tmp_path / "inbox" / "ep.mp3.json").write_text("{not json")
    intake.finish(intake.next_job(), ok=False)
    assert ledger(tmp_path) == {}


def test_concurrent_uploads_all_reach_the_ledger(tmp_path):
    intake = Intake(tmp_path)
    threads = [threading.Thread(target=intake.accept, args=(str(i).encode(),),
                                kwargs={"raw_name": f"ep{i}.mp3"}) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ledger(tmp_path)) == 20
    assert len(intake.status()["queue"]) == 20


def test_existing_ledger_and_queue_survive_a_restart(tmp_path):
    first = Intake(tmp_path)
    first.accept(b"audio", raw_name="ep.mp3")
    second = Intake(tmp_path)
    assert second.accept(b"audio", raw_name="ep.mp3").status == "duplicate"
    assert second.next_job().name == "ep.mp3"
