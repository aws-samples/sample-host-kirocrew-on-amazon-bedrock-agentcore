# ruff: noqa: RUF001
"""Pure speech helpers for the voice-call backend: language detection and voice choice.

No AWS or network access here, so the unit tests can import it anywhere.
"""

from xml.sax.saxutils import escape

# One native voice per language: (VoiceId, engine, LanguageCode). Generative
# where Polly has it, neural otherwise.
VOICES = {
    "zh": ("Zhiyu", "neural", "cmn-CN"),
    "ja": ("Kazuha", "neural", "ja-JP"),
    "ko": ("Seoyeon", "generative", "ko-KR"),
    "ar": ("Hala", "neural", "ar-AE"),
    "en": ("Ruth", "generative", "en-US"),
    "pt": ("Carolina", "generative", "pt-BR"),
    "es": ("Lucia", "generative", "es-ES"),
    "fr": ("Lea", "generative", "fr-FR"),
    "it": ("Bianca", "generative", "it-IT"),
    "de": ("Vicki", "generative", "de-DE"),
}

# Every voice the settings page may offer: (VoiceId, engine, LanguageCode, gender f|m).
CHOICES = {
    "zh": [("Zhiyu", "neural", "cmn-CN", "f")],
    "ja": [
        ("Kazuha", "neural", "ja-JP", "f"),
        ("Tomoko", "neural", "ja-JP", "f"),
        ("Takumi", "neural", "ja-JP", "m"),
    ],
    "ko": [
        ("Seoyeon", "generative", "ko-KR", "f"),
        ("Hyunduk", "generative", "ko-KR", "m"),
        ("Jihye", "neural", "ko-KR", "f"),
    ],
    "ar": [("Hala", "neural", "ar-AE", "f"), ("Zayd", "neural", "ar-AE", "m")],
    "en": [
        (v, "generative", "en-US", g)
        for v, g in (
            ("Ruth", "f"),
            ("Danielle", "f"),
            ("Joanna", "f"),
            ("Tiffany", "f"),
            ("Salli", "f"),
            ("Matthew", "m"),
            ("Stephen", "m"),
            ("Kenneth", "m"),
            ("Nate", "m"),
        )
    ],
    "pt": [
        ("Carolina", "generative", "pt-BR", "f"),
        ("Camila", "generative", "pt-BR", "f"),
        ("Leo", "generative", "pt-BR", "m"),
        ("Vitoria", "neural", "pt-BR", "f"),
        ("Thiago", "neural", "pt-BR", "m"),
    ],
    "es": [
        ("Lucia", "generative", "es-ES", "f"),
        ("Sergio", "generative", "es-ES", "m"),
        ("Lupe", "generative", "es-US", "f"),
        ("Pedro", "generative", "es-US", "m"),
        ("Carlos", "generative", "es-US", "m"),
    ],
    "fr": [
        (v, "generative", "fr-FR", g)
        for v, g in (("Lea", "f"), ("Celine", "f"), ("Ambre", "f"), ("Remi", "m"), ("Florian", "m"))
    ],
    "it": [
        ("Bianca", "generative", "it-IT", "f"),
        ("Beatrice", "generative", "it-IT", "f"),
        ("Lorenzo", "generative", "it-IT", "m"),
        ("Adriano", "neural", "it-IT", "m"),
    ],
    "de": [
        (v, "generative", "de-DE", g)
        for v, g in (("Vicki", "f"), ("Tina", "f"), ("Daniel", "m"), ("Lennart", "m"))
    ],
}

SAMPLES = {
    "zh": "你好，今天很高兴跟你聊天。",
    "ja": "こんにちは、今日はお話しできてとても嬉しいです。",
    "ko": "안녕하세요, 오늘 이야기하게 되어 정말 기쁩니다.",
    "ar": "مرحبا، يسعدني جدا التحدث معك اليوم.",
    "en": "Hello, it's great to talk with you today.",
    "pt": "Olá, que bom falar com você hoje.",
    "es": "¡Hola! Me alegra mucho hablar contigo hoy.",
    "fr": "Bonjour, je suis très contente de vous parler aujourd'hui.",
    "it": "Ciao, sono molto contenta di parlare con te oggi.",
    "de": "Hallo, ich freue mich sehr, heute mit dir zu sprechen.",
}

