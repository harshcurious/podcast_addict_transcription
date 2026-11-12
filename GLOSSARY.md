# Glossary

Domain terms used in this codebase. Use them exactly in code, tests and docs.

**Episode**: one podcast audio file, identified by the SHA-256 of its bytes.

**Upload**: one HTTP delivery of an Episode from the phone. Several Uploads of the same
Episode are deduplicated by digest.

**Sidecar**: the `<audio>.json` file written next to a queued Episode, holding what the
Upload said about it (show, source path, feed URL, timestamps, digest).

**Ledger**: `seen.json`, the record of digests that have been accepted. A digest leaves
the Ledger when its Episode fails, so the Episode can be uploaded again.

**Transcript**: the raw, timestamped result of speech recognition for one Episode, stored
as `.json` (metadata + segments), `.md` and `.srt`. Never rewritten after it is produced.

**Segment**: one timestamped span of recognised speech inside a Transcript.

**Clean transcript**: the `.clean.md` derivative of a Transcript with ad Segments removed.

**Ad manifest**: the `.ads.json` audit record of which Segments were removed, by which
model, and why.

**Retention**: storing a Clean transcript in the local Hindsight bank.

**Receipt**: the file written when a queued job is accepted downstream; its presence means
the job will not be retried.
