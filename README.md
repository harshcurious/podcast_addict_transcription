# Podcast Addict → local transcript pipeline

A local-first pipeline that receives Podcast Addict downloads from Tasker, transcribes them on a Raspberry Pi with faster-whisper, removes ad-read segments using a structured AI classifier, and retains the cleaned transcript in a local Hindsight bank.

Audio stays on the phone/Pi path. Only timestamped transcript text is sent to the configured AI cleaner. Raw transcript artifacts are immutable; cleaning creates separate derivatives.

## Architecture

```text
Podcast Addict DOWNLOAD_COMPLETED broadcast
        │
        ▼
Tasker HTTP POST /ingest with audio + metadata headers
        │
        ▼
ingest.py + intake.py: token auth, SHA-256 dedupe, durable inbox, single worker
        │
        ▼
transcribe.py: ffmpeg → 16 kHz mono → faster-whisper CPU int8
        │        raw .md + .srt + timestamped .json, then queues post-processing
        ▼
postprocess.py: drains the durable queue, one process for both steps below
        │
        ├─ clean_transcript_ai.py: structured ad-segment classification
        │        .clean.md + .ads.json audit manifest
        ▼
   hindsight_save.py: local Hindsight, bank podcast-addict
```

Supporting modules: `transcript.py` owns the transcript files and their rendering, `durable_queue.py` the pending/receipt retry queues. `GLOSSARY.md` defines the terms used in the code.

If transcription of an episode fails, its audio is kept as `inbox/<name>.failed`, a message is sent through `hermes send` (see `PA_NOTIFY_TARGET`), and the episode can be uploaded again: download it again in Podcast Addict.

The optional `/complete` endpoint supports a future completion gate, but it is disabled by default because Podcast Addict does not expose an authoritative played/completed broadcast.

## Requirements

- Linux/macOS
- Python 3.12 recommended
- `ffmpeg` and `ffprobe`
- `faster-whisper` dependencies from `requirements.txt`
- Optional: Hermes CLI configured with an OpenCode provider for AI ad classification
- Optional: local Hindsight at `PA_HINDSIGHT_URL`
- Android phone with Podcast Addict and Tasker

## Installation

```bash
git clone https://github.com/harshcurious/podcast_addict_transcription.git ~/podcast-transcripts
cd ~/podcast-transcripts

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.env.example config.env
$EDITOR config.env
```

Set a long random `PA_TOKEN`. Keep `config.env` private; it is ignored by Git.

For the Pi service:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/pa-ingest.service.example ~/.config/systemd/user/pa-ingest.service
systemctl --user daemon-reload
systemctl --user enable --now pa-ingest
```

`EnvironmentFile` must contain an absolute `PA_ROOT` and `PA_OUTDIR`. Enable user-service linger if the service must run without an interactive login:

```bash
loginctl enable-linger "$USER"
```

## Tasker configuration

Create or reuse a Tasker profile for the Podcast Addict intent:

```text
com.bambuna.podcastaddict.service.DOWNLOAD_COMPLETED
```

Add one **Net → HTTP Request** action:

- Method: `POST`
- URL: `http://<PI-LAN-IP>:8787/ingest`
- Body: empty
- File To Send: `%path` with no leading or trailing space
- File To Save With Output: empty

Headers:

```text
X-PA-Token:<the value of PA_TOKEN in config.env>
X-PA-Name:%file
X-PA-Path:%path
X-PA-Source:Podcast Addict
X-PA-Device:Pixel 9
```

The Pi derives the show from the parent directory and extracts embedded publisher metadata with `ffprobe`. Optional headers are supported:

```text
X-PA-Show:<show name>
X-PA-Feed-URL:<feed URL>
X-PA-Episode-URL:<episode URL>
X-PA-Published-At:<ISO-8601 timestamp>
```

Do not put the token in the URL, README, screenshots, logs, or a Tasker export committed to Git.

## Configuration

Important settings in `config.env`:

- `PA_MODEL`: ASR model, normally `base.en` on a Pi 4
- `PA_LANG`: language or empty for auto-detection
- `PA_BEAM`: decoding beam size
- `PA_KEEP_AUDIO`: retain source audio under `archive/`
- `PA_REQUIRE_COMPLETED`: stage uploads until `/complete` when set to `1`
- `PA_CLEAN_MODEL`: primary AI ad classifier
- `PA_CLEAN_FALLBACK_MODELS`: comma-separated fallback order
- `PA_HINDSIGHT_BANK`: defaults to `podcast-addict`
- `PA_NOTIFY_TARGET`: `hermes send` target for failure messages, default `telegram`; empty disables
- `PA_PYTHON`: interpreter for the transcription and post-processing child processes, default `.venv/bin/python` next to the code

The default cleaner is `deepseek-v4.1-flash-free`, followed by free MiMo/Beam variants and paid MiMo only if all free endpoints are unavailable. Review the provider's current privacy and retention terms before sending transcript text.

## Outputs

Runtime data is intentionally ignored by Git:

```text
inbox/                  transient upload queue
archive/                original audio, if PA_KEEP_AUDIO=1
out/<show>/             raw .md/.srt/.json plus .clean.md/.ads.json
logs/                   operational logs
*-pending/              durable downstream retry queues
*-receipts/             accepted-operation receipts
seen.json               SHA-256 dedupe state (a failed episode is removed from it)
```

Raw `.md`, `.srt`, and `.json` files are never rewritten by the ad cleaner. The `.ads.json` file records selected segment IDs, intervals, model use, and reasons. If AI cleaning fails, the transcript remains pending and is not retained uncleaned in Hindsight.

## Manual commands

```bash
# Transcribe one local file
.venv/bin/python transcribe.py episode.mp3 --show "Some Podcast"

# Clean and retain an existing timestamped transcript
set -a; . ./config.env; set +a
.venv/bin/python postprocess.py out/some-podcast/episode.json

# Check service
TOKEN="$(grep -oP 'PA_TOKEN=\\K.*' config.env)"
curl -s -H "X-PA-Token:$TOKEN" http://127.0.0.1:8787/health
curl -s -H "X-PA-Token:$TOKEN" http://127.0.0.1:8787/transcripts
```

## Backfilling past listens

`backfill/` is a separate, Mac-only tool for Episodes listened to before this service
existed. It reads a Podcast Addict backup and uses the publisher's transcript where one is
online, or transcribes locally with mlx-whisper. Each transcript is cleaned with the same
ad classifier and retained in the same Hindsight bank. It imports the pipeline modules
without changing them and does not run on the Pi. See `backfill/README.md`.

## Security and privacy

- `config.env` is never committed.
- Audio, transcripts, logs, queues, receipts, virtual environments, and dedupe state are never committed.
- The HTTP endpoint is intended for a trusted LAN. Use a firewall or private overlay network if exposed beyond it.
- The shared Tasker/Pi token should be rotated if it has ever been pasted into a public location or committed history.
- Free AI endpoints may have different data-use policies; the pipeline sends transcript text only, never source audio.

## Development checks

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests need neither `faster-whisper` nor a network. GitHub Actions runs a syntax check, the tests, and a basic repository secret scan on pushes and pull requests. Domain terms are defined in `GLOSSARY.md`.
