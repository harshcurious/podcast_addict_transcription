import json

from durable_queue import DurableQueue


def make(tmp_path, log=None):
    return DurableQueue(tmp_path / "pending", tmp_path / "receipts",
                        (log if log is not None else []).append)


def names(directory):
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


def test_enqueue_persists_the_job_and_leaves_no_temp_file(tmp_path):
    q = make(tmp_path)
    path = q.enqueue("a.json", {"n": 1, "title": "café"})
    assert names(tmp_path / "pending") == ["a.json"]
    assert json.loads(path.read_text(encoding="utf-8")) == {"n": 1, "title": "café"}


def test_enqueue_under_the_same_name_replaces_the_job(tmp_path):
    q = make(tmp_path)
    q.enqueue("a.json", {"n": 1})
    q.enqueue("a.json", {"n": 2})
    seen = []
    assert q.drain(lambda job: seen.append(job) or {"ok": True}) == 0
    assert seen == [{"n": 2}]


def test_handled_job_becomes_a_receipt(tmp_path):
    q = make(tmp_path)
    q.enqueue("a.json", {"n": 1})
    assert q.drain(lambda job: {"done": job["n"]}) == 0
    assert names(tmp_path / "pending") == []
    assert json.loads((tmp_path / "receipts" / "a.json").read_text()) == {"done": 1}


def test_declined_job_stays_pending_and_is_offered_again(tmp_path):
    q = make(tmp_path)
    q.enqueue("a.json", {"n": 1})
    assert q.drain(lambda job: None) == 1
    assert names(tmp_path / "pending") == ["a.json"]
    assert names(tmp_path / "receipts") == []
    assert q.drain(lambda job: {"ok": True}) == 0


def test_a_crashing_handler_keeps_its_job_and_does_not_block_the_rest(tmp_path):
    log = []
    q = make(tmp_path, log)
    q.enqueue("a.json", {"n": 1})
    q.enqueue("b.json", {"n": 2})

    def handler(job):
        if job["n"] == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    assert q.drain(handler) == 1
    assert names(tmp_path / "pending") == ["a.json"]
    assert names(tmp_path / "receipts") == ["b.json"]
    assert "a.json" in log[0] and "boom" in log[0]


def test_unreadable_job_file_is_kept_and_reported(tmp_path):
    log = []
    q = make(tmp_path, log)
    (tmp_path / "pending").mkdir()
    (tmp_path / "pending" / "bad.json").write_text("{nope")
    assert q.drain(lambda job: {"ok": True}) == 1
    assert names(tmp_path / "pending") == ["bad.json"]
    assert len(log) == 1


def test_jobs_left_by_a_previous_process_are_drained(tmp_path):
    make(tmp_path).enqueue("a.json", {"n": 1})
    (tmp_path / "pending" / "half-written.tmp").write_text("{")
    restarted = make(tmp_path)
    assert [p.name for p in restarted.pending()] == ["a.json"]
    assert restarted.drain(lambda job: {"ok": True}) == 0


def test_draining_an_empty_queue_is_fine(tmp_path):
    assert make(tmp_path).drain(lambda job: {"ok": True}) == 0
