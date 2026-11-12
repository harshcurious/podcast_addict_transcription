# Backfill: transcripts of everything already listened to

The Pi service only sees Episodes as they download. `backfill/` handles the history: it
reads a Podcast Addict backup, finds every Episode you have listened to, and gets each one
into the same `podcast-addict` Hindsight bank as a Clean transcript (ads removed). It is
meant to run on an Apple-silicon Mac.

For each listened Episode, most recently played first:

1. **Online first.** It uses the publisher's own transcript if there is one: first the
   transcript URLs Podcast Addict stored in the backup, then the Podcasting 2.0
   `<podcast:transcript>` tags in the Episode's live RSS feed. JSON, WebVTT, SRT, HTML and
   plain-text transcripts are all accepted. A file too short for the episode length (a
   summary rather than a transcript) is rejected.
2. **Otherwise, transcribe on this Mac.** It downloads the audio and runs mlx-whisper
   (`whisper-large-v3-turbo` on the GPU) or faster-whisper, then deletes the audio.
3. **Remove ad reads** with the pipeline's own classifier (`clean_transcript_ai.py`). Only
   segment IDs come back from the model; the raw transcript is never rewritten. This step
   fails closed: if cleaning fails, nothing is retained.
4. **Retain** the Clean transcript in Hindsight with `hindsight_save.py`'s durable queue.

The Pi pipeline's modules are imported, never modified, so the Pi service is unaffected.
Nothing here runs on the Pi.

## Setup (once)

```bash
cd ~/podcast-transcripts            # this repository
uv venv --python 3.12 backfill/.venv
uv pip install --python backfill/.venv/bin/python -r backfill/requirements.txt
cp backfill/config.env.example backfill/config.env
$EDITOR backfill/config.env         # PA_ROOT, PA_HINDSIGHT_URL, cleaner models
```

`backfill.py` loads `backfill/config.env` by itself; environment variables override it.

- **Speech recognition:** `ffmpeg` must be installed (`brew install ffmpeg`). The first run
  downloads the Whisper model (about 1.6 GB) from Hugging Face.
- **Ad cleaner:** uses Hermes when installed (as the Pi does), otherwise `opencode run`.
  For opencode, the classifier runs as an inline agent with every tool disabled and every
  permission denied, in an empty directory, because the prompt contains untrusted
  transcript text. Choose models your provider actually offers:
  `opencode models | grep opencode-go/`.
- **Hindsight:** point `PA_HINDSIGHT_URL` at the instance that holds the Pi's bank, for
  example its Tailscale HTTPS name.

## Running

```bash
B=~/Downloads/PodcastAddict_20261112_183053.backup
PY=backfill/.venv/bin/python

$PY backfill/backfill.py "$B" --dry-run          # plan: online vs local, hours of audio
$PY backfill/backfill.py "$B" --online-only      # publisher transcripts only (fast)
caffeinate -i $PY backfill/backfill.py "$B"      # everything; stop and restart any time
$PY backfill/backfill.py --status                # progress and the most common errors
$PY backfill/backfill.py --flush                 # retry cleaning/retention that failed
```

Useful filters: `--limit N`, `--podcast "Talk Python"` (repeatable),
`--played-since 2026-01-01`, `--episode-id 48935`.

Re-running is always safe. Each Episode's progress is kept in
`$PA_ROOT/backfill-state/<key>.json`, where the key comes from the feed URL and the item
GUID, so a newer backup of the same library continues where the last one stopped.
Episode statuses:

| status | meaning |
|---|---|
| `transcribed` | raw Transcript exists; cleaning or retention still to do (`--flush`) |
| `retained` | Hindsight accepted the Clean transcript |
| `pi-pipeline` | the Pi already retained this Episode; skipped |
| `waiting` | `--online-only` found no transcript; a full run will transcribe it |
| `failed` | no transcript could be made (dead audio URL…); retried up to `--max-attempts` |

Ad cleaning stops for the rest of a run after `PA_BACKFILL_MAX_CLEAN_FAILURES` failures in
a row (for example, the model endpoint is down), but transcription carries on;
`--flush` finishes those Episodes later. When Hindsight is unreachable, retain requests
stay in `hindsight-pending/` and are re-sent at the end of the run and by `--flush`.

## Outputs

`PA_ROOT` (default `~/podcast-backfill`) uses the same layout as the Pi:

```text
out/<show>/<published>-<title>.json|.md|.srt     raw Transcript (never rewritten)
out/<show>/<published>-<title>.clean.md|.ads.json  Clean transcript + Ad manifest
backfill-state/   hindsight-pending/   hindsight-receipts/   logs/   work/
```

In Hindsight, each Episode is one document, `podcast:episode:<key>`, tagged
`source:backfill`. Its metadata records `transcript_source` (`publisher` or `asr`),
`transcript_url` and `played_at`. Re-processing an Episode replaces its document instead of
adding a second one. Before starting, the backfill lists the bank and skips Episodes whose
show and title match a document the Pi already stored.

## Expectations

- Most podcasts do not publish transcripts in their feed, so expect most Episodes to need
  local transcription. A dry run gives the split and the hours of audio for your library.
- Each retained transcript is processed by Hindsight on the Pi (memory extraction), so a
  large backfill queues a lot of work there. `--limit` and `--podcast` let you pace it.
- Retention sends transcript text to the AI cleaner's provider and to Hindsight; audio
  never leaves the Mac.
