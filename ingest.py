#!/usr/bin/env python3
"""
pa-ingest: LAN endpoint + job worker that turns Podcast Addict downloads into transcripts.

HTTP (default port 8787):
  POST /ingest?name=<filename>[&show=<podcast>&token=<t>]
       body: multipart/form-data (any file part) OR raw audio bytes (application/octet-stream)
  GET  /health            -> JSON queue status
  GET  /transcripts       -> JSON list of what has been produced

Auth: shared token in the `X-PA-Token` header or `token` query param
      (set PA_TOKEN in config.env; if unset, auth is disabled -- LAN only either way).

Queue: files land in $PA_ROOT/inbox, a worker thread transcribes them one at a time
       (single-threaded on purpose: faster-whisper already uses every core).
"""
import email.parser
import email.policy
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from intake import Intake, UnsupportedAudio

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("PA_ROOT", os.path.expanduser("~/podcast-transcripts"))
OUT = os.environ.get("PA_OUTDIR", os.path.join(ROOT, "out"))
LOGDIR = os.path.join(ROOT, "logs")
LOG_FILE = os.path.join(LOGDIR, "ingest.log")
PYTHON = os.environ.get("PA_PYTHON") or os.path.join(CODE_DIR, ".venv", "bin", "python")
PORT = int(os.environ.get("PA_PORT", "8787"))
TOKEN = os.environ.get("PA_TOKEN", "")
MAX_BYTES = int(os.environ.get("PA_MAX_MB", "800")) * 1024 * 1024
KEEP_AUDIO = os.environ.get("PA_KEEP_AUDIO", "1") not in ("0", "false", "no")
REQUIRE_COMPLETED = os.environ.get("PA_REQUIRE_COMPLETED", "1").lower() not in ("0", "false", "no")
NOTIFY_TARGET = os.environ.get("PA_NOTIFY_TARGET", "telegram").strip()
HERMES = (os.environ.get("PA_NOTIFY_HERMES") or os.environ.get("PA_CLEAN_HERMES")
          or os.path.expanduser("~/.local/bin/hermes"))

worker_lock = threading.Lock()
postprocess_lock = threading.Lock()
postprocess_again = threading.Event()
state = {"received": 0, "transcribed": 0, "failed": 0, "last": None, "busy": False}


def log(*a):
    """Print to stdout (journal) AND append to logs/ingest.log so request history
    survives reboots - journald user logs here are volatile and get wiped."""
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def parse_multipart(content_type, body):
    """Return (filename, bytes) for the first file part of a multipart body."""
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    if not msg.is_multipart():
        return None, None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        fn = part.get_filename()
        payload = part.get_payload(decode=True)
        if payload:
            return fn, payload
    return None, None


def _header(headers, key, maxlen):
    return (headers.get(key) or "").strip()[:maxlen]


