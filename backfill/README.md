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
2. **Otherwise, transcribe on this Mac.** It downloads the audio, transcribes it with
   Whisper `large-v3-turbo` on the Apple-silicon GPU (mlx-whisper), then deletes the
   audio. See [Choosing the speech-recognition engine](#choosing-the-speech-recognition-engine).
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
  downloads the Whisper model (about 1.6 GB) from Hugging Face; Parakeet, if you choose it,
  is another 2.5 GB.
- **Ad cleaner:** `PA_CLEAN_BACKEND` picks how the classifier model is reached. The
  prompt contains untrusted transcript text, so no backend gives the model tools.
  - `openai`: one HTTP request per transcript chunk to any OpenAI-compatible endpoint
    (OpenRouter, Groq, a provider's own API, or a local `mlx_lm.server`). Set
    `PA_CLEAN_API_URL`, `PA_CLEAN_API_KEY` and the provider's model names in
    `PA_CLEAN_MODEL` / `PA_CLEAN_FALLBACK_MODELS`. A local model keeps the text on the
    Mac, but it must be strong enough to follow the classifier's rules: tested on a 24 GB
    Mac, `mlx-community/Qwen3.5-9B-MLX-4bit` found the right ads but also listed context
    segments it was told to leave alone, so every chunk was rejected (the cleaner fails
    closed); with thinking enabled it took over 7 minutes for one chunk.
  - `hermes`: the Hermes CLI, as on the Pi.
  - `opencode`: `opencode run`. Choose models your provider offers
    (`opencode models | grep opencode-go/`). It runs with an empty private config
    directory, so your global opencode plugins (including the Hindsight plugin, which
    would otherwise record every classifier call as a coding session), MCP servers and
    `AGENTS.md` are not loaded. The classifier is an inline agent with every tool
    disabled and every permission denied. Older opencode builds without `--archived`
    work too. `PA_CLEAN_OPENCODE` pins a binary when several are installed.
  - `auto` (default): `openai` when `PA_CLEAN_API_URL` is set, else Hermes if
    installed, else opencode.
- **Hindsight:** point `PA_HINDSIGHT_URL` at the instance that holds the Pi's bank, for
  example its Tailscale HTTPS name.

## Running

```bash
B=~/Downloads/PodcastAddict_20261112_183053.backup
PY=backfill/.venv/bin/python

$PY backfill/backfill.py "$B" --dry-run          # plan: online vs local, hours of audio
$PY backfill/backfill.py "$B" --online-only      # publisher transcripts only (fast)
$PY backfill/backfill.py "$B"                    # everything; stop and restart any time
$PY backfill/backfill.py --status                # progress and the most common errors
$PY backfill/backfill.py --flush                 # retry cleaning/retention that failed
```

Useful filters: `--limit N`, `--podcast "Talk Python"` (repeatable),
`--played-since 2026-01-01`, `--episode-id 48935`.

A run keeps the Mac from idle sleep until it ends. Ctrl-C or `kill` (SIGTERM) stops it
cleanly: the Episode being cleaned is finished, and the rest resume on the next run.

### As a background service

A whole library takes days, so `service.sh` runs the backfill as a macOS LaunchAgent: it
keeps running with no terminal open, is restarted 5 minutes after a crash, and starts
again at login until you stop it.

```bash
backfill/service.sh show "$B"                  # review the LaunchAgent first (optional)
backfill/service.sh install "$B"               # create it and start; options go after "$B"
backfill/service.sh status                     # running or not, plus --status
backfill/service.sh logs                       # follow logs/backfill.log
backfill/service.sh restart                    # e.g. after editing config.env
backfill/service.sh stop                       # stays stopped, also across logins
backfill/service.sh start
backfill/service.sh uninstall
```

The LaunchAgent (`~/Library/LaunchAgents/com.podcast-addict.backfill.plist`) points at
this checkout and its `backfill/.venv` by absolute path, and records your shell's `PATH`
when you install (launchd's own lacks Homebrew, so `ffmpeg` and `opencode` would not be
found). Re-run `install` after moving the checkout, changing `PATH`, or to change the
backup file or options. Output that is not in `backfill.log` (tracebacks) goes to
`logs/service.log`.

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

## Choosing the speech-recognition engine

`PA_ASR_BACKEND` picks the engine: `auto` (Whisper on the GPU; the default), `whisper`,
`faster-whisper` (CPU) or `parakeet`. `compare_asr.py` measures them on your own Episodes
against the publishers' transcripts:

```bash
uv pip install --python backfill/.venv/bin/python jiwer
backfill/.venv/bin/python backfill/compare_asr.py "$B" --episode-id 48935 --episode-id 47871
```

Measured on an M-series Mac, November 2026 (word error rate; lower is better):

| Episode | Whisper large-v3-turbo | Parakeet TDT 0.6B v3 | Qwen3-ASR 1.7B |
|---|---|---|---|
| Talk Python #559, 68 min, English | **5.3%**, 22x realtime | 10.5%, 42x | 7.6% |
| Normal Curves, 55 min, English | **11.8%**, 26x | 13.3%, 48x | not run |
| Puliyabaazi, 33 min, Hindi | (40.9%), 5.5x | no Hindi | not run |

Whisper is the default because it was the most accurate, and at over 20x realtime an
hour of audio still takes about three minutes. Parakeet is about twice as fast; part of
its higher error rate is that it is verbatim ("uh", "um", "you know", false starts) where
the publishers' transcripts are edited, but it was still less accurate with fillers
removed (8.8% against 5.0% for Talk Python). Choose it with `PA_ASR_BACKEND=parakeet`
for a faster bulk run; Episodes in languages it lacks still go to Whisper. Qwen3-ASR is
benchmark-only: it gives one timestamp per 20 minutes, too coarse to cut out ad reads,
and on the second Episode it looped without finishing.

The Hindi figure is in brackets because it does not measure accuracy: the publisher's
machine-made transcript leaves out whole passages and writes English loanwords in Latin
script, so a more complete transcript scores worse. NVIDIA's
`nemotron-3.5-asr-streaming-0.6b` (Parakeet's successor family, 40 languages including
Hindi; `mlx-community/nemotron-3.5-asr-streaming-0.6b-8bit` runs in mlx-audio) "scored"
50.3% on that reference, yet it missed only 35 of the reference's words where Whisper
missed 369, and read side by side its Hindi is the more accurate. It is not wired in: in
English it was less accurate than Whisper (11.9% for Talk Python), and in Hindi it
returned five "sentences" for 33 minutes, so its timings are too coarse to cut out ad
reads without re-splitting its output.

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
