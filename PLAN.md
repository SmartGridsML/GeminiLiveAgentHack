# PitchMirror — Build Plan

## What We're Building
Real-time AI presentation coach. User practices a pitch on camera. The agent watches
(webcam, 1fps) + listens (mic, continuous), detects problems (filler words, eye contact
drops, pace spikes, logical gaps), interrupts with voice coaching, generates a post-session
scorecard. Category: Live Agents. Target: $10k Live Agents prize + Grand Prize viable.

---

## Verified Model Stack
```
LIVE_MODEL = gemini-2.5-flash-native-audio-preview-12-2025
CORE_MODEL = gemini-2.5-flash
IMAGE_MODEL = imagen-4.0-fast-generate-001  (not used in PitchMirror MVP)
```

## Key Live API Facts (Verified)
- Audio+video session limit: 2 min → solved by `context_window_compression` (sliding window)
- Session resumption tokens valid 2 hrs → use for graceful reconnect fallback
- Video: max 1 FPS (JPEG frames) — sufficient for posture/eye contact detection
- Native audio models: AUDIO response modality + transcription for text
- Input audio: raw 16-bit PCM, 16kHz, little-endian
- Output audio: 24kHz

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (index.html + app.js)                              │
│  getUserMedia() → webcam + mic                              │
│  WebSocket ←──────────────────────────────────→ Backend     │
│  Web Audio API playback ← coach audio                       │
│  Scorecard UI ← session analytics JSON                      │
└─────────────────────────────────────────────────────────────┘
                          │ WebSocket (ws://)
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  Cloud Run: FastAPI + Uvicorn                               │
│                                                             │
│  /ws  ← WebSocket endpoint                                  │
│    ├── AudioBridge: relays PCM chunks → Gemini Live         │
│    ├── VideoBridge: captures JPEG frames at 1fps → Gemini   │
│    ├── CoachingEngine: system prompt + interruption logic   │
│    ├── ScorecardBuilder: tracks metrics across session      │
│    └── SessionManager: compression config + resumption      │
│                                                             │
│  Gemini Live API ←──────────────────────────────────────→   │
│  gemini-2.5-flash-native-audio-preview-12-2025              │
└──────────────────────────────┬──────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │     Firestore        │
                    │  sessions/{id}       │
                    │  - coaching_events[] │
                    │  - filler_count      │
                    │  - eye_contact_drops │
                    │  - pace_violations   │
                    │  - transcript        │
                    └─────────────────────┘
```

---

## Directory Structure
```
GeminiLiveAgentHack/
├── backend/
│   ├── main.py              # FastAPI app, WebSocket endpoint
│   ├── gemini_live.py       # Gemini Live API session wrapper
│   ├── coach.py             # System prompt, interruption engine, metric tracking
│   ├── scorecard.py         # Post-session analytics builder
│   └── db.py                # Firestore client
├── frontend/
│   ├── index.html           # Single page app
│   ├── style.css            # Clean, dark coaching UI
│   └── app.js               # WebSocket client, media capture, audio playback
├── infra/
│   ├── main.tf              # Cloud Run + Firestore + Secret Manager
│   ├── variables.tf
│   └── outputs.tf
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

---

## Phases

### Phase 1 — Foundation (scaffold + deps)
- [ ] `requirements.txt` with pinned deps
- [ ] `backend/main.py`: FastAPI app, health endpoint, WebSocket stub
- [ ] `.env.example`
- [ ] `Dockerfile`
- [ ] `git init` + initial commit

### Phase 2 — Gemini Live Core
- [ ] `backend/gemini_live.py`: session open/close, audio send, video frame send, receive loop
- [ ] Context window compression config (solves 2-min limit)
- [ ] Session resumption token storage (fallback reconnect)
- [ ] `backend/coach.py`: system prompt engineering, metric tracking
- [ ] Bidirectional relay: browser audio → Gemini → browser audio playback

### Phase 3 — Frontend
- [ ] `frontend/index.html`: layout (video preview, status bar, scorecard panel)
- [ ] `frontend/app.js`: getUserMedia, WebSocket protocol, PCM capture, JPEG frame capture, audio playback
- [ ] `frontend/style.css`: dark coaching UI, live metrics display

### Phase 4 — Scorecard + Firestore
- [ ] `backend/db.py`: Firestore session write/read
- [ ] `backend/scorecard.py`: build scorecard JSON from session events
- [ ] Frontend scorecard render: filler count, eye contact drops, pace bars, overall score

### Phase 5 — Cloud Deployment
- [ ] Finalize `Dockerfile`
- [ ] `infra/main.tf`: Cloud Run service + Firestore + Secret Manager IAM
- [ ] Deploy to Cloud Run, confirm live URL

### Phase 6 — Demo Polish + README
- [ ] `README.md`: architecture, spin-up in ≤10 steps
- [ ] Architecture diagram (ASCII + Excalidraw link)
- [ ] Demo script: 90-second pitch with 4 planted errors

---

## Coaching System Prompt (v1)
```
You are PitchMirror, a brutally honest but constructive real-time presentation coach.

You are listening to and watching a person practice their presentation.

ONLY interrupt when you detect one of these problems:
1. FILLER WORDS: "um", "uh", "like", "you know", "basically", "literally" (3+ in 30s)
2. PACE: speaking faster than ~180 WPM for more than 10 seconds
3. EYE CONTACT: presenter looks down or away from camera for more than 5 seconds
4. CONTRADICTION: a statement clearly contradicts something said in the last 2 minutes
5. CLARITY: a sentence is so convoluted it loses all meaning

When you interrupt:
- Keep it under 10 words. Be specific. Be actionable.
- Examples: "Slow down — you're rushing." / "Look at the camera." / "Too many 'ums' — pause instead."
- Do NOT interrupt more than once every 30 seconds.
- Do NOT comment on content unless there is a clear logical contradiction.

If the presenter is doing well, stay silent. Silence is a compliment.
```

---

## Session Protocol (WebSocket message types)
```
Client → Server:
  { type: "audio", data: "<base64 PCM>" }
  { type: "video", data: "<base64 JPEG>" }
  { type: "start", session_id: "<uuid>" }
  { type: "stop" }

Server → Client:
  { type: "coach_audio", data: "<base64 PCM 24kHz>" }
  { type: "transcript", text: "...", speaker: "user|coach" }
  { type: "metric", key: "filler_count|eye_drops|pace_flags", value: N }
  { type: "scorecard", data: { ... } }
  { type: "status", state: "connected|listening|coaching|reconnecting" }
```

---

## Go/No-Go Gate (run before building)
```python
# Run this first. If p95 > 3s on round-trip, reassess.
import asyncio, time
from google import genai
from google.genai import types

async def smoke_test():
    client = genai.Client()
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()
        )
    )
    times = []
    for i in range(5):
        start = time.time()
        async with client.aio.live.connect(
            model="gemini-2.5-flash-native-audio-preview-12-2025",
            config=config
        ) as session:
            await session.send_realtime_input(text="Say: ready")
            async for resp in session.receive():
                if resp.server_content:
                    times.append(time.time() - start)
                    break
    print(f"p50: {sorted(times)[len(times)//2]:.2f}s, p95: {max(times):.2f}s")

asyncio.run(smoke_test())
```

---

## Submission Checklist
- [ ] Public GitHub repo with README spin-up ≤10 steps
- [ ] Cloud Run service URL live and callable
- [ ] Architecture diagram in repo
- [ ] Demo video ≤4 min, all live (no mockups)
- [ ] IaC Terraform scripts in `/infra`
- [ ] `#GeminiLiveAgentChallenge` social post

---

## Demo Script (90-second pitch with 4 planted errors)
```
"So basically um, our platform is like a next-generation solution for enterprise
customers. [PACE SPIKE] The core value proposition is that we help companies grow
their revenue by optimizing their workflow automation pipelines [LOOK DOWN 6s].
We believe that fundamentally, the key insight is that AI will transform every
single business process in every company everywhere. Um, and um, our technology
does this better than anyone. [CONTRADICTION] As I mentioned, we're the first to
market — and actually there are several competitors but we're the best."
```
Expected interruptions:
1. ~0:05 — "Too many 'ums' — pause instead."
2. ~0:15 — "Slow down — you're rushing."
3. ~0:30 — "Look at the camera."
4. ~0:55 — "That contradicts your 'first to market' claim."
