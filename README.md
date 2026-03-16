# PitchMirror

> Real-time AI speaking coach. Rehearse talks, interviews, demos, or pitches while PitchMirror listens, watches, and interrupts with measured feedback.

**Category:** Live Agents — [Gemini Live Agent Challenge](https://ai.google.dev/competition/live-agent)

[![Architecture](docs/architecture.png)](docs/architecture.png)

> Detailed view: [docs/architecture_detailed.png](docs/architecture_detailed.png)

---

## What it does

PitchMirror uses the **Gemini Live API** to simultaneously watch your webcam, listen to your microphone, and optionally analyze your shared screen while you speak.
You can also upload a slide deck PDF (from PowerPoint/Canva/Keynote export) so the coach can critique actual slide visuals in real time.
Before each run it captures a short intake:
- coach persona (`Professional Coach`, `Brutal VC`, `Supportive Mentor`)
- what you are preparing for
- delivery context (`virtual`, `in_person`, `hybrid`)
- primary goal (`balanced`, fillers, pacing, confidence, structure)

When it detects:

| Problem | Trigger | Example coaching response |
|---------|---------|--------------------------|
| Filler words | 3+ "um/uh/like" in 30s | *"Three 'ums' in ten seconds. Pause instead."* |
| Pace | >180 WPM for 10s | *"Slow down — you're rushing."* |
| Eye contact | Prolonged disengaged gaze (context-aware threshold) | *"Reconnect with the audience/camera."* |
| Contradiction | Statement contradicts prior claim | *"That contradicts your 'first to market' claim."* |
| Clarity | Incomprehensible sentence | *"That sentence lost everyone. Say it in one clause."* |
| Slide clarity (optional screen share) | clutter/unreadable/weak hierarchy | *"Slide is text-dense. Cut to three bullets."* |
| Slide-speech mismatch (optional) | narration and slide conflict | *"Narration and slide are misaligned."* |

After your session: a **scorecard** with per-category scores, timeline events, AI report, citations, and 2 generated visual coaching cards.

UI action path: the agent can execute real frontend actions, including advancing the built-in **Practice Deck** via `navigate_practice_slides`.

Eye-contact handling is context-aware:
- `virtual`: camera-facing is prioritized.
- `in_person`: natural audience scanning is not penalized.
- `hybrid`: balanced room scanning plus periodic camera reconnection.

---

## Architecture

```
Browser (HTML/JS)
  ├── getUserMedia()  → webcam + mic
  ├── getDisplayMedia() (optional) → screen share
  ├── PCM 16kHz audio → WebSocket → Cloud Run
  ├── Webcam JPEG @1fps  → WebSocket → Cloud Run
  ├── Screen JPEG @0.5fps → WebSocket → Cloud Run
  └── Coach audio 24kHz ← WebSocket ← Cloud Run
            │
    ┌───────▼────────────────────────────────┐
    │  Cloud Run (FastAPI + Uvicorn)          │
    │  WebSocket /ws                          │
    │    ├── AudioBridge → Gemini Live API    │
    │    ├── VideoBridge → Gemini Live API    │
    │    ├── CoachingEngine (mode-aware prompt)│
    │    ├── Screen Clarity Tool + Demo Mode  │
    │    ├── Imagen post-session visual cards │
    │    └── ScorecardBuilder                 │
    └───────┬────────────────────────────────┘
            │
    ┌───────▼────────────┐   ┌─────────────────────┐
    │  Gemini Live API   │   │  Firestore           │
    │  gemini-2.5-flash  │   │  Session scorecards  │
    │  -native-audio-    │   └─────────────────────┘
    │  preview-12-2025   │
    └────────────────────┘
```

**Google Cloud services used:**
- Cloud Run (backend hosting)
- Firestore (session persistence)
- Secret Manager (API key management)
- Artifact Registry (container images)

---

## Prerequisites

- Python 3.12+
- A [Gemini API key](https://aistudio.google.com/app/apikey) with access to `gemini-2.5-flash-native-audio-preview-12-2025`
- Docker (for deployment)
- `gcloud` CLI + `terraform` (for cloud deployment)

---

## Local development (5 steps)

```bash
# 1. Clone and enter the repo
git clone https://github.com/YOUR_USERNAME/pitchmirror
cd pitchmirror

# 2. Create virtual environment and install deps
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Set your API key
cp .env.example .env
# Edit .env and set GOOGLE_API_KEY=your_key_here

# 4. Run the smoke test (validates Live API access before building)
python scripts/smoke_test.py

# 5. Start the server
uvicorn backend.main:app --host 0.0.0.0 --port 8080 --reload
# Open: http://localhost:8080
```

---

## Reproducible testing (judge checklist)

Use this exact sequence to validate the project end-to-end in ~10 minutes.

### A) Backend health + model access

```bash
# 1) Health check (must return {"status":"ok"...})
curl -s http://localhost:8080/health

# 2) Gemini connectivity smoke test (must print success, no traceback)
python scripts/smoke_test.py
```

Expected result:
- `/health` returns JSON with `"status": "ok"`.
- `smoke_test.py` completes without errors.

### B) UI + live agent behavior

1. Open `http://localhost:8080/app`.
2. Session intake:
   - Mode: `presentation`
   - Delivery context: `virtual`
   - Toggle `Screen-aware coaching` ON (optional)
   - Toggle `Deterministic demo mode` ON
3. Click `Start Session`.
4. Speak this planted test script:

```text
So basically um, today I want to share our onboarding approach.
[LOOK DOWN at notes for 6+ seconds]
Our process is kind of sort of basically simple and literally works for everyone.
[SPEAK VERY FAST for ~10 seconds]
```

Expected result:
- Live interruptions appear in transcript and audio.
- ADK tool-call lines appear (for metrics and flags).
- Evidence chips show threshold/metric context on `flag_issue`.

### C) Post-session output

1. Click `End Session`.
2. Wait for pipeline steps to complete.

Expected result:
- Scorecard appears with category scores.
- `AI Coaching Report` section renders.
- `Evidence-Based Techniques` section renders.
- `Multimodal Visuals` section renders up to 2 generated cards (or fallback cards if image generation is unavailable).

### D) Optional API verification

```bash
# Recent sessions summary
curl -s http://localhost:8080/api/sessions?limit=3

# Session aggregates
curl -s http://localhost:8080/api/sessions/stats?limit=10
```

If `API_BEARER_TOKEN` is set, include auth headers:

```bash
curl -s -H "Authorization: Bearer $API_BEARER_TOKEN" http://localhost:8080/api/sessions?limit=3
```

Expected result:
- Both endpoints return valid JSON.
- Latest session appears in `/api/sessions`.

---

## Cloud Run deployment

### Option A: Manual (gcloud)

```bash
# Set your project
export PROJECT_ID=your-project-id
export REGION=us-central1
export GEMINI_API_KEY=your_gemini_api_key
export API_BEARER_TOKEN=$(openssl rand -hex 24)

# Enable APIs
gcloud services enable run.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com \
  --project=$PROJECT_ID

# Create Artifact Registry repo
gcloud artifacts repositories create pitchmirror \
  --repository-format=docker --location=$REGION --project=$PROJECT_ID

# Build and push container
docker build -t $REGION-docker.pkg.dev/$PROJECT_ID/pitchmirror/app:latest .
docker push $REGION-docker.pkg.dev/$PROJECT_ID/pitchmirror/app:latest

# Store API key in Secret Manager (create secret once, then add a version)
if ! gcloud secrets describe pitchmirror-gemini-api-key --project=$PROJECT_ID >/dev/null 2>&1; then
  gcloud secrets create pitchmirror-gemini-api-key \
    --replication-policy=automatic --project=$PROJECT_ID
fi
printf '%s' "$GEMINI_API_KEY" | gcloud secrets versions add pitchmirror-gemini-api-key \
  --data-file=- --project=$PROJECT_ID

# Deploy to Cloud Run
gcloud run deploy pitchmirror \
  --image=$REGION-docker.pkg.dev/$PROJECT_ID/pitchmirror/app:latest \
  --region=$REGION \
  --set-env-vars=GOOGLE_CLOUD_PROJECT=$PROJECT_ID,CORS_ALLOWED_ORIGINS=https://your-frontend-domain,API_BEARER_TOKEN=$API_BEARER_TOKEN,ENABLE_SCREEN_SHARE=true,ENABLE_IMAGE_GENERATION=true,DEMO_MODE_DEFAULT=false \
  --set-secrets=GOOGLE_API_KEY=pitchmirror-gemini-api-key:latest \
  --allow-unauthenticated \
  --project=$PROJECT_ID
```

### Option B: Terraform (IaC — recommended)

```bash
# ADC login for Terraform/provider auth
gcloud auth application-default login

# Optional explicit path (use $HOME, not "~")
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/application_default_credentials.json"

# Avoid leaking values into shell history by using TF_VAR_*
export TF_VAR_project_id=$PROJECT_ID
export TF_VAR_container_image=$REGION-docker.pkg.dev/$PROJECT_ID/pitchmirror/app:latest
export TF_VAR_gemini_secret_id=pitchmirror-gemini-api-key
export TF_VAR_firestore_collection=pitchmirror_sessions
export TF_VAR_allowed_origins=https://your-frontend-domain
export TF_VAR_api_bearer_token=$(openssl rand -hex 24)
export TF_VAR_allow_unauthenticated=false
export TF_VAR_enable_screen_share=true
export TF_VAR_enable_image_generation=true
export TF_VAR_demo_mode_default=false
export TF_VAR_image_generation_timeout_s=24
export TF_VAR_image_generation_retries=1
export TF_VAR_image_model=imagen-4.0-fast-generate-001
# For public demo links, set true intentionally and keep API_BEARER_TOKEN enabled.

cd infra
terraform init
terraform apply

# Firestore composite index creation can take a few minutes on first deploy.
# Check output: firestore_user_history_index
```

---

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOOGLE_API_KEY` | Yes | — | Gemini Developer API key |
| `GOOGLE_CLOUD_PROJECT` | For Firestore | — | GCP project ID |
| `FIRESTORE_COLLECTION` | No | `pitchmirror_sessions` | Firestore collection name |
| `GEMINI_BACKEND` | No | `gemini` | `gemini` or `vertex` |
| `ENVIRONMENT` | No | `development` | `development` or `production` |
| `CORS_ALLOWED_ORIGINS` | No | `*` | Comma-separated allowed origins (`*` for local dev only) |
| `API_BEARER_TOKEN` | No | empty | Optional shared token required for `/api/*` and `/ws` when set |
| `HTTP_MAX_REQUESTS_PER_MINUTE` | No | `120` | Per-IP HTTP request cap |
| `WS_MAX_CONNECTIONS_PER_MINUTE` | No | `20` | Per-IP WebSocket connection attempts per minute |
| `WS_MAX_CONCURRENT_PER_IP` | No | `2` | Max active live sessions per IP |
| `WS_MAX_MESSAGES_PER_SECOND` | No | `30` | Per-session WS frame rate cap |
| `WS_MAX_BYTES_PER_SECOND` | No | `1000000` | Per-session WS throughput cap (bytes/s) |
| `WS_MAX_BINARY_FRAME_BYTES` | No | `600000` | Max single binary WS frame size |
| `WS_MAX_TEXT_FRAME_BYTES` | No | `4096` | Max single text WS frame size |
| `RECENT_SESSION_CACHE_SIZE` | No | `200` | In-memory fallback cache size for recent sessions |
| `SLIDE_MAX_UPLOAD_BYTES` | No | `20971520` | Max PDF upload size in bytes |
| `SLIDE_MAX_PAGES` | No | `20` | Max pages converted per uploaded PDF |
| `SLIDE_MAX_WIDTH` | No | `1024` | Max rendered slide width in pixels |
| `SLIDE_STORE_MAX_USERS` | No | `64` | Max in-memory user decks retained before eviction |
| `SLIDE_STORE_TTL_S` | No | `21600` | Slide-deck TTL in seconds (default 6 hours) |
| `ENABLE_SCREEN_SHARE` | No | `true` | Enable screen-share frame ingestion (UI toggle still controls per session) |
| `ENABLE_IMAGE_GENERATION` | No | `true` | Enable post-session multimodal image generation |
| `DEMO_MODE_DEFAULT` | No | `false` | Make deterministic demo behavior default unless overridden in UI |
| `PITCHMIRROR_IMAGE_MODEL` | No | `imagen-4.0-fast-generate-001` | Image model for scorecard visuals |
| `IMAGE_GENERATION_TIMEOUT_S` | No | `24` | Timeout per generated image |
| `IMAGE_GENERATION_RETRIES` | No | `1` | Retry count per generated image |

---

## REST API endpoints

PitchMirror is WebSocket-first for live coaching, and also exposes read-only REST endpoints for history/stats:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/slides/upload` | POST | Upload PDF deck and return metadata + first slide eagerly (lazy loading for remaining slides) |
| `/api/slides/{deck_id}/{slide_index}` | GET | Fetch one slide image on demand for lazy/background loading |
| `/api/sessions?limit=20` | GET | Recent sessions (summary) |
| `/api/sessions/{session_id}` | GET | Full stored scorecard/report for one session |
| `/api/sessions/stats?limit=20` | GET | Aggregate stats across recent sessions |

If `API_BEARER_TOKEN` is set:
- Send `Authorization: Bearer <token>` (or `x-api-key: <token>`) for `/api/*`.
- For WebSocket `/ws`, pass `?token=<token>` (or the same auth headers).
- Send a stable `x-user-id` (or `?user=` on WebSocket) to scope session history to one presenter.
- The browser client reads `window.localStorage['pitchmirror_api_token']` for both REST and WebSocket auth.

Notes:
- Slide uploads are optimized for responsiveness: conversion runs off the event loop and only the first slide is returned immediately.
- Remaining slides are fetched lazily via `/api/slides/{deck_id}/{slide_index}`.
- Slide decks are kept in per-instance memory. `infra/main.tf` already sets `session_affinity = true` and `min_instance_count = 1` to ensure upload and WebSocket session reach the same instance. If you deploy via `gcloud run deploy` instead of Terraform, pass `--session-affinity --min-instances 1`.

---

## Demo script (4 minutes)

Recommended demo setup:
- Mode: `presentation`
- Screen-aware coaching: `ON`
- Demo mode: `ON`

Use this scripted segment with planted issues:

```
"So basically um, today I want to share how teams can improve customer onboarding..."
[LOOK DOWN 6+ seconds at notes]
"Our process is kind of sort of basically simple and literally works for every company."
[SPEAK VERY FAST for 10+ seconds]
[SHOW A TEXT-DENSE SLIDE WITH TINY FONT]
```

Expected real-time corrections:
1. Filler words (`filler_count_30s >= 3`)
2. Pace (`wpm_20s > 180`)
3. Eye contact drop (`>=5s`)
4. Slide clarity/speech mismatch when screen share is active

Target video flow (3:30–3:50):
1. 0:00–0:20: hook + mode/toggles
2. 0:20–1:40: live interruptions (audio + transcript + tool-call evidence)
3. 1:40–2:20: screen-aware slide correction
4. 2:20–3:10: post-session pipeline + generated visual cards
5. 3:10–3:50: final scorecard + Cloud Run proof + architecture diagram

Rehearsal checklist: [scripts/rehearsal_checklist.md](scripts/rehearsal_checklist.md)

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| AI model | `gemini-2.5-flash-native-audio-preview-12-2025` |
| AI SDK | Google ADK + Google GenAI Python SDK |
| Backend | FastAPI + Uvicorn (Python 3.12) |
| Frontend | Vanilla HTML/CSS/JS |
| Audio (browser) | Web Audio API + AudioWorklet (16-bit PCM 16kHz) |
| Audio (coach) | Web Audio API (24kHz PCM playback) |
| Video (browser) | Canvas 2D → webcam JPEG @ 1fps + optional screen JPEG @ 0.5fps |
| Database | Google Cloud Firestore |
| Secrets | Google Secret Manager |
| Hosting | Google Cloud Run |
| IaC | Terraform |

---

## Session management

The Live run config enables:
- `context_window_compression` with `SlidingWindow`
- `session_resumption`

In practice:
- Normal sessions rely on the sliding window for continuity.
- If a transport failure occurs, the browser currently starts a fresh live session (best-effort reconnect behavior can be layered on top of the existing config).
- Previous-session coaching context is resumed per presenter using user-scoped history from Firestore.

---

## Submission narrative

**Core thesis:** PitchMirror is a **measurement-first speaking intelligence system**.  
It does not rely on vague LLM impressions; it uses explicit tools, rolling windows, and hard server-side gates to intervene only when evidence is strong.

Detailed submission steps: [docs/submission_runbook.md](docs/submission_runbook.md)
Bonus link prep template: [docs/bonus_assets_template.md](docs/bonus_assets_template.md)

This positions the project across rubric dimensions:
- Innovation & UX: real-time multimodal coaching + screen-aware corrections + generated visual guidance
- Technical execution: ADK live loop, custom binary protocol, objective tool pipeline, post-session parallel agents
- Demo quality: visible interruptions, tool-call evidence, and concrete before/after assets

---

## Submission checklist (judge-facing)

1. Project description with problem, solution, and measurable outcomes
2. Public repository URL + spin-up instructions (this README)
3. Cloud deployment proof clip (30–45s, separate from demo)
4. Architecture diagram:
   - [docs/architecture.png](docs/architecture.png)
   - [docs/architecture_detailed.png](docs/architecture_detailed.png)
5. Demo video `< 4 minutes` showing live software (no mockups)
6. Bonus evidence:
   - Terraform IaC included in [`infra/`](infra/)
   - Public build post with `#GeminiLiveAgentChallenge`
   - Public GDG profile link in submission form

### Google Cloud deployment proof URLs

Use this as your primary proof URL (Option 2: code proof):

- https://github.com/SmartGridsML/GeminiLiveAgentHack/blob/main/infra/main.tf

This file explicitly shows:
- Cloud Run service deployment
- Firestore database and composite index
- Secret Manager integration for `GOOGLE_API_KEY`

Supporting proof links (if multiple links are allowed):

- https://github.com/SmartGridsML/GeminiLiveAgentHack/blob/main/infra/outputs.tf
- https://github.com/SmartGridsML/GeminiLiveAgentHack#cloud-run-deployment

Live deployed backend URL (include in project description):

- `https://pitchmirror-101569338664.us-central1.run.app`

---

## License

MIT
