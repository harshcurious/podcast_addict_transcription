import pytest

from mac_asr import FASTER_WHISPER, PARAKEET, WHISPER, engine_for


def everything(module):
    return True


def no_mlx_whisper(module):
    return module != "mlx_whisper"


@pytest.mark.parametrize("language", ["en", "hi", ""])
def test_auto_is_whisper_for_every_language(language):
    assert engine_for("auto", language, available=everything) == WHISPER
    assert engine_for("auto", language, available=no_mlx_whisper) == FASTER_WHISPER


@pytest.mark.parametrize("language", ["en", "de", "uk"])
def test_parakeet_for_its_languages(language):
    assert engine_for(PARAKEET, language, available=everything) == PARAKEET


@pytest.mark.parametrize("language", ["hi", "ja", ""])
def test_parakeet_falls_back_to_whisper_for_other_or_unknown_languages(language):
    assert engine_for(PARAKEET, language, available=everything) == WHISPER
    assert engine_for(PARAKEET, language, available=no_mlx_whisper) == FASTER_WHISPER


def test_explicit_whisper_and_old_mlx_name():
    assert engine_for("whisper", "en", available=everything) == WHISPER
    assert engine_for("mlx", "en", available=everything) == WHISPER
    assert engine_for("faster-whisper", "en", available=everything) == FASTER_WHISPER


def test_parakeet_requested_but_missing_is_an_error_not_a_silent_switch():
    with pytest.raises(RuntimeError):
        engine_for(PARAKEET, "en", available=lambda m: m != "parakeet_mlx")


def test_unknown_backend():
    with pytest.raises(ValueError):
        engine_for("nope", "en", available=everything)
