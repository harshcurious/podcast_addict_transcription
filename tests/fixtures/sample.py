"""One fixed Transcript used by the rendering reference tests."""

META = {
    "title": 'The "Big" Episode', "show": "Demo Show", "source_file": "ep.mp3",
    "audio_duration": 3723.4, "model": "base.en", "language": "en",
    "transcribed_at": "2026-01-01T10:00:00", "rtf": 4.2, "audio_sha256": "abc123",
    "description": "not in front matter", "words": 0,
}


def _segments():
    out, t = [], 0.0
    for i in range(30):  # dense speech: paragraphs break on the 600-character rule
        out.append({"start": round(t, 2), "end": round(t + 4.5, 2),
                    "text": f" Sentence number {i} of the opening discussion about things. "})
        t += 4.5
    for i in range(4):  # sparse speech: paragraphs break on the 75-second rule
        out.append({"start": round(t, 2), "end": round(t + 40, 2), "text": f"Slow remark {i}."})
        t += 40
    out.append({"start": 3700.25, "end": 3723.4, "text": "Goodbye after the hour mark."})
    return out


SEGMENTS = _segments()
AD_IDS = [3, 4, 5, 31]
