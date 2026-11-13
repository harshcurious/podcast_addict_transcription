"""The backfill end to end, with the web, the AI cleaner, ASR and Hindsight replaced."""
import json

import publisher_transcripts as online
import pytest
from test_pa_backup import make_db

import backfill
import hindsight_save
from durable_queue import DurableQueue

VTT = "WEBVTT\n\n" + "\n\n".join(
    f"00:{i // 60:02d}:{i % 60:02d}.000 --> 00:{i // 60:02d}:{i % 60:02d}.900\n"
    + ("Buy MegaWidget now." if i == 3 else "talk " * 9 + "end.")
    for i in range(100))


class Hindsight:
    def __init__(self, status=200):
        self.status, self.requests = status, []

    def __call__(self, url, body, timeout):
        self.requests.append(json.loads(body))
        if self.status != 200:
            return self.status, '{"detail": "down"}'
        return 200, '{"success": true, "operation_id": "op"}'

    def documents(self):
        return [r["items"][0] for r in self.requests]


def web(pages):
    def get(url, **_):
        if url not in pages:
            raise OSError(f"404 {url}")
        return pages[url], ""
    return get


def ads_at_index(n):
    """A scripted classifier that flags every core segment containing 'MegaWidget'."""
    def ask(prompt, model):
        ids = [int(line.split()[1]) for line in prompt.splitlines()
               if line.startswith("[CORE ") and "MegaWidget" in line]
        return json.dumps({"ad_segment_ids": ids})
    return ask


def asr_stub(ep, recognizer, keep_audio):
    segments = [{"start": float(i), "end": float(i + 1), "text": "spoken words " * 5}
                for i in range(20)]
    return {**backfill.base_meta(ep), "file_bytes": 1, "audio_duration": 20.0,
            "model": "stub-asr", "language": "en", "transcribed_at": backfill.now(),
            "rtf": 1.0, "wall_seconds": 1.0, "audio_sha256": "a" * 64,
            "segments": len(segments), "words": 200, "transcript_source": "asr",
            "timing": "exact"}, segments


@pytest.fixture
def env(tmp_path, monkeypatch):
    logs = []
    for name, value in (("OUT", tmp_path / "out"), ("WORK", tmp_path / "work"),
                        ("STATE_DIR", tmp_path / "state"), ("log", logs.append)):
        monkeypatch.setattr(backfill, name, value)
    monkeypatch.setattr(hindsight_save, "log", logs.append)
    monkeypatch.setattr(hindsight_save, "QUEUE", DurableQueue(
        tmp_path / "hs-pending", tmp_path / "hs-receipts", logs.append))
    monkeypatch.setenv("PA_CLEAN_RETRIES", "0")
    monkeypatch.setenv("PA_CLEAN_FALLBACK_MODELS", "")
    monkeypatch.setattr(online, "http_get", web({
        "https://example.com/1.vtt": VTT.encode(),
        f"{hindsight_save.API_URL}/v1/default/banks/{hindsight_save.BANK_ID}"
        f"/documents?limit=100&offset=0": json.dumps({"total": 1, "items": [{
            "id": "podcast:" + "e" * 64, "retain_params": {"metadata": {
                "show": "Show", "title": "Completed, not marked"}}}]}).encode(),
    }))
    monkeypatch.setattr(backfill, "from_audio", asr_stub)
    hindsight = Hindsight()
    monkeypatch.setattr(hindsight_save, "http_post", hindsight)
    monkeypatch.setattr(backfill, "cleaner_backend", lambda config: ("stub", ads_at_index(3)))
    make_db(tmp_path / "podcastAddict.db")
    return tmp_path, hindsight, logs


def run(tmp_path, *extra):
    return backfill.main(backfill.parse_args([str(tmp_path / "podcastAddict.db"),
                                              "--env-file", "/nonexistent", *extra]))


def states(tmp_path):
    records = backfill.State(tmp_path / "state").all()
    return {r["episode"]["title"]: r for r in records}


