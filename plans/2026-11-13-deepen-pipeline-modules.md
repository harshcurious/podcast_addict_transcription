# Deepen the pipeline modules

**Goal:** Implement the five architecture-review candidates one at a time, each landing as a tested, independently deployable change, without disturbing the live service.

**Architecture:** Keep the five top-level scripts and their CLIs as entry points. Add three stdlib-only modules beside them (`intake.py`, `transcript.py`, `durable_queue.py`) and give the Ad classifier and Hindsight retention an injected function at their external dependency. No on-disk format changes: `seen.json`, sidecars, transcript JSON, `.ads.json`, pending and receipt files all keep their current shape and names, so no migration is needed and a rollback is `git revert` + restart.

**Tech Stack:** Python 3.12, stdlib only for anything `ingest.py` imports (the systemd unit runs it with `/usr/bin/python3`, not the venv), pytest for tests, GitHub Actions.

## Constraints found in the codebase

- `~/podcast-transcripts` is the live deployment. `pa-ingest` is active and launches `transcribe.py` / `postprocess.py` from this directory for every Episode. **No task edits files in this directory**; all work happens in the worktree created in Task 0.
- `ingest.py` runs under system Python. `intake.py` and anything else it imports must not need third-party packages.
- Importing `ingest`, `postprocess`, `clean_transcript_ai` or `hindsight_save` reads `PA_ROOT` at import, and `ingest` also creates directories. The current smoke test therefore touches whatever `PA_ROOT` resolves to (by default the live tree). Task 0 fixes this before any other test is written.
- Tasker depends on the HTTP status codes and JSON bodies of `/ingest`, `/complete`, `/health`, `/transcripts`. They stay byte-compatible except where a task says otherwise.
- README invariants that tests must protect: raw `.md/.srt/.json` are never rewritten by cleaning; an uncleaned Transcript is never sent to Hindsight; only transcript text reaches the AI classifier.

## Decisions (defaults used by this plan)

| # | Decision | Default in this plan |
|---|---|---|
| D1 | What happens to a failed Episode | Its digest is released from `seen.json`, so re-uploading it from the phone queues it again. The `.failed` file is kept for inspection. No automatic retry. The owner is notified on Telegram through `hermes send` (`PA_NOTIFY_TARGET`, default `telegram`; empty disables). |
| D2 | When changes reach the live service | Once, after Task 5, and only when the live queue is empty and the worker is idle. Before that, the whole pipeline is exercised with a small audio file in an isolated `PA_ROOT` on another port. |
| D3 | Commit shape | Branch `deepen-modules`, one commit per task, no pull requests, nothing pushed until asked. |
| D4 | Scope of candidate 5 | Included. It also closes a gap where a Transcript is never cleaned if the service dies between transcription and queuing. |

Smaller choices made without asking: flat modules rather than a package (keeps systemd and README paths valid); the two retry queues keep their separate directories and file formats and share one implementation; the clean Transcript header names the models that actually classified it.

## Task 0: Worktree, test harness, CI

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/conftest.py`
- Create: `GLOSSARY.md`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md` (Development checks section)

