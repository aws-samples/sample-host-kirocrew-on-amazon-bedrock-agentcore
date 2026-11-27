"""voice-call backend: Amazon Polly for speech out, Amazon Transcribe for speech in.

Both are optional. Without AWS access the page reads replies with the
browser's own voice and recognizes speech with Kiro Crew's recognizer, and the
settings page says so (GET-free: POST /api/status).

- Binds 127.0.0.1 only; the gateway reaches it through /apps/voice-call/api,
  signing every request with the X-KiroCrew-Proxy HMAC. Unsigned callers get 401.
- POST /api/tts    {"text", "voices"?, "lang"?, "rates"?} -> audio/mpeg. Each
  sentence is read by a native voice of its own language (speech.py).
- POST /api/stt[?langs=zh-CN,en-US]  body: mono PCM16 WAV -> {"text"}. Raw PCM
  into Transcribe streaming, so every browser works (iPhone records AAC, which
  Kiro Crew's own path rejects). Two or more languages turn on language
  identification, about 4 s slower per sentence.
- POST /api/voices -> the voice catalog; POST /api/lang {"text"} -> {"lang"};
  POST /api/status -> {"polly": bool, "transcribe": bool, "region", "error"?}.
- Credentials: the AWS profile named in VOICE_CALL_AWS_PROFILE or in the file
  backend/aws-profile (one line, the profile name only), otherwise the
  default chain (inside KiroCrew on AgentCore, the runtime's IAM role).
  Needed: polly:SynthesizeSpeech, polly:DescribeVoices (status check only) and
  transcribe:StartStreamTranscription.
- VOICE_CALL_AWS_REGION picks the region (default AWS_REGION, then us-east-1).
- Logs carry no recognized or spoken text: only kind, size, language, outcome.
"""

import asyncio
import hashlib
import hmac
import io
import json
import os
import pathlib
import sys
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import speech

PORT = int(os.environ.get("PORT", 9100))
SECRET = os.environ.get("KIROCREW_PROXY_SECRET", "")
MAX_TEXT = 600
MAX_BODY = 1024 * 1024  # the sandbox transport's request limit
LANGUAGES = speech.parse_languages(os.environ.get("VOICE_CALL_LANGUAGES"), ["zh-CN"])
_PROFILE_FILE = (
    pathlib.Path(__file__).resolve().parent / "aws-profile"
)  # optional, one line: a profile name
PROFILE = (
    os.environ.get("VOICE_CALL_AWS_PROFILE")
    or (_PROFILE_FILE.read_text().strip() if _PROFILE_FILE.is_file() else "")
    or None
)
REGION = (
    os.environ.get("VOICE_CALL_AWS_REGION")
    or os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "us-east-1"
)
_session = None
_polly = None
_status: tuple[float, dict] | None = None


def _log(**fields) -> None:
    print(json.dumps({"ts": round(time.time(), 1), **fields}), file=sys.stderr, flush=True)


def _boto_session():
    global _session
    if _session is None:
        if PROFILE:
            # A credential_process (ada, aws sso) must resolve without the user's shell PATH.
            extra = [
                str(pathlib.Path(p).expanduser())
                for p in ("~/.toolbox/bin", "/opt/homebrew/bin", "/usr/local/bin")
            ]
            os.environ["PATH"] = os.pathsep.join([*extra, os.environ.get("PATH", "")])
        import boto3  # the gateway interpreter ships boto3

        _session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    return _session


def _polly_client():
    global _polly
    if _polly is None:
        _polly = _boto_session().client("polly")
    return _polly


def _synth(text: str, chosen=None, lang=None, rates=None) -> bytes:
    return (
        _polly_client()
        .synthesize_speech(**speech.polly_request(text, chosen, lang, rates))["AudioStream"]
        .read()
    )


def _check_status() -> dict:
    """Is speech available? Cached for 5 minutes; costs one DescribeVoices call."""
    global _status
    if _status and time.time() - _status[0] < 300:
        return _status[1]
    out: dict = {"region": REGION, "polly": False, "transcribe": False}
    try:
        _polly_client().describe_voices(LanguageCode="cmn-CN")
        out["polly"] = True
    except Exception as e:  # no credentials, no permission, no network
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    try:
        import amazon_transcribe  # noqa: F401

        # Same credentials as Polly; the streaming call itself is the only real test.
        out["transcribe"] = out["polly"]
    except ImportError:
        out["transcribe_missing"] = (
            "amazon-transcribe is not installed (pip install amazon-transcribe)"
        )
    _status = (time.time(), out)
    return out


