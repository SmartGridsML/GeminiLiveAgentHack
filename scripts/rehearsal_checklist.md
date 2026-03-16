# PitchMirror Rehearsal Checklist

Use this to run 3 deterministic rehearsals before recording the final demo.

## Preflight (run once)

```bash
python -m compileall backend
node --check frontend/app.js
```

## Session config for rehearsals

- Coach mode: `presentation`
- Screen-aware coaching: `ON`
- Demo mode: `ON`
- Stable lighting + headset mic

## Rehearsal run #1

1. Verify session starts and timer runs.
2. Trigger filler interruption.
3. Trigger pace interruption.
4. Trigger eye-contact interruption.
5. Trigger slide clarity interruption via dense slide.
6. Verify scorecard shows generated visual cards.

## Rehearsal run #2

1. Repeat run #1.
2. Confirm no websocket disconnects.
3. Confirm no backend stack traces.

## Rehearsal run #3

1. Record full flow with final script.
2. Confirm runtime < 4 minutes.
3. Confirm architecture image and Cloud Run proof are shown.

## Pass criteria

- No crashes or WS disconnects across 3 runs.
- At least 3 real-time interruptions in final take.
- At least 1 screen-aware correction in final take.
- Generated visual section appears in scorecard.