**Work:**
- [ ] `git -C ~/podcast-transcripts worktree add -b deepen-modules ~/podcast-transcripts-dev main`
- [ ] In the worktree: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`. `requirements-dev.txt` contains `pytest>=8,<9` only (tests never import `faster_whisper` or `numpy`; `transcribe.py` imports them lazily).
- [ ] `tests/conftest.py`: before any project import, set `PA_ROOT` and `PA_OUTDIR` to a session temp directory and clear `PA_TOKEN`; add the repo root to `sys.path` so tests use plain `import intake` instead of the `importlib` loader in `test_smoke.py`.
- [ ] `GLOSSARY.md` with the terms this plan uses: Episode, Upload, Sidecar, Transcript (raw), Clean transcript, Ad manifest, Retention, Receipt.
- [ ] CI: keep the compile and secret-scan steps; add `pip install -r requirements-dev.txt` and `python -m pytest -q`. Extend the compile list as new modules appear.
- [ ] README: replace the `py_compile`-only instructions with the pytest command.

**Verification:**
- Run: `cd ~/podcast-transcripts-dev && .venv/bin/python -m pytest -q`
- Expected: 2 passed, and `git -C ~/podcast-transcripts status --short` shows nothing but `plans/`.

## Task 1: Episode intake (candidate 1)

**Files:**
- Create: `intake.py`
- Modify: `ingest.py`
- Create: `tests/test_intake.py`
- Create: `tests/test_ingest_http.py`
- Modify: `tests/test_smoke.py` (delete `test_safe_name_and_show_path`; its cases move to `test_intake.py`)

**Work:**
- [ ] `intake.py` holds `safe_name`, `show_from_name`, `show_from_path`, `AUDIO_EXT` and a class `Intake(root, *, keep_audio, require_completed)` that creates `inbox/`, `staged/`, `archive/` on construction and owns one `threading.Lock`.
- [ ] `Intake.accept(payload, *, raw_name, show_hint, phone_path, fields) -> Accepted(status, name, show, sha256, existing)`. Does name sanitising, extension check (raises `UnsupportedAudio`), show inference, `.part` write, SHA-256, dedupe, sidecar write, ledger update. Status is `queued`, `staged_until_completion` or `duplicate`. `fields` is the dict of already-trimmed header values the sidecar records today.
- [ ] `Intake.complete(name, *, completed_at, source) -> status`: the logic of `_complete_staged`, returning `queued_after_completion`, `already_queued`, `already_processed` or `not_found`.
- [ ] `Intake.next_job() -> Job | None` (`name`, `audio_path`, `meta_path`, `meta`) and `Intake.finish(job, ok)`. Success archives or deletes the audio and removes the sidecar, as `worker()` does now. Failure renames to `.failed`, removes the sidecar, **and removes the digest from the ledger** (D1).
- [ ] `ingest.py` gets `notify_failure(name, rc)`: runs `hermes send --to $PA_NOTIFY_TARGET -q <message>` with a 30 s timeout, never raises, and is passed to the worker as a function so tests supply a recorder. `config.env.example` gains `PA_NOTIFY_TARGET=telegram`.
- [ ] `ingest.py` resolves `transcribe.py` / `postprocess.py` relative to its own file and the interpreter from `PA_PYTHON` (default `<code dir>/.venv/bin/python`), so the code can run against an isolated `PA_ROOT`.
- [ ] `Intake.status() -> {"queue": [...], "staged": [...]}` for `/health`.
- [ ] Ledger: same `seen.json` format. Every read-modify-write happens under the lock and is written with temp file + `os.replace`.
- [ ] `ingest.py`: remove the import-time `os.makedirs` loop and path constants that `Intake` now owns. `main()` builds one `Intake` from env and passes it to `build_server(intake, token, max_bytes, port)`, which sets it on the handler class. `do_POST` shrinks to: auth, length checks, multipart parsing, one `intake.accept` call, map result to the existing status codes and JSON bodies. `worker()` loops on `intake.next_job()` and calls `intake.finish`. The in-memory counters (`received`, `transcribed`, `failed`, `last`, `busy`) stay in `ingest.py`.
- [ ] `tests/test_intake.py` against `tmp_path`: accept then duplicate; unsupported extension; show inferred from path, then from name; staged mode then complete; complete for unknown name; `finish(ok=True)` with and without `keep_audio`; **`finish(ok=False)` then accepting the same bytes returns `queued`**; 20 threads accepting distinct payloads leave 20 ledger entries.
- [ ] `tests/test_ingest_http.py`: start `build_server` on port 0 in a thread with a temp `Intake` and a no-op worker; assert `POST /ingest` → 202 `queued`, repeat → 200 `duplicate`, wrong token → 401, `GET /health` lists the queued name.

**Verification:**
- Run: `.venv/bin/python -m pytest -q tests/test_intake.py tests/test_ingest_http.py`
- Expected: all pass. `grep -n "seen.json\|\.failed\|\.part" ingest.py` returns no matches.
- Run: `PA_ROOT=$(mktemp -d) PA_PORT=8799 PA_TOKEN=t /usr/bin/python3 ingest.py &` then `curl -s -H X-PA-Token:t http://127.0.0.1:8799/health`
- Expected: JSON with `"ok": true` under system Python (proves stdlib-only), then kill the process.

## Task 2: Transcript module (candidate 2)

**Files:**
- Create: `transcript.py`
- Modify: `transcribe.py`, `clean_transcript_ai.py`, `postprocess.py`, `hindsight_save.py`
- Create: `tests/test_transcript.py`
- Create: `tests/fixtures/raw.expected.md`, `tests/fixtures/clean.expected.md`

