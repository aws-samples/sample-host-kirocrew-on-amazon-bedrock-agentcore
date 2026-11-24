"""voice-call backend (cloud edition): speak replies with Amazon Polly (neural voice Zhiyu).

- Binds 127.0.0.1 only; the gateway reaches it through its /apps/voice-call/api
  proxy, which signs every request with the X-KiroCrew-Proxy HMAC. Unsigned
  callers get 401.
- POST /api/tts {"text": "..."} -> audio/mpeg
- Credentials come from the default AWS credential chain, which inside a
  KiroCrew on AgentCore sandbox is the runtime's IAM role. It needs
  polly:SynthesizeSpeech (Terraform: enable_voice_call_speech = true).
- VOICE_CALL_AWS_REGION picks the Polly region (default: the sandbox's own
  region). Any failure returns an error and the browser speaks that sentence
  with its own voice instead.
"""

import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

PORT = int(os.environ.get("PORT", 9100))
SECRET = os.environ.get("KIROCREW_PROXY_SECRET", "")
MAX_TEXT = 600
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
        return self.rfile.read(min(n, 64 * 1024)) if n else b""

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        body = self._body()
        if not _signed("POST", self.path, body, self.headers.get("X-KiroCrew-Proxy", "")):
            return self._send(401, {"error": "unsigned"})
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