class Handler(BaseHTTPRequestHandler):
    """HTTP adapter over Intake. Bound to its collaborators by build_server()."""
    server_version = "pa-ingest/1.0"
    protocol_version = "HTTP/1.1"
    intake = None
    token = ""
    max_bytes = MAX_BYTES
    outdir = OUT
    on_queued = staticmethod(lambda: None)

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        log(f"{self.command} {self.path.split('?')[0]} -> {code} {json.dumps(obj)[:160]}")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self, q):
        if not self.token:
            return True
        return (self.headers.get("X-PA-Token") == self.token
                or q.get("token", [""])[0] == self.token)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self._authed(q):
            return self._json(401, {"error": "bad token"})
        if u.path == "/health":
            status = self.intake.status()
            return self._json(200, {"ok": True, "busy": state["busy"],
                                    "queue": status["queue"], "staged": status["staged"],
                                    "failed_files": status["failed"],
                                    "completion_required": self.intake.require_completed,
                                    **{k: state[k] for k in ("received", "transcribed", "failed", "last")}})
        if u.path == "/transcripts":
            idx = os.path.join(self.outdir, "index.jsonl")
            rows = []
            if os.path.exists(idx):
                with open(idx) as f:
                    for line in f:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
            return self._json(200, {"count": len(rows), "items": rows[-50:]})
        return self._json(404, {"error": "not found"})

    def _complete_staged(self, q):
        phone_path = (self.headers.get("X-PA-Path") or "").strip()
        raw_name = (q.get("name", [""])[0] or self.headers.get("X-PA-Name")
                    or os.path.basename(phone_path))
        done = self.intake.complete(raw_name,
                                    completed_at=self.headers.get("X-PA-Completed-At") or "",
                                    source=self.headers.get("X-PA-Completion-Source") or "")
        if done.status == "not_found":
            return self._json(404, {"error": "staged episode not found", "name": done.name})
        if done.status != "queued_after_completion":
            return self._json(200, {"status": done.status, "name": done.name})
        log(f"completion confirmed; queued {done.name} completed_at={done.meta['completed_at']}")
        self.on_queued()
        return self._json(202, {"status": done.status, "name": done.name,
                                "show": done.meta.get("show", ""),
                                "sha256": done.meta.get("sha256", ""),
                                "completed_at": done.meta["completed_at"]})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path not in ("/ingest", "/complete"):
            return self._json(404, {"error": "not found"})
        if not self._authed(q):
            return self._json(401, {"error": "bad token"})
        if u.path == "/complete":
            return self._complete_staged(q)

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._json(411, {"error": "Content-Length required"})
        if length > self.max_bytes:
            return self._json(413, {"error": f"too large (>{self.max_bytes//1048576} MB)"})

        ctype = self.headers.get("Content-Type", "")
        body = self.rfile.read(length)

        fname, payload = (None, None)
        if ctype.lower().startswith("multipart/form-data"):
            try:
                fname, payload = parse_multipart(ctype, body)
            except Exception as e:
                log("multipart parse failed:", e)
            if payload is None:
                return self._json(400, {"error": "no file part found in multipart body"})
        else:
            payload = body

        h = self.headers
        try:
            got = self.intake.accept(
                payload,
                raw_name=q.get("name", [""])[0] or h.get("X-PA-Name") or fname or "episode.mp3",
                show_hint=q.get("show", [""])[0] or h.get("X-PA-Show") or "",
                phone_path=(h.get("X-PA-Path") or "").strip(),
                original_name=fname,
                fields={
                    "source_app": _header(h, "X-PA-Source", 100) or "Podcast Addict",
                    "source_device": _header(h, "X-PA-Device", 100),
                    "feed_url": _header(h, "X-PA-Feed-URL", 2000),
                    "episode_url": _header(h, "X-PA-Episode-URL", 2000),
                    "published_at": _header(h, "X-PA-Published-At", 100),
                    "completed_at": _header(h, "X-PA-Completed-At", 100),
                })
        except UnsupportedAudio as e:
            return self._json(400, {"error": f"unsupported extension for {e}"})

        if got.status == "duplicate":
            log(f"duplicate upload ignored: {got.name} ({got.sha256[:12]})")
            return self._json(200, {"status": "duplicate", "sha256": got.sha256,
                                    "existing": got.existing})
        state["received"] += 1
        log(f"{got.status} {got.name} ({len(payload)/1048576:.1f} MB, "
            f"show={got.show or '?'}, sha={got.sha256[:12]})")
        if got.status == "queued":
            self.on_queued()
        return self._json(202, {"status": got.status, "name": got.name, "show": got.show,
                                "sha256": got.sha256,
                                "completion_required": self.intake.require_completed})

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")


def build_server(intake, *, token="", max_bytes=MAX_BYTES, port=PORT, host="0.0.0.0",
                 outdir=OUT, on_queued=lambda: None):
    handler = type("BoundHandler", (Handler,), {
        "intake": intake, "token": token, "max_bytes": max_bytes, "outdir": outdir,
        "on_queued": staticmethod(on_queued)})
    return ThreadingHTTPServer((host, port), handler)


def _flush_postprocess():
    cmd = [PYTHON, os.path.join(CODE_DIR, "postprocess.py"), "--flush"]
    os.makedirs(LOGDIR, exist_ok=True)
    with open(os.path.join(LOGDIR, "postprocess-worker.log"), "a") as lf:
        return subprocess.call(cmd, stdout=lf, stderr=subprocess.STDOUT)


def run_postprocess(flush=_flush_postprocess):
    """Drain AI cleaning + Hindsight retention without blocking the ASR worker.

    One drain runs at a time. A request that arrives while one is running makes it run
    once more afterwards, so a Transcript queued mid-drain is not left waiting.
    """
    postprocess_again.set()
    while postprocess_again.is_set():
        if not postprocess_lock.acquire(blocking=False):
            return  # the running drain will see the flag
        try:
            while postprocess_again.is_set():
                postprocess_again.clear()
                rc = flush()
                if rc:
                    log(f"postprocess pending rc={rc}; durable queue will retry")
        finally:
            postprocess_lock.release()


