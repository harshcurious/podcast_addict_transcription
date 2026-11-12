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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("PA_ROOT", os.path.expanduser("~/podcast-transcripts"))
INBOX = os.path.join(ROOT, "inbox")
STAGED = os.path.join(ROOT, "staged")
OUT = os.environ.get("PA_OUTDIR", os.path.join(ROOT, "out"))
ARCHIVE = os.path.join(ROOT, "archive")
LOGDIR = os.path.join(ROOT, "logs")
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python")
PORT = int(os.environ.get("PA_PORT", "8787"))
TOKEN = os.environ.get("PA_TOKEN", "")
MAX_BYTES = int(os.environ.get("PA_MAX_MB", "800")) * 1024 * 1024
KEEP_AUDIO = os.environ.get("PA_KEEP_AUDIO", "1") not in ("0", "false", "no")
REQUIRE_COMPLETED = os.environ.get("PA_REQUIRE_COMPLETED", "1").lower() not in ("0", "false", "no")
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".mp4", ".m4b", ".wma", ".webm"}

for d in (INBOX, STAGED, OUT, ARCHIVE, LOGDIR):
    os.makedirs(d, exist_ok=True)

LOG_FILE = os.path.join(LOGDIR, "ingest.log")

worker_lock = threading.Lock()
postprocess_lock = threading.Lock()
state = {"received": 0, "transcribed": 0, "failed": 0, "last": None, "busy": False}


def log(*a):
    """Print to stdout (journal) AND append to logs/ingest.log so request history
    survives reboots - journald user logs here are volatile and get wiped."""
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def safe_name(raw):
    name = os.path.basename(urllib.parse.unquote(raw or "")).strip()
    name = re.sub(r"[^\w\s.\-()&',\[\]+]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "episode.mp3"
    if not os.path.splitext(name)[1]:
        name += ".mp3"
    return name[:180]


def show_from_name(name):
    """Podcast Addict names files either '<Show> - <Episode>.mp3' or the remote filename."""
    stem = os.path.splitext(name)[0]
    m = re.match(r"^(.+?)\s+[-–]\s+.+$", stem)
    if m and len(m.group(1)) < 80:
        return m.group(1).strip()
    return ""


def show_from_path(phone_path):
    """Derive the show folder from Podcast Addict's .../podcast/<show>/<file> path."""
    if not phone_path:
        return ""
    normalized = urllib.parse.unquote(phone_path).replace("\\", "/").rstrip("/")
    parent = os.path.basename(os.path.dirname(normalized)).strip()
    return parent[:160] if parent.lower() not in ("", "podcast") else ""


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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


class Handler(BaseHTTPRequestHandler):
    server_version = "pa-ingest/1.0"
    protocol_version = "HTTP/1.1"

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        log(f"{self.command} {self.path.split('?')[0]} -> {code} {json.dumps(obj)[:160]}")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self, q):
        if not TOKEN:
            return True
        return self.headers.get("X-PA-Token") == TOKEN or q.get("token", [""])[0] == TOKEN

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self._authed(q):
            return self._json(401, {"error": "bad token"})
        if u.path == "/health":
            pending = [f for f in os.listdir(INBOX) if not f.endswith((".json", ".done", ".part"))]
            staged = [f for f in os.listdir(STAGED) if not f.endswith((".json", ".part"))]
            return self._json(200, {"ok": True, "busy": state["busy"], "queue": sorted(pending),
                                    "staged": sorted(staged), "completion_required": REQUIRE_COMPLETED,
                                    **{k: state[k] for k in ("received", "transcribed", "failed", "last")}})
        if u.path == "/transcripts":
            idx = os.path.join(OUT, "index.jsonl")
            rows = []
            if os.path.exists(idx):
                for line in open(idx):
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
        name = safe_name(raw_name)
        staged_audio = os.path.join(STAGED, name)
        staged_meta = staged_audio + ".json"
        if not os.path.exists(staged_audio):
            if os.path.exists(os.path.join(INBOX, name)):
                return self._json(200, {"status": "already_queued", "name": name})
            if os.path.exists(os.path.join(ARCHIVE, name)):
                return self._json(200, {"status": "already_processed", "name": name})
            return self._json(404, {"error": "staged episode not found", "name": name})

        meta = {}
        try:
            meta = json.load(open(staged_meta))
        except Exception:
            pass
        meta["completed_at"] = ((self.headers.get("X-PA-Completed-At") or "").strip()
                                or time.strftime("%Y-%m-%dT%H:%M:%S"))
        meta["completion_source"] = ((self.headers.get("X-PA-Completion-Source") or "").strip()
                                     or "Tasker")[:100]
        inbox_audio = os.path.join(INBOX, name)
        inbox_meta = inbox_audio + ".json"
        os.replace(staged_audio, inbox_audio)
        with open(inbox_meta, "w") as f:
            json.dump(meta, f, indent=2)
        try:
            os.unlink(staged_meta)
        except OSError:
            pass
        log(f"completion confirmed; queued {name} completed_at={meta['completed_at']}")
        threading.Thread(target=worker, daemon=True).start()
        return self._json(202, {"status": "queued_after_completion", "name": name,
                                "show": meta.get("show", ""),
                                "sha256": meta.get("sha256", ""),
                                "completed_at": meta["completed_at"]})

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
        if length > MAX_BYTES:
            return self._json(413, {"error": f"too large (>{MAX_BYTES//1048576} MB)"})

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

        name = safe_name(q.get("name", [""])[0]
                         or self.headers.get("X-PA-Name")
                         or fname or "episode.mp3")
        if os.path.splitext(name)[1].lower() not in AUDIO_EXT:
            return self._json(400, {"error": f"unsupported extension for {name}"})

        phone_path = (self.headers.get("X-PA-Path") or "").strip()
        show = (q.get("show", [""])[0] or self.headers.get("X-PA-Show") or "").strip() \
            or show_from_path(phone_path) or show_from_name(name)

        target_dir = STAGED if REQUIRE_COMPLETED else INBOX
        tmp = os.path.join(target_dir, name + ".part")
        with open(tmp, "wb") as f:
            f.write(payload)
        digest = sha256_of(tmp)

        seen_file = os.path.join(ROOT, "seen.json")
        seen = {}
        if os.path.exists(seen_file):
            try:
                seen = json.load(open(seen_file))
            except Exception:
                seen = {}
        if digest in seen:
            os.unlink(tmp)
            log(f"duplicate upload ignored: {name} ({digest[:12]})")
            return self._json(200, {"status": "duplicate", "sha256": digest,
                                     "existing": seen[digest]})

        dest = os.path.join(target_dir, name)
        os.replace(tmp, dest)
        meta = {
            "sha256": digest,
            "show": show,
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "bytes": len(payload),
            "original_name": fname or name,
            "source_path": phone_path,
            "source_app": (self.headers.get("X-PA-Source") or "Podcast Addict").strip()[:100],
            "source_device": (self.headers.get("X-PA-Device") or "").strip()[:100],
            "feed_url": (self.headers.get("X-PA-Feed-URL") or "").strip()[:2000],
            "episode_url": (self.headers.get("X-PA-Episode-URL") or "").strip()[:2000],
            "published_at": (self.headers.get("X-PA-Published-At") or "").strip()[:100],
            "completed_at": (self.headers.get("X-PA-Completed-At") or "").strip()[:100],
        }
        with open(os.path.join(target_dir, name + ".json"), "w") as f:
            json.dump(meta, f)
        seen[digest] = {"name": name, "at": meta["received_at"]}
        json.dump(seen, open(seen_file, "w"), indent=1)

        state["received"] += 1
        status = "staged_until_completion" if REQUIRE_COMPLETED else "queued"
        log(f"{status} {name} ({len(payload)/1048576:.1f} MB, show={show or '?'}, sha={digest[:12]})")
        if not REQUIRE_COMPLETED:
            threading.Thread(target=worker, daemon=True).start()
        return self._json(202, {"status": status, "name": name, "show": show,
                                "sha256": digest, "completion_required": REQUIRE_COMPLETED})

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")