**Work:**
- [ ] Before touching any renderer, generate the two fixture files by calling the existing `transcribe.build_markdown` and `clean_transcript_ai.build_markdown` on one fixed segment list (long enough to produce three paragraphs, one over an hour). These are the byte-for-byte reference.
- [ ] `transcript.py`: `Transcript(json_path, meta, segments)` with `load(json_path)`, `save()`, and properties `md_path`, `srt_path`, `clean_path`, `ads_path` (same derivation as today: replace the final `.json` suffix). `Transcript.from_clean_path(md_path)` for callers that hold the clean Markdown.
- [ ] `render_markdown(segments, meta, *, heading_suffix="", front_matter_extra=(), show_duration=True, notes=())`, `render_srt(segments)`, `timestamp(seconds)`. One paragraphing rule (over 600 characters or over 75 seconds).
- [ ] `transcribe.py`: delete `ts_md`, `build_markdown`, `build_srt`; write artifacts through `Transcript`.
- [ ] `clean_transcript_ai.py`: delete `ts` and `build_markdown`; load with `Transcript.load`, write to `clean_path` / `ads_path`.
- [ ] `postprocess.py`: replace the two `with_suffix` lines with `Transcript` properties.
- [ ] `hindsight_save.py`: replace the `.clean.md` → `.ads.json` string replacement with `Transcript.from_clean_path(md).ads_path`. This also stops a raw `.md` being parsed as its own Ad manifest.
- [ ] Tests: both fixtures match byte for byte; `save()` then `load()` round-trips; path properties for a name containing extra dots; `render_srt` for a segment crossing an hour.

**Verification:**
- Run: `.venv/bin/python -m pytest -q`
- Expected: all pass. `grep -n "def build_markdown\|with_suffix(\".clean\|replace(\".clean" *.py` returns no matches.

## Task 3: Ad classifier seam (candidate 3)

**Files:**
- Modify: `clean_transcript_ai.py`, `postprocess.py`
- Create: `tests/test_ad_cleaning.py`
- Modify: `tests/test_smoke.py` (delete `test_ad_cleaner_chunk_and_ranges`; delete the file if empty)

**Work:**
- [ ] `CleanerConfig` dataclass (`provider`, `model`, `fallback_models`, `timeout`, `max_core_chars`, `context_seconds`, `retries`, `hermes_path`) with `from_env()`. Module-level env constants are removed.
- [ ] `clean(transcript, ask_model, config, *, sleep=time.sleep) -> CleanResult(kept, ad_ids, ranges, chunks, models_used)`. `ask_model(prompt, model) -> str` raises on failure. Chunking, prompt building, JSON extraction, core-ID validation, the retry × fallback loop and chunk merging all sit inside; `make_cores`, `build_prompt`, `parse_json_output`, `contiguous_ranges` become private.
- [ ] `hermes_ask(config)` returns the production `ask_model`: temp prompt file, the current Hermes command line, timeout, cleanup.
- [ ] `write_outputs(transcript, result, config, *, out=None, manifest=None)`: writes the Clean transcript and the Ad manifest. Manifest keys are unchanged. The `ai_cleaner` front-matter line becomes `provider/` followed by the comma-joined `models_used`.
- [ ] `is_reusable(transcript, config) -> bool`: the rule now in `postprocess.py` lines 68–76 moves here; `postprocess.py` calls it and no longer reads `PA_CLEAN_PROVIDER` / `PA_CLEAN_MODEL`.
- [ ] `main()` keeps its arguments (`json_transcript`, `--out`, `--manifest`) and its JSON line on stdout.
- [ ] Tests with a scripted `ask_model` and `sleep=lambda s: None`: no ads; ads spanning two chunks merge into correct intervals (small `max_core_chars`); first model returns non-core IDs and the fallback answers, `models_used` records the fallback; response with no JSON object; every model fails → `clean` raises and no files exist; raw `.json` bytes unchanged after `write_outputs`; `is_reusable` false when the configured model changes or the source is newer.

**Verification:**
- Run: `.venv/bin/python -m pytest -q tests/test_ad_cleaning.py`
- Expected: all pass.
- Run: `.venv/bin/python clean_transcript_ai.py --help`
- Expected: same three arguments as before.

## Task 4: Durable queue (candidate 4)

**Files:**
- Create: `durable_queue.py`
- Modify: `postprocess.py`, `hindsight_save.py`
- Create: `tests/test_durable_queue.py`

**Work:**
- [ ] `DurableQueue(pending_dir, receipts_dir, log)` with `enqueue(filename, envelope) -> Path` (temp file + `os.replace`; same filename overwrites) and `drain(handler) -> int`. `handler(envelope)` returns a receipt dict on success or `None` to leave the job pending; an exception is logged and treated as `None`, and the drain continues with the next file. On success the receipt is written atomically under the same filename and the pending file is removed. Returns the number still pending.
- [ ] `postprocess.py`: `queue()` and `flush()` use a `DurableQueue` on `postprocess-pending/` and `postprocess-receipts/`; `process()` becomes the handler. Envelope and receipt contents unchanged. Fix the `flush complete` log line so it reports processed and pending counts correctly.
- [ ] `hindsight_save.py`: `queue()` and `flush()` use a `DurableQueue` on `hindsight-pending/` and `hindsight-receipts/`; `submit()` becomes the handler. Filenames (`sha256(document_id)[:24].json`), envelope and receipt contents unchanged.
- [ ] Tests: enqueue leaves no `.tmp`; success moves pending → receipt; `None` keeps pending and returns 1; a raising handler keeps its job and later jobs still run; re-enqueue under one filename leaves one job; a pending file written before the queue object existed is drained (restart case).

