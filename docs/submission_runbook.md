# PitchMirror Submission Runbook

## Core positioning

PitchMirror is a measurement-first multimodal speaking coach for talks, interviews, demos, and pitches.

## Required artifacts

1. Public repository with spin-up instructions.
2. Architecture diagram image.
3. Demo video (`<4m`) showing live product behavior.
4. Separate Cloud deployment proof clip (`30–45s`).

## Preflight before recording (60s)

Run this immediately before capture:

```bash
python -m compileall backend
node --check frontend/app.js
pytest -q
```

All commands should pass with no errors.

## Final demo script (target: 3:45)

### `0:00–0:15` Hook

Show app screen.

Say:

> PitchMirror is a real-time multimodal speaking coach. It listens to voice, sees webcam and slides, and interrupts only when confidence is high.

### `0:15–0:35` Intake setup

Set:

- Mode: `presentation`
- Context: `virtual`
- Goal: `improve_pacing`
- Persona: `Professional Coach`
- `Screen-aware coaching`: ON
- `Deterministic demo mode`: ON

Say:

> This run is goal-conditioned, so scoring and coaching are weighted to my objective.

### `0:35–0:50` Slide prep

Upload a dense PDF slide deck and start screen share.

Say:

> I’ve uploaded real slides so the coach can critique visual clarity in-session.

### `0:50–1:40` Force live triggers

Start session and speak the planted script with fillers and high speed:

> Um basically today I’m kind of presenting our system and like it literally works for everyone, and um basically we can sort of accelerate every workflow quickly.

Then continue very fast for 10–12 seconds:

> We capture audio video and screen context, compute delivery signals, and return live interventions without manual review across talks interviews and demos.

Then look away from camera for ~6 seconds.

Expected visible signals:

- Tool calls
- Real-time interruption(s)
- Coach audio
- Telemetry updates

### `1:40–2:20` Slide-aware + visual hint

Say:

> Coach, next slide.  
> Critique this slide.

Expected:

- Slide clarity or mismatch flag
- Live visual hint generation
- Before/after redesigned visual appears with rationale

Say:

> Now it’s doing UI navigation plus multimodal generation in the same live loop.

### `2:20–2:35` End live phase

Click `End Session`.

Say:

> Now the post-session multi-agent pipeline runs.

### `2:35–3:05` Pipeline proof

Show pipeline step progression:

- Delivery
- Content
- Research
- Synthesis
- Memory update

Say:

> We run parallel analysts, synthesize a final report, then update cross-session memory.

### `3:05–3:30` Scorecard + replay timeline

Show:

- Overall score
- Category cards (including prosody)
- Evidence chips
- Replay timeline scrubber with markers

Say:

> This score is evidence-backed, goal-conditioned, and replayable with event markers.

### `3:30–3:45` Architecture + Cloud proof

Show architecture diagram and deployed URL.

Say:

> Deployed on Google Cloud Run with Firestore persistence and Terraform IaC.

## Fallback line if image generation is slow

Use this exact line if live visual hint takes longer:

> Image generation is asynchronous; while it completes, live coaching and scoring continue uninterrupted.

## Cloud proof clip (30–45s)

1. Show Cloud Run service page and revision.
2. Show logs receiving websocket traffic.
3. Show Firestore documents for completed sessions.

## Bonus points

1. Publish build content and include `#GeminiLiveAgentChallenge`.
2. Highlight Terraform IaC in `infra/`.
3. Include a public GDG profile link in final submission.