_STOP = {
    "pt": (
        "não você está são bom dia olá obrigada obrigado tudo uma para com que de os "
        "as em do da dos das muito também isso então mas como ele ela"
    ).split(),
    "es": (
        "el los las una para con que de del está son muy gracias también pero como "
        "por qué es y usted hola me contigo"
    ).split(),
    "fr": (
        "le les une des pour avec que de du est sont très merci aussi mais comme vous "
        "nous je et dans bonjour"
    ).split(),
    "it": (
        "il gli una per con che di del è sono molto grazie anche ma come lei noi io e "
        "nella questo ciao"
    ).split(),
    "de": (
        "der die das ein eine und ist sind mit für nicht sehr danke dank vielen bitte "
        "du mir kannst hallo auch aber wie sie wir ich zu"
    ).split(),
    "en": (
        "the a an and is are with for not very thanks also but how you we i to of this that hello"
    ).split(),
}
_MARKS = {"pt": "ãõç", "es": "ñ¿¡", "fr": "èêëàâîïôûùœ", "it": "àèìòù", "de": "äöüß"}


def detect_language(text: str, fallback: str = "zh") -> str:
    """Pick the sentence's language from its script, then from common words and accents."""
    if any("\u3040" <= c <= "\u30ff" for c in text):
        return "ja"
    if any("\uac00" <= c <= "\ud7af" for c in text):
        return "ko"
    if any("\u0600" <= c <= "\u06ff" for c in text):
        return "ar"
    if any("\u4e00" <= c <= "\u9fff" for c in text):
        return "zh"
    low = text.lower()
    words = [w.strip(".,!?;:\"'()¿¡") for w in low.split()]
    if not any(w.isalpha() for w in words):
        return fallback
    score = {lang: sum(w in stop for w in words) * 2 for lang, stop in _STOP.items()}
    for lang, marks in _MARKS.items():
        score[lang] += sum(c in marks for c in low)
    best = max(score, key=lambda k: score[k])
    return best if score[best] > 0 else "en"


def voice_catalog() -> dict:
    return {
        lang: {
            "sample": SAMPLES[lang],
            "default": VOICES[lang][0],
            "options": [{"id": v, "gender": g, "engine": e} for v, e, _c, g in opts],
        }
        for lang, opts in CHOICES.items()
    }


def pick_voice(lang: str, chosen: dict | None) -> tuple[str, str, str]:
    """The user's choice for this language when it is a known voice, else the default."""
    want = (chosen or {}).get(lang)
    for v, e, c, _g in CHOICES.get(lang, []):
        if v == want:
            return v, e, c
    return VOICES[lang]


def clamp_rate(rates: dict | None, lang: str) -> float:
    try:
        r = float((rates or {}).get(lang, 1))
    except (TypeError, ValueError):
        return 1.0
    if r != r:  # NaN
        return 1.0
    return min(2.0, max(0.5, r))


def polly_request(
    text: str, chosen: dict | None = None, lang: str | None = None, rates: dict | None = None
) -> dict:
    """The SynthesizeSpeech arguments for one sentence."""
    lang = lang if lang in VOICES else detect_language(text)
    voice, engine, code = pick_voice(lang, chosen)
    req = {
        "VoiceId": voice,
        "Engine": engine,
        "OutputFormat": "mp3",
        "LanguageCode": code,
        "Text": text,
    }
    r = clamp_rate(rates, lang)
    if abs(r - 1) > 0.01:
        req.update(
            Text=f'<speak><prosody rate="{round(r * 100)}%">{escape(text)}</prosody></speak>',
            TextType="ssml",
        )
    return req


_TRANSCRIBE_CODE = __import__("re").compile(r"^[a-z]{2}-[A-Z]{2}$")


def parse_languages(raw: str | None, default: list[str]) -> list[str]:
    """Transcribe language codes from a comma list like 'zh-CN,en-US'; bad entries dropped."""
    if not raw:
        return default
    out = [c.strip() for c in raw.split(",") if _TRANSCRIBE_CODE.match(c.strip())]
    return out[:5] or default
