"""Download an Episode's audio and transcribe it on this Mac.

Engines, chosen per Episode by `PA_ASR_BACKEND` and the Episode's language:

    auto            whisper, or faster-whisper where mlx-whisper is not installed (default).
    whisper         Whisper large-v3-turbo via mlx-whisper (Apple-silicon GPU), any language.
    faster-whisper  Whisper via faster-whisper (CPU), what the Pi uses.
    parakeet        NVIDIA Parakeet TDT v3 via parakeet-mlx (Apple-silicon GPU): about twice
                    as fast as Whisper, but less accurate in compare_asr.py on English
                    Episodes, and verbatim (keeps "uh", "um", false starts). 25 European
                    languages only; any other or unknown language falls back to Whisper.

Each engine's model is loaded once and reused for every Episode in a run. Whisper decoding
reuses `transcribe.decode_to_pcm` from the Pi pipeline.
"""
import hashlib
import importlib.util
import os
import pathlib
import time
import urllib.parse
import urllib.request

from transcribe import decode_to_pcm, ffprobe_info

USER_AGENT = "pa-transcript-backfill/1.0"
PARAKEET, WHISPER, FASTER_WHISPER = "parakeet", "whisper", "faster-whisper"
DEFAULT_MODELS = {PARAKEET: "mlx-community/parakeet-tdt-0.6b-v3",
                  WHISPER: "mlx-community/whisper-large-v3-turbo",
                  FASTER_WHISPER: "large-v3-turbo"}
MODEL_ENV = {PARAKEET: "PA_PARAKEET_MODEL", WHISPER: "PA_WHISPER_MODEL",
             FASTER_WHISPER: "PA_WHISPER_MODEL"}
# From the parakeet-tdt-0.6b-v3 model card.
PARAKEET_LANGUAGES = frozenset("bg hr cs da nl en et fi fr de el hu it lv lt mt pl pt ro sk sl "
                               "es sv ru uk".split())
PARAKEET_CHUNK_SECONDS = 120     # long audio is transcribed in overlapping chunks
PARAKEET_OVERLAP_SECONDS = 15
PARAKEET_MAX_SENTENCE_SECONDS = 30  # keeps Segments short enough to cut ads precisely
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


def installed(module):
    return importlib.util.find_spec(module) is not None


def engine_for(backend, language, *, available=installed):
    """The engine that transcribes an Episode in `language` ("" = unknown)."""
    backend = {"mlx": WHISPER, "mlx-whisper": WHISPER}.get(backend, backend)
    whisper = WHISPER if available("mlx_whisper") else FASTER_WHISPER
    if backend in (WHISPER, FASTER_WHISPER):
        return backend
    if backend == "auto":
        return whisper
    if backend != PARAKEET:
        raise ValueError(f"unknown ASR backend {backend!r}")
    if not available("parakeet_mlx"):
        raise RuntimeError("PA_ASR_BACKEND=parakeet but parakeet-mlx is not installed")
    return PARAKEET if language in PARAKEET_LANGUAGES else whisper


class Recognizer:
    def __init__(self, backend="auto", *, models=None, beam=1, threads=0):
        self.backend = backend
        self.models = {**DEFAULT_MODELS,
                       **{e: os.environ[v] for e, v in MODEL_ENV.items() if os.environ.get(v)},
                       **(models or {})}
        self.beam, self.threads = beam, threads or (os.cpu_count() or 4)
        self._loaded = {}

    def describe(self, language):
        engine = engine_for(self.backend, language)
        return engine, self.models[engine]

    def transcribe(self, audio_path, language=""):
        """Returns (segments, info): info has engine, model, language, duration, wall_seconds,
        rtf. Segments are [{start, end, text}] like the Pi's transcribe.py writes."""
        engine, model = self.describe(language)
        started = time.time()
        raw, detected, duration = getattr(self, f"_{engine.replace('-', '_')}")(
            str(audio_path), model, language)
        wall = time.time() - started
        segments = [{"start": round(a, 2), "end": round(b, 2), "text": t.strip()}
                    for a, b, t in raw if t.strip()]
        return segments, {"engine": engine, "model": model, "language": detected or "",
                          "duration": round(duration, 1), "wall_seconds": round(wall, 1),
                          "rtf": round(duration / wall, 2) if wall else 0}

    def _parakeet(self, path, model, language):
        from parakeet_mlx import DecodingConfig, SentenceConfig, from_pretrained
        if PARAKEET not in self._loaded:
            self._loaded[PARAKEET] = from_pretrained(model)
        config = DecodingConfig(sentence=SentenceConfig(
            max_duration=PARAKEET_MAX_SENTENCE_SECONDS))
        result = self._loaded[PARAKEET].transcribe(
            path, decoding_config=config, chunk_duration=PARAKEET_CHUNK_SECONDS,
            overlap_duration=PARAKEET_OVERLAP_SECONDS)
        duration = ffprobe_info(path)[0] or (result.sentences[-1].end if result.sentences
                                             else 0)
        return [(s.start, s.end, s.text) for s in result.sentences], language, duration

    def _whisper(self, path, model, language):
        import mlx_whisper
        audio = decode_to_pcm(path)
        result = mlx_whisper.transcribe(audio, path_or_hf_repo=model, verbose=None,
                                        language=language or None,
                                        condition_on_previous_text=False)
        raw = [(s["start"], s["end"], s["text"]) for s in result.get("segments", [])]
        return raw, result.get("language") or language, len(audio) / 16000

    def _faster_whisper(self, path, model, language):
        if FASTER_WHISPER not in self._loaded:
            from faster_whisper import WhisperModel
            self._loaded[FASTER_WHISPER] = WhisperModel(model, device="cpu", compute_type="int8",
                                                        cpu_threads=self.threads)
        audio = decode_to_pcm(path)
        segs, info = self._loaded[FASTER_WHISPER].transcribe(
            audio, beam_size=self.beam, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False, language=language or None)
        return [(s.start, s.end, s.text) for s in segs], info.language, len(audio) / 16000