def _pcm_from_wav(body: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(body)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected mono 16-bit PCM WAV")
        return w.readframes(w.getnframes()), w.getframerate()


async def _transcribe_pcm(pcm: bytes, rate: int, languages: list[str]) -> str:
    from amazon_transcribe.auth import StaticCredentialResolver
    from amazon_transcribe.client import TranscribeStreamingClient
    from amazon_transcribe.handlers import TranscriptResultStreamHandler

    parts: list[str] = []

    class Collector(TranscriptResultStreamHandler):
        async def handle_transcript_event(self, transcript_event):
            for result in transcript_event.transcript.results:
                if not result.is_partial and result.alternatives:
                    parts.append(result.alternatives[0].transcript)

    resolver = None
    if PROFILE:
        # The streaming SDK has its own chain without profiles; hand it the profile's keys.
        c = _boto_session().get_credentials().get_frozen_credentials()
        resolver = StaticCredentialResolver(c.access_key, c.secret_key, c.token)
    client = (
        TranscribeStreamingClient(region=REGION, credential_resolver=resolver)
        if resolver
        else TranscribeStreamingClient(region=REGION)
    )
    # 0.6.x makes language_code a required keyword; None leaves the header out.
    options: dict = {"media_sample_rate_hz": rate, "media_encoding": "pcm", "language_code": None}
    if len(languages) >= 2:
        options.update(
            identify_language=True, language_options=languages, preferred_language=languages[0]
        )
    else:
        options["language_code"] = languages[0]
    stream = await client.start_stream_transcription(**options)

    async def write() -> None:
        for i in range(0, len(pcm), 8192):
            await stream.input_stream.send_audio_event(audio_chunk=pcm[i : i + 8192])
        await stream.input_stream.end_stream()

    await asyncio.wait_for(
        asyncio.gather(write(), Collector(stream.output_stream).handle_events()), timeout=60
    )
    return "".join(parts).strip()


def _signed(method: str, target: str, body: bytes, header: str) -> bool:
    if not SECRET or ":" not in header:
        return False
    ts, sig = header.split(":", 1)
    try:
        t = int(ts)
    except ValueError:
        return False
    now = time.time()
    if abs(now - t) > 300 and abs(now * 1000 - t) > 300_000:
        return False
    msg = f"{ts}:{method}:{target}:{hashlib.sha256(body).hexdigest()}"
    want = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, data, ctype="application/json"):
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(n, MAX_BODY)) if n else b""

    def _json(self, body: bytes) -> dict | None:
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def do_GET(self):
        if self.path in ("/health", "/api/health"):
            return self._send(200, {"status": "ok"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        body = self._body()
        if not _signed("POST", self.path, body, self.headers.get("X-KiroCrew-Proxy", "")):
            return self._send(401, {"error": "unsigned"})
        url = urlsplit(self.path)
        route = url.path
        if route == "/api/status":
            return self._send(200, _check_status())
        if route == "/api/voices":
            return self._send(200, speech.voice_catalog())
        if route == "/api/lang":
            req = self._json(body)
            if req is None:
                return self._send(400, {"error": "bad json"})
            return self._send(
                200, {"lang": speech.detect_language(str(req.get("text", ""))[:MAX_TEXT])}
            )
        if route == "/api/stt":
            langs = speech.parse_languages(
                (parse_qs(url.query).get("langs") or [None])[0], LANGUAGES
            )
            try:
                pcm, rate = _pcm_from_wav(body)
            except (wave.Error, ValueError, EOFError) as e:
                return self._send(400, {"error": f"bad audio: {e}"})
            t0 = time.time()
            try:
                text = asyncio.run(_transcribe_pcm(pcm, rate, langs))
            except Exception as e:  # credentials, network, throttling: the page says so
                _log(kind="stt", ok=False, error=type(e).__name__, bytes=len(pcm))
                return self._send(502, {"error": f"transcribe: {type(e).__name__}: {str(e)[:200]}"})
            _log(
                kind="stt",
                ok=True,
                bytes=len(pcm),
                chars=len(text),
                langs=langs,
                ms=round((time.time() - t0) * 1000),
            )
            return self._send(200, {"text": text})
        if route != "/api/tts":
            return self._send(404, {"error": "not found"})
        req = self._json(body)
        if req is None:
            return self._send(400, {"error": "bad json"})
        text = str(req.get("text", "")).strip()[:MAX_TEXT]
        if not text:
            return self._send(400, {"error": "text required"})
        chosen = req.get("voices") if isinstance(req.get("voices"), dict) else None
        lang = req.get("lang") if isinstance(req.get("lang"), str) else None
        rates = req.get("rates") if isinstance(req.get("rates"), dict) else None
        try:
            audio = _synth(text, chosen, lang, rates)
        except Exception as e:  # credentials, network, throttling: the page falls back
            _log(kind="tts", ok=False, error=type(e).__name__, chars=len(text))
            return self._send(502, {"error": f"polly: {type(e).__name__}: {str(e)[:200]}"})
        _log(kind="tts", ok=True, chars=len(text), bytes=len(audio))
        return self._send(200, audio, "audio/mpeg")

    def log_message(self, *args):
        pass


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = not sys.platform.startswith("win")


if __name__ == "__main__":
    Server(("127.0.0.1", PORT), Handler).serve_forever()
