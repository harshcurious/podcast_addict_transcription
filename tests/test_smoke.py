import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_safe_name_and_show_path():
    ingest = load("ingest")
    assert ingest.safe_name("../Morning Brew - Episode?.mp3") == "Morning Brew - Episode_.mp3"
    assert ingest.show_from_path(
        "/storage/emulated/0/Android/media/com.bambuna.podcastaddict/podcast/Demo Show/ep.mp3"
    ) == "Demo Show"


def test_ad_cleaner_chunk_and_ranges():
    cleaner = load("clean_transcript_ai")
    segments = [{"start": 0, "end": 1, "text": "a"},
                {"start": 1, "end": 2, "text": "b"},
                {"start": 2, "end": 3, "text": "c"}]
    assert cleaner.contiguous_ranges([1], segments) == [
        {"start_index": 1, "end_index": 1, "start": 1, "end": 2}
    ]
    assert cleaner.make_cores(segments) == [(0, 3)]
