import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ad_cleaner_chunk_and_ranges():
    cleaner = load("clean_transcript_ai")
    segments = [{"start": 0, "end": 1, "text": "a"},
                {"start": 1, "end": 2, "text": "b"},
                {"start": 2, "end": 3, "text": "c"}]
    assert cleaner.contiguous_ranges([1], segments) == [
        {"start_index": 1, "end_index": 1, "start": 1, "end": 2}
    ]
    assert cleaner.make_cores(segments) == [(0, 3)]