**Verification:**
- Run: `.venv/bin/python -m pytest -q`
- Expected: all pass. `grep -n "os.replace" postprocess.py hindsight_save.py` returns no matches.

## Task 5: In-process stages (candidate 5)

**Files:**
- Modify: `postprocess.py`, `hindsight_save.py`, `transcribe.py`, `ingest.py`
- Create: `tests/test_postprocess.py`
- Modify: `README.md` (architecture diagram and manual commands, only where behaviour changed)

**Work:**
- [ ] `hindsight_save.py`: split `load_transcript` into `build_payload(transcript, clean_text) -> (payload, document_id, operation_id, content_sha)` (pure) and `submit(envelope, post=http_post)`, where `http_post(url, body, timeout) -> (status, text)` is the only place `urllib` is used. Add `retain(transcript, post=http_post) -> bool` that enqueues and drains. CLI unchanged.
- [ ] `postprocess.py`: `process()` calls `is_reusable`, `clean(..., hermes_ask(config), config)`, `write_outputs` and `retain` directly; `run_logged` and both subprocess calls are deleted. `process(envelope, *, ask_model=None, post=None)` accepts the two functions so tests can supply them.
- [ ] `transcribe.py`: new flag `--enqueue-postprocess`. When set, after the three artifacts are written it calls `postprocess.queue(base + ".json")`. It no longer writes `transcript_json` back into the sidecar.
- [ ] `ingest.py`: the worker passes `--enqueue-postprocess`, drops the sidecar re-read, and after each successful transcription starts `run_postprocess()` in flush mode. `run_postprocess` loses its argument.
- [ ] Tests for `postprocess.process` with scripted `ask_model` and `post`: happy path writes one postprocess receipt and one Hindsight receipt; **cleaner raises → `post` is never called and the job stays pending**; Hindsight returns 500 → job stays pending, clean files exist, a second drain reuses them without calling `ask_model`; missing transcript file keeps the job pending.

**Verification:**
- Run: `.venv/bin/python -m pytest -q`
- Expected: all pass. `grep -n "subprocess" postprocess.py` returns no matches.
- Run: `.venv/bin/python postprocess.py --help && .venv/bin/python hindsight_save.py --help && .venv/bin/python transcribe.py --help`
- Expected: all previous arguments present, plus `--enqueue-postprocess` on `transcribe.py`.

## Task 6: End-to-end check with a small audio file

**Work:**
- [ ] Cut a 30-second clip from an archived Episode with ffmpeg into the scratchpad.
- [ ] Start the worktree's `ingest.py` with `PA_ROOT=<temp dir>`, `PA_PORT=8799`, `PA_PYTHON=~/podcast-transcripts/.venv/bin/python`, `PA_NOTIFY_TARGET=` (empty) and `PA_HINDSIGHT_URL=http://127.0.0.1:9` so nothing reaches the real Hindsight bank.
- [ ] POST the clip; wait for the raw Transcript, the Clean transcript and the Ad manifest; confirm the retention job is left pending (Hindsight unreachable) and a second POST returns `duplicate`.
- [ ] POST a corrupt `.mp3`; confirm `.failed`, the digest released, and a re-POST returns `queued`.

**Verification:**
- Expected: the artifacts above exist under the temp root and nothing under `~/podcast-transcripts` changed except through the live service itself.

## Deploying (once, after Task 6, when the live queue is empty)

Not run until `/health` on the live service shows `"busy": false` and an empty queue.

```bash
git -C ~/podcast-transcripts merge --ff-only deepen-modules
systemctl --user restart pa-ingest
TOKEN="$(grep -oP 'PA_TOKEN=\K.*' ~/podcast-transcripts/config.env)"
curl -s -H "X-PA-Token:$TOKEN" http://127.0.0.1:8787/health
```

Expected: `"ok": true`, the same queue as before the restart, and `logs/ingest.log` shows `transcribing ...` within a few seconds. Rollback: `git -C ~/podcast-transcripts reset --hard <previous main>` then restart; no data format changed, so nothing else needs undoing.

## Out of scope

- Streaming uploads to disk instead of reading up to `PA_MAX_MB` into memory.
- Removing the `?token=` query-parameter auth path.
- Linting with ruff in CI.