def run_postprocess(transcript_json=None):
    """Run AI cleaning + Hindsight retention without blocking the ASR worker."""
    if not postprocess_lock.acquire(blocking=False):
        return
    try:
        cmd = [VENV_PY, os.path.join(ROOT, "postprocess.py")]
        cmd += [transcript_json] if transcript_json else ["--flush"]
        with open(os.path.join(LOGDIR, "postprocess-worker.log"), "a") as lf:
            rc = subprocess.call(cmd, stdout=lf, stderr=subprocess.STDOUT)
        if rc:
            log(f"postprocess pending rc={rc}; durable queue will retry")
    finally:
        postprocess_lock.release()


def worker():
    if not worker_lock.acquire(blocking=False):
        return
    try:
        state["busy"] = True
        while True:
            jobs = sorted(f for f in os.listdir(INBOX)
                          if not f.endswith((".json", ".done", ".part", ".failed"))
                          and os.path.splitext(f)[1].lower() in AUDIO_EXT)
            if not jobs:
                return
            name = jobs[0]
            path = os.path.join(INBOX, name)
            meta_path = path + ".json"
            meta = {}
            try:
                meta = json.load(open(meta_path))
            except Exception:
                pass
            log(f"transcribing {name} ...")
            cmd = [VENV_PY, os.path.join(ROOT, "transcribe.py"), path,
                   "--source-meta", meta_path,
                   "--outdir", OUT]
            if meta.get("show"):
                cmd += ["--show", meta["show"]]
            with open(os.path.join(LOGDIR, "worker.log"), "a") as lf:
                rc = subprocess.call(cmd, stdout=lf, stderr=subprocess.STDOUT)
            if rc == 0:
                # transcribe.py enriches the sidecar with exact artifact paths.
                try:
                    meta = json.load(open(meta_path))
                except Exception:
                    pass
                transcript_json = meta.get("transcript_json")
                if transcript_json:
                    threading.Thread(target=run_postprocess, args=(transcript_json,),
                                     daemon=True).start()
                open(path + ".done", "w").close()
                state["transcribed"] += 1
                state["last"] = {"file": name, "status": "ok",
                                 "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                try:
                    os.unlink(meta_path)
                except OSError:
                    pass
                if KEEP_AUDIO:
                    shutil.move(path, os.path.join(ARCHIVE, name))
                else:
                    os.unlink(path)
                try:
                    os.unlink(path + ".done")
                except OSError:
                    pass
            else:
                state["failed"] += 1
                state["last"] = {"file": name, "status": f"failed rc={rc}"}
                log(f"FAILED {name} rc={rc} (see {LOGDIR}/worker.log)")
                os.replace(path, path + ".failed")
                try:
                    os.unlink(meta_path)
                except OSError:
                    pass
    finally:
        state["busy"] = False
        worker_lock.release()


def main():
    if not os.path.exists(VENV_PY):
        log(f"WARNING: venv python missing at {VENV_PY}; run setup first")
    log(f"pa-ingest listening on 0.0.0.0:{PORT}  root={ROOT}  out={OUT}  "
        f"auth={'on' if TOKEN else 'off'}  keep_audio={KEEP_AUDIO}")
    threading.Thread(target=run_postprocess, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
