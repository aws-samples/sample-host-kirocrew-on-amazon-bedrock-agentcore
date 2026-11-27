# 语音通话（voice-call）

A Kiro Crew app for talking to the Kiro in any chat out loud, from a phone or a laptop browser. It works on a local Kiro Crew and inside a KiroCrew on AgentCore sandbox, and it works without AWS (with simpler voices).

- **Listen**: the browser microphone and a level-based segmenter. Each segment goes as 16 kHz PCM WAV to Amazon Transcribe through the app backend, so every browser works, including iPhone Safari. If Transcribe is not available, the call bar says so and Kiro Crew's own recognizer takes over.
- **Filter**: fillers and hums ("嗯", "hmm", "NNN") and well-known recognizer hallucinations ("谢谢观看", "Thanks for watching", "YoYo Television", "Terima kasih") are never sent, in either mode. One-word answers ("行", "Sí", "OK") are.
- **Send**: the recognized text goes to the current chat (`POST /api/chat`) with a 🎙 prefix and a short "answer briefly, spoken style" hint in the interface language, so memory, tools and approvals work exactly as for a typed message.
- **Speak**: the reply is cleaned of tables, code and links, split into sentences, and read with Amazon Polly. Each sentence gets a native voice for its own language (Chinese, Japanese, Korean, Arabic, English, Portuguese, Spanish, French, Italian, German); the next sentence is synthesized while the current one plays. A sentence Polly cannot read is read by the browser voice.
- **Voice and speed per language**: the app page lists every Polly voice per language with a preview button. During a call, say "说慢一点 / slower", "说快一点 / faster" or "正常语速 / normal speed" to change the speed of the language just spoken; the command is not sent to Kiro.
- **Fast replies** (optional): during a call the chat's reasoning effort is set to `low`, and restored on hang-up.
- **Languages**: the interface is Chinese or English, following the browser (switchable on the app page). Recognition defaults to the interface language; list more Transcribe codes on the app page (for example `zh-CN,en-US`) to turn on language identification.

## Talking

- **Manual** (default): on a computer click **Talk** / **说** or press ⌥Space, then **Send** / **发送** or press it again; Esc discards. Hold the hotkey to talk and release to send. On a phone, hold the large button, release to send, slide off to discard. Nothing is heard in between, so people nearby never reach Kiro.
- **Automatic**: always listening. Sending is timed from the last *recognized words*, not from sound, so background noise cannot hold a message back: it goes out a few seconds (default 5) after the last new words. Speaking while Kiro talks interrupts it.
- Phones stop the microphone when the page goes to the background; keep the page in front. The app holds a [Screen Wake Lock](https://developer.mozilla.org/docs/Web/API/Screen_Wake_Lock_API) and reopens the microphone when the page comes back. The first tap unlocks audio playback.

## AWS (optional)

The app page shows whether Polly and Transcribe are connected.

- **Permissions**: `polly:SynthesizeSpeech`, `polly:DescribeVoices` (the status check) and `transcribe:StartStreamTranscription`. On KiroCrew on AgentCore set `enable_voice_call_speech = true` in your Terraform variables.
- **Credentials**: the default AWS credential chain (inside a sandbox, the runtime role). Locally you can name a profile instead, either in the environment variable `VOICE_CALL_AWS_PROFILE` or in a one-line file `backend/aws-profile` containing only the profile name (git-ignored). The app never stores keys.
- **Region**: `VOICE_CALL_AWS_REGION`, else `AWS_REGION`, else `us-east-1`.
- **Transcribe client**: the backend needs the `amazon-transcribe` Python package; KiroCrew on AgentCore images ship it through `kirocrew[voice-aws]`.
- Language identification (two or more recognition languages) measured about 4 s slower per sentence than a single language.

## Install

From a terminal (inside the sandbox for KiroCrew on AgentCore):

```bash
git clone --depth 1 https://github.com/aws-samples/sample-host-kirocrew-on-amazon-bedrock-agentcore ~/workplace/kirocrew-samples
```

Then in the dashboard: **Apps → Discover → Manage app sources → Install from Path**, enter the full path to `kirocrew-samples/examples/apps/voice-call`, and enable and trust it in **Library**. Kiro Crew refuses app installs from the terminal; installing is always the owner's own action. To update, pull the clone and choose **Sync** on the app in Library.

The app ships a backend (`backend/server.py`), so it is third-party executable code: Kiro Crew blocks it until you trust it. Choosing **Trust this app and enable** in Library sets that trust for this app only; to allow third-party apps globally instead, set `agent.apps_allow_third_party` to `true`.

On KiroCrew on AgentCore, the deployment must tunnel app routes (`/apps/{name}/api|ui|art/*`, see `docs/security.md`) and allow the microphone (`Permissions-Policy: microphone=(self)`).

The built UI (`ui/dist`) is committed so no Node build is needed. To rebuild: `cd ui && npm ci && npm run build`.

## Privacy

Audio is never stored. Backend logs record only the kind of request, sizes, languages and outcome, never recognized or spoken text.

## Tests

- `tests/unit/test_voice_call_speech.py`: language detection, voice choice, speed, Transcribe language parsing.
- `examples/apps/voice-call/ui/tests/text.test.ts`: filters, hallucinations, speed commands, auto-send timing, read-aloud cleanup.

## Limits

- One upload is at most about 25 seconds of audio (the sandbox transport accepts 1 MiB per request); longer speech is split into segments that join the same message.
- Replies are read once the whole turn has finished.
- If the page is closed during a call, the chat's reasoning effort stays at `low` until you change it or start and end another call.
- The browser fallback voice picks by writing system only: Latin-script sentences use an English voice.
