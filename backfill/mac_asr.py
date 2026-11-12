"""Download an Episode's audio and transcribe it on this Mac.

Backends: `mlx` (mlx-whisper on the Apple-silicon GPU, the default when installed) or
`faster-whisper` (CPU, what the Pi uses). The model is loaded once and reused for every
Episode in a run. Decoding reuses `transcribe.decode_to_pcm` from the Pi pipeline.
"""
import hashlib
import os
import pathlib
import time
import urllib.parse
import urllib.request

from transcribe import decode_to_pcm

USER_AGENT = "pa-transcript-backfill/1.0"
DEFAULT_MODELS = {"mlx": "mlx-community/whisper-large-v3-turbo",
                  "faster-whisper": "large-v3-turbo"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".wav", ".flac", ".webm"}


def download(url, dest_dir, name, *, max_bytes, timeout=60):
    """Stream `url` to `dest_dir/name.<ext>`. Returns (path, sha256 hex, byte count)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        ext = pathlib.PurePosixPath(urllib.parse.urlparse(response.url).path).suffix.lower()
        path = dest_dir / (name + (ext if ext in AUDIO_EXTENSIONS else ".audio"))
        part = path.with_name(path.name + ".part")
        try:
            with part.open("wb") as out:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"audio larger than {max_bytes // 2**20} MB")
                    digest.update(chunk)
                    out.write(chunk)
            os.replace(part, path)
        finally:
            part.unlink(missing_ok=True)
    return path, digest.hexdigest(), size


def pick_backend(requested="auto"):
    if requested != "auto":
        return requested
    try:
        import mlx_whisper  # noqa: F401
        return "mlx"
    except ImportError:
        return "faster-whisper"


class Recognizer:
    def __init__(self, backend="auto", model="", *, beam=1, threads=0):
        self.backend = pick_backend(backend)
        if self.backend not in DEFAULT_MODELS:
            raise ValueError(f"unknown ASR backend {self.backend!r}")
        self.model = model or DEFAULT_MODELS[self.backend]
        self.beam, self.threads = beam, threads or (os.cpu_count() or 4)
        self._fw = None

    def transcribe(self, audio_path, language=""):
        """Returns (segments, info) where info has language, duration, wall_seconds."""
        audio = decode_to_pcm(str(audio_path))
        duration = len(audio) / 16000
        started = time.time()
        if self.backend == "mlx":
            import mlx_whisper
            result = mlx_whisper.transcribe(
                audio, path_or_hf_repo=self.model, verbose=None, language=language or None,
                condition_on_previous_text=False)
            raw = [(s["start"], s["end"], s["text"]) for s in result.get("segments", [])]
            detected = result.get("language") or language
        else:
            if self._fw is None:
                from faster_whisper import WhisperModel
                self._fw = WhisperModel(self.model, device="cpu", compute_type="int8",
                                        cpu_threads=self.threads)
            segs, info = self._fw.transcribe(
                audio, beam_size=self.beam, vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                condition_on_previous_text=False, language=language or None)
            raw = [(s.start, s.end, s.text) for s in segs]
            detected = info.language
        segments = [{"start": round(a, 2), "end": round(b, 2), "text": t.strip()}
                    for a, b, t in raw if t.strip()]
        wall = time.time() - started
        return segments, {"language": detected or "", "duration": round(duration, 1),
                          "wall_seconds": round(wall, 1),
                          "rtf": round(duration / wall, 2) if wall else 0}