def test_every_listened_episode_ends_cleaned_in_hindsight_once(env):
    tmp_path, hindsight, _ = env
    assert run(tmp_path) == 0

    s = states(tmp_path)
    assert s["Played early"]["status"] == "retained"
    assert s["Played early"]["source"] == "publisher"
    assert s["Played late"]["status"] == "retained" and s["Played late"]["source"] == "asr"
    assert s["Completed, not marked"]["status"] == "pi-pipeline"  # the Pi already has it

    docs = {d["metadata"]["title"]: d for d in hindsight.documents()}
    assert set(docs) == {"Played early", "Played late"}
    publisher = docs["Played early"]
    assert publisher["document_id"] == f"podcast:episode:{s['Played early']['key']}"
    assert "source:backfill" in publisher["tags"]
    assert "transcribed by the podcast's publisher" in publisher["content"]
    assert "MegaWidget" not in publisher["content"]
    assert publisher["metadata"]["transcript_url"] == "https://example.com/1.vtt"
    assert publisher["metadata"]["ads_segments_removed"] == "1"
    assert "transcribed by ASR" in docs["Played late"]["content"]

    sent = len(hindsight.requests)
    assert run(tmp_path) == 0  # a second run has nothing left to do
    assert len(hindsight.requests) == sent


def test_cleaning_failure_never_reaches_hindsight_and_flush_finishes_it(env, monkeypatch):
    tmp_path, hindsight, _ = env

    def down(prompt, model):
        raise RuntimeError("endpoint down")
    monkeypatch.setattr(backfill, "cleaner_backend", lambda config: ("stub", down))
    run(tmp_path)
    s = states(tmp_path)
    assert s["Played early"]["status"] == "transcribed"
    assert "ad cleaning failed" in s["Played early"]["error"]
    assert hindsight.requests == []

    monkeypatch.setattr(backfill, "cleaner_backend", lambda config: ("stub", ads_at_index(3)))
    backfill.main(backfill.parse_args(["--flush", "--env-file", "/nonexistent"]))
    assert {r["status"] for r in states(tmp_path).values()} == {"retained", "pi-pipeline"}


def test_hindsight_outage_keeps_requests_queued_until_delivered(env, monkeypatch):
    tmp_path, hindsight, _ = env
    hindsight.status = 503
    run(tmp_path, "--episode-id", "1")
    record = states(tmp_path)["Played early"]
    assert record["status"] == "transcribed"
    assert (tmp_path / "hs-pending" / record["hindsight_pending"]).exists()

    hindsight.status = 200
    backfill.main(backfill.parse_args(["--flush", "--env-file", "/nonexistent"]))
    assert states(tmp_path)["Played early"]["status"] == "retained"


def test_online_only_leaves_episodes_without_a_transcript_waiting(env):
    tmp_path, hindsight, _ = env
    run(tmp_path, "--online-only")
    s = states(tmp_path)
    assert s["Played late"]["status"] == "waiting"
    assert s["Played early"]["status"] == "retained"


def test_failed_episodes_are_retried_until_max_attempts(env, monkeypatch):
    tmp_path, _, _ = env

    def broken(ep, recognizer, keep_audio):
        raise RuntimeError("HTTP 404")
    monkeypatch.setattr(backfill, "from_audio", broken)
    for _ in range(4):
        run(tmp_path, "--episode-id", "2", "--max-attempts", "3")
    record = states(tmp_path)["Played late"]
    assert record["status"] == "failed" and record["attempts"] == 3


def test_dry_run_changes_nothing(env, capsys):
    tmp_path, hindsight, _ = env
    run(tmp_path, "--dry-run")
    out = capsys.readouterr().out
    assert "1 with an online transcript link" in out and "2 to transcribe locally" in out
    assert states(tmp_path) == {} and hindsight.requests == []


def test_sigterm_stops_like_ctrl_c_and_a_second_one_is_not_caught():
    import os
    import signal
    previous = signal.getsignal(signal.SIGTERM)
    try:
        backfill.stop_gracefully_on_sigterm()
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGTERM)
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, previous)