def run_transcribe(job):
    """Transcribe one job in a child process (frees the model's memory afterwards)."""
    cmd = [PYTHON, os.path.join(CODE_DIR, "transcribe.py"), job.audio_path,
           "--source-meta", job.meta_path, "--outdir", OUT, "--enqueue-postprocess"]
    if job.meta.get("show"):
        cmd += ["--show", job.meta["show"]]
    os.makedirs(LOGDIR, exist_ok=True)
    with open(os.path.join(LOGDIR, "worker.log"), "a") as lf:
        return subprocess.call(cmd, stdout=lf, stderr=subprocess.STDOUT)


def notify_failure(name, detail):
    """Tell the owner on their messaging platform that an Episode failed. Never raises."""
    if not NOTIFY_TARGET:
        return
    message = (f"Podcast transcription failed: {name} ({detail}). "
               "Download the episode again in Podcast Addict to retry. "
               f"The audio is kept as inbox/{name}.failed.")
    try:
        proc = subprocess.run([HERMES, "send", "--to", NOTIFY_TARGET, "-q", message],
                              capture_output=True, text=True, timeout=90)
        if proc.returncode:
            log(f"failure notification not sent rc={proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[-300:]}")
    except Exception as exc:
        log(f"failure notification not sent: {exc}")


def _postprocess_in_background():
    threading.Thread(target=run_postprocess, daemon=True).start()


def _transcribe_queue(intake, transcribe, notify, postprocess):
    """Work through the queue. Returns False if it had to stop on an unrecordable outcome."""
    while True:
        job = intake.next_job()
        if job is None:
            return True
        log(f"transcribing {job.name} ...")
        try:
            rc = transcribe(job)
        except Exception as exc:
            log(f"transcribe could not start for {job.name}: {exc}")
            rc = -1
        try:
            intake.finish(job, ok=(rc == 0))
        except Exception as exc:
            # The job is still in the inbox and would be picked again at once; stop
            # instead of looping, and say so. The next upload or restart retries it.
            state["last"] = {"file": job.name, "status": f"stuck: {exc}"}
            log(f"STUCK {job.name}: could not record outcome rc={rc}: {exc}")
            notify(job.name, f"could not record outcome: {exc}")
            return False
        if rc == 0:
            # transcribe.py has queued the Transcript for post-processing.
            postprocess()
            state["transcribed"] += 1
            state["last"] = {"file": job.name, "status": "ok",
                             "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        else:
            state["failed"] += 1
            state["last"] = {"file": job.name, "status": f"failed rc={rc}"}
            log(f"FAILED {job.name} rc={rc} (see {LOGDIR}/worker.log)")
            notify(job.name, f"rc={rc}")


def worker(intake, *, transcribe=run_transcribe, notify=notify_failure,
           postprocess=_postprocess_in_background):
    """Transcribe queued Episodes one at a time. Safe to call from any thread at any
    time: if a worker is already running, this returns and that one does the work."""
    while True:
        if not worker_lock.acquire(blocking=False):
            return
        try:
            state["busy"] = True
            drained = _transcribe_queue(intake, transcribe, notify, postprocess)
        finally:
            state["busy"] = False
            worker_lock.release()
        # An upload may have landed between the last empty look and the release.
        if not drained or intake.next_job() is None:
            return


def main():
    if not os.path.exists(PYTHON):
        log(f"WARNING: venv python missing at {PYTHON}; run setup first")
    os.makedirs(OUT, exist_ok=True)
    intake = Intake(ROOT, keep_audio=KEEP_AUDIO, require_completed=REQUIRE_COMPLETED)

    def start_worker():
        threading.Thread(target=worker, args=(intake,), daemon=True).start()

    log(f"pa-ingest listening on 0.0.0.0:{PORT}  root={ROOT}  out={OUT}  "
        f"auth={'on' if TOKEN else 'off'}  keep_audio={KEEP_AUDIO}")
    threading.Thread(target=run_postprocess, daemon=True).start()
    start_worker()
    build_server(intake, token=TOKEN, on_queued=start_worker).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
