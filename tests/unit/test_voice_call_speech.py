# ruff: noqa: RUF001
"""The voice-call example's speech rules: language detection, voice choice, speed."""

import importlib.util
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[2] / "examples/apps/voice-call/backend/speech.py"
_spec = importlib.util.spec_from_file_location("voice_call_speech", _PATH)
assert _spec and _spec.loader
speech = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(speech)


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("你好，今天下午三点帮我看一下新加坡的环境。", "zh"),
        ("我刚部署了 AgentCore runtime，一切正常。", "zh"),
        ("行", "zh"),
        ("こんにちは、これを手伝ってもらえますか？", "ja"),
        ("안녕하세요, 이것 좀 도와주시겠어요?", "ko"),
        ("مرحبا، هل يمكنك مساعدتي في هذا؟", "ar"),
        ("Hello, can you help me with this? Thanks a lot.", "en"),
        ("Olá, você pode me ajudar com isso? Muito obrigado.", "pt"),
        ("Bom dia", "pt"),
        ("¿Dónde está la estación?", "es"),
        ("Bonjour, est-ce que vous pouvez m'aider avec ça ?", "fr"),
        ("Grazie, ci vediamo domani.", "it"),
        ("Hallo, kannst du mir bitte dabei helfen? Vielen Dank.", "de"),
        ("OK", "en"),
    ],
)
def test_detect_language(text: str, lang: str) -> None:
    assert speech.detect_language(text) == lang


def test_detect_language_without_words_uses_the_fallback() -> None:
    assert speech.detect_language("123 456") == "zh"
    assert speech.detect_language("...", fallback="en") == "en"


def test_every_language_has_a_default_choice_and_sample() -> None:
    assert set(speech.VOICES) == set(speech.CHOICES) == set(speech.SAMPLES)
    for lang, (voice, engine, code) in speech.VOICES.items():
        assert (voice, engine, code) in [c[:3] for c in speech.CHOICES[lang]], lang
    catalog = speech.voice_catalog()
    assert catalog["pt"]["default"] == "Carolina"
    assert {o["gender"] for opts in catalog.values() for o in opts["options"]} <= {"f", "m"}


def test_pick_voice_honours_known_choices_only() -> None:
    assert speech.pick_voice("pt", {"pt": "Leo"}) == ("Leo", "generative", "pt-BR")
    assert speech.pick_voice("pt", {"pt": "Nobody"}) == speech.VOICES["pt"]
    assert speech.pick_voice("es", None) == speech.VOICES["es"]


@pytest.mark.parametrize(
    ("value", "want"), [(0.8, 0.8), (5, 2.0), (0.1, 0.5), ("x", 1.0), (float("nan"), 1.0)]
)
def test_clamp_rate(value: object, want: float) -> None:
    assert speech.clamp_rate({"pt": value}, "pt") == want
    assert speech.clamp_rate(None, "pt") == 1.0


def test_polly_request_uses_ssml_only_off_normal_speed_and_escapes_text() -> None:
    plain = speech.polly_request("Olá, tudo bem?")
    assert plain["VoiceId"] == "Carolina" and "TextType" not in plain
    slow = speech.polly_request("A & B <ok>", lang="en", rates={"en": 0.85})
    assert slow["TextType"] == "ssml"
    assert slow["Text"] == '<speak><prosody rate="85%">A &amp; B &lt;ok&gt;</prosody></speak>'
    assert speech.polly_request("hola", lang="xx")["LanguageCode"] in {"es-ES", "en-US"}


def test_parse_languages_drops_bad_codes() -> None:
    assert speech.parse_languages("zh-CN,en-US", ["zh-CN"]) == ["zh-CN", "en-US"]
    assert speech.parse_languages("zh-CN;rm -rf,EN", ["zh-CN"]) == ["zh-CN"]
    assert speech.parse_languages("", ["en-US"]) == ["en-US"]
    assert speech.parse_languages(None, ["en-US"]) == ["en-US"]
    assert len(speech.parse_languages(",".join(["en-US"] * 9), ["x"])) == 5
