# 语音通话（voice-call）— KiroCrew on AgentCore 手机版

A Kiro Crew app for talking to the Kiro in any chat out loud, from a phone or a laptop browser. It runs inside your sandbox like any installed app.

- **Listen**: the browser microphone, a level-based segmenter, and 16 kHz WAV uploads to Kiro Crew's own recognizer (`POST /api/stt/transcribe`) inside the sandbox. Traditional characters are converted to Simplified.
- **Send**: the recognised text goes to the current chat (`POST /api/chat`) with a 🎙️ prefix and a "answer briefly, spoken style" hint, so memory, tools and approvals work exactly as for a typed message.
- **Speak**: the reply is cleaned of tables, code and links, split into sentences, and read with Amazon Polly (neural voice Zhiyu) through the app backend. A sentence Polly cannot synthesise is read by the browser voice instead.
- **Fast replies** (optional): during a call the chat's reasoning effort is set to `low`, and restored on hang-up.

## Phone

- Hold the large **按住说话** button to talk; let go to send; slide well off the button to discard.
- A phone browser stops the microphone when the page goes to the background or the screen locks. Keep the page in front; the app holds a [Screen Wake Lock](https://developer.mozilla.org/docs/Web/API/Screen_Wake_Lock_API) during a call where the browser supports it. When the page comes back, the microphone is reopened.
- The first tap also unlocks audio playback, which mobile browsers require before a page may play sound.

## Requirements

1. **The deployment tunnels app routes.** The frontend shell and the adapter forward an installed app's `/apps/{name}/api/*`, `/apps/{name}/ui/*` and `/apps/{name}/art/*` into the sandbox (see `docs/security.md`). Deployments older than this change cannot load any installed app's UI.
2. **Speech permissions.** Set `enable_voice_call_speech = true` in your Terraform variables. It adds `polly:SynthesizeSpeech` and `transcribe:StartStreamTranscription` to the runtime role. Without it every reply is read by the browser voice, and recognition falls back to Kiro Crew's own recognizer (the call bar says so).
3. **Speech-to-text.** The app backend sends each segment to Amazon Transcribe streaming as 16 kHz PCM, so it works with every browser, including iPhone Safari. `VOICE_CALL_LANGUAGES` (default `zh-CN`) lists the spoken languages. Two or more, such as `zh-CN,en-US`, turn on language identification, which measured about 4 s slower per sentence (5.6 s against 1.2 s for a 5 s clip). The runtime image ships the `amazon-transcribe` client through `kirocrew[voice-aws]`.

## Install

Inside the sandbox, from the Kiro Crew terminal:

```bash
git clone --depth 1 https://github.com/aws-samples/sample-host-kirocrew-on-amazon-bedrock-agentcore ~/workplace/kirocrew-samples
```

Then in the dashboard: **Apps → Discover → Install from local path**, choose `~/workplace/kirocrew-samples/examples/apps/voice-call`, and enable and trust it in **Library**. Kiro Crew refuses app installs from the terminal; installing is always the owner's own action.

The built UI (`ui/dist`) is committed so the sandbox needs no Node build. To rebuild:

```bash
cd ui && npm ci && npm run build
```

## Limits

- A single recorded segment is capped at 25 seconds (the sandbox transport accepts at most 1 MiB per request). Longer speech is split at pauses automatically.
- Replies are read once the whole turn has finished.
- If the page is closed during a call, the chat's reasoning effort stays at `low` until you change it or start and end another call.
