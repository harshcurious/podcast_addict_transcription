import json
import threading
import urllib.error
import urllib.request

import pytest

import ingest
from intake import Intake


@pytest.fixture
def server(tmp_path):
    queued = []
    intake = Intake(tmp_path / "root")
    srv = ingest.build_server(intake, token="secret", port=0, host="127.0.0.1",
                              outdir=str(tmp_path / "out"),
                              on_queued=lambda: queued.append(1))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", intake, queued
    srv.shutdown()
    srv.server_close()


def call(url, *, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_upload_then_duplicate_then_health(server):
    base, intake, queued = server
    headers = {"X-PA-Token": "secret", "X-PA-Name": "ep.mp3", "X-PA-Show": "Demo",
               "X-PA-Device": "Pixel 9"}
    code, body = call(base + "/ingest", data=b"audio", headers=headers)
    assert code == 202
    assert body["status"] == "queued"
    assert body["name"] == "ep.mp3"
    assert body["show"] == "Demo"
    assert body["completion_required"] is False
    assert queued == [1]
    assert intake.next_job().meta["source_device"] == "Pixel 9"
    assert intake.next_job().meta["source_app"] == "Podcast Addict"

    code, again = call(base + "/ingest", data=b"audio", headers=headers)
    assert (code, again["status"], again["sha256"]) == (200, "duplicate", body["sha256"])
    assert queued == [1]

    code, health = call(base + "/health", headers={"X-PA-Token": "secret"})
    assert code == 200
    assert health["ok"] is True
    assert health["queue"] == ["ep.mp3"]


def test_rejections(server):
    base, intake, _ = server
    assert call(base + "/ingest", data=b"audio", headers={"X-PA-Name": "ep.mp3"})[0] == 401
    assert call(base + "/health")[0] == 401
    ok = {"X-PA-Token": "secret"}
    code, body = call(base + "/ingest", data=b"x", headers={**ok, "X-PA-Name": "notes.txt"})
    assert (code, body["error"]) == (400, "unsupported extension for notes.txt")
    assert call(base + "/nope", data=b"x", headers=ok)[0] == 404
    assert call(base + "/complete?name=ghost.mp3", data=b"", headers=ok)[0] == 404
    assert intake.status()["queue"] == []


def test_worker_archives_success_and_reports_failure(tmp_path):
    intake = Intake(tmp_path)
    intake.accept(b"good", raw_name="a-good.mp3")
    intake.accept(b"bad", raw_name="b-bad.mp3")
    notified = []

    ingest.worker(intake,
                  transcribe=lambda job: 0 if job.name == "a-good.mp3" else 3,
                  notify=lambda name, detail: notified.append((name, detail)),
                  postprocess=lambda path: None)

    assert (tmp_path / "archive" / "a-good.mp3").exists()
    assert (tmp_path / "inbox" / "b-bad.mp3.failed").exists()
    assert notified == [("b-bad.mp3", "rc=3")]
    assert intake.next_job() is None
    assert intake.accept(b"bad", raw_name="b-bad.mp3").status == "queued"
    assert ingest.state["busy"] is False


def test_worker_survives_a_transcriber_that_cannot_start(tmp_path):
    intake = Intake(tmp_path)
    intake.accept(b"x", raw_name="ep.mp3")
    notified = []

    def boom(job):
        raise OSError("no interpreter")

    ingest.worker(intake, transcribe=boom,
                  notify=lambda name, detail: notified.append(name),
                  postprocess=lambda path: None)
    assert notified == ["ep.mp3"]
    assert intake.status()["failed"] == ["ep.mp3"]


def test_notification_is_skipped_without_a_target_and_never_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(ingest.subprocess, "run", lambda *a, **k: calls.append(a) or 1 / 0)
    monkeypatch.setattr(ingest, "NOTIFY_TARGET", "")
    ingest.notify_failure("ep.mp3", "rc=1")
    assert calls == []
    monkeypatch.setattr(ingest, "NOTIFY_TARGET", "telegram")
    ingest.notify_failure("ep.mp3", "rc=1")
    assert calls[0][0][:4] == [ingest.HERMES, "send", "--to", "telegram"]
