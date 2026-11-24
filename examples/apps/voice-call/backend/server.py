"""voice-call backend (cloud edition): speak replies with Amazon Polly (neural voice Zhiyu).

- Binds 127.0.0.1 only; the gateway reaches it through its /apps/voice-call/api
  proxy, which signs every request with the X-KiroCrew-Proxy HMAC. Unsigned
  callers get 401.
- POST /api/tts {"text": "..."} -> audio/mpeg
- POST /api/stt (body: mono PCM16 WAV) -> {"text": "..."}. Amazon Transcribe
  streaming, sent as raw PCM, so every browser works (an iPhone records AAC,
  which Kiro Crew's own Transcribe path does not accept). VOICE_CALL_LANGUAGES
  lists the spoken languages (default "zh-CN", which also
  catches common English words). Two or more turn on language identification,
  which adds about 4 s per sentence.
- Both SDKs use their default AWS credential chain, which inside a KiroCrew on
  AgentCore sandbox is the runtime's IAM role. It needs polly:SynthesizeSpeech
  and transcribe:StartStreamTranscription (Terraform:
  enable_voice_call_speech = true).
- VOICE_CALL_AWS_REGION picks the region (default: the sandbox's own region).
  A failed sentence is read by the browser voice; a failed recognition is
  reported in the call bar, and the page then tries Kiro Crew's recognizer.
"""

import asyncio
import hashlib
import hmac
import io
import json
import os
import sys
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

PORT = int(os.environ.get("PORT", 9100))
SECRET = os.environ.get("KIROCREW_PROXY_SECRET", "")
MAX_TEXT = 600
MAX_BODY = 1024 * 1024  # the sandbox transport's request limit
LANGUAGES = [
    code.strip()
    for code in os.environ.get("VOICE_CALL_LANGUAGES", "zh-CN").split(",")
    if code.strip()
] or ["zh-CN"]
POLLY_REGION = (
    os.environ.get("VOICE_CALL_AWS_REGION")
    or os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "us-east-1"
)
_polly = None


def _polly_client():
    global _polly
    if _polly is None:
        import boto3  # the gateway interpreter ships boto3

        _polly = boto3.Session(region_name=POLLY_REGION).client("polly")
    return _polly


def _synth_polly(text: str) -> bytes:
    r = _polly_client().synthesize_speech(
        Text=text, VoiceId="Zhiyu", Engine="neural", OutputFormat="mp3", LanguageCode="cmn-CN"
    )
    return r["AudioStream"].read()


def _pcm_from_wav(body: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(body)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected mono 16-bit PCM WAV")
        return w.readframes(w.getnframes()), w.getframerate()


async def _transcribe_pcm(pcm: bytes, rate: int) -> str:
    from amazon_transcribe.client import TranscribeStreamingClient
    from amazon_transcribe.handlers import TranscriptResultStreamHandler

    parts: list[str] = []

    class Collector(TranscriptResultStreamHandler):
        async def handle_transcript_event(self, transcript_event):
            for result in transcript_event.transcript.results:
                if not result.is_partial and result.alternatives:
                    parts.append(result.alternatives[0].transcript)

    client = TranscribeStreamingClient(region=POLLY_REGION)
    # 0.6.x makes language_code a required keyword; None leaves the header out.
    options: dict = {"media_sample_rate_hz": rate, "media_encoding": "pcm", "language_code": None}
    if len(LANGUAGES) >= 2:
        options.update(
            identify_language=True,
            language_options=LANGUAGES,
            preferred_language=LANGUAGES[0],
        )
    else:
        options["language_code"] = LANGUAGES[0]
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

    def do_GET(self):
        if self.path in ("/health", "/api/health"):
            return self._send(200, {"status": "ok"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        body = self._body()
        if not _signed("POST", self.path, body, self.headers.get("X-KiroCrew-Proxy", "")):
            return self._send(401, {"error": "unsigned"})
        if self.path == "/api/stt":
            try:
                pcm, rate = _pcm_from_wav(body)
            except (wave.Error, ValueError, EOFError) as e:
                return self._send(400, {"error": f"bad audio: {e}"})
            try:
                return self._send(200, {"text": asyncio.run(_transcribe_pcm(pcm, rate))})
            except Exception as e:  # credentials, network, throttling: the page says so
                return self._send(502, {"error": f"transcribe: {type(e).__name__}: {str(e)[:200]}"})
        if self.path != "/api/tts":
            return self._send(404, {"error": "not found"})
        try:
            text = str(json.loads(body or b"{}").get("text", "")).strip()[:MAX_TEXT]
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if not text:
            return self._send(400, {"error": "text required"})
        try:
            return self._send(200, _synth_polly(text), "audio/mpeg")
        except Exception as e:  # credentials, network, throttling: client falls back
            return self._send(502, {"error": f"polly: {type(e).__name__}: {str(e)[:200]}"})

    def log_message(self, *args):
        pass


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = not sys.platform.startswith("win")


if __name__ == "__main__":
    Server(("127.0.0.1", PORT), Handler).serve_forever()
