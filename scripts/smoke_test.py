"""
PitchMirror — Pre-build smoke test.

Run this BEFORE investing hours in the build.
Gate criteria:
  - Live API round-trip p95 < 3s  → proceed with PitchMirror
  - Live API p95 >= 3s            → reassess or use fallback (ScreenPilot)

Usage:
  export GOOGLE_API_KEY=your_key
  python scripts/smoke_test.py
"""
import asyncio
import os
import sys
import time

try:
    from google import genai
    from google.genai import types
except ImportError:
    print("ERROR: google-genai not installed. Run: pip install google-genai")
    sys.exit(1)


LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
N_RUNS = 5


async def single_round_trip(client: genai.Client) -> float:
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()
        ),
    )
    start = time.time()
    async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
        await session.send_realtime_input(text="Say exactly: ready")
        async for response in session.receive():
            if response.server_content and response.server_content.model_turn:
                return time.time() - start
            if response.server_content and response.server_content.turn_complete:
                return time.time() - start
    return time.time() - start


async def run_smoke_test():
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY environment variable not set")
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    print(f"PitchMirror Smoke Test — model: {LIVE_MODEL}")
    print(f"Running {N_RUNS} round-trip tests...\n")

    latencies = []
    errors = []

    for i in range(N_RUNS):
        try:
            t = await single_round_trip(client)
            latencies.append(t)
            status = "✓" if t < 3.0 else "⚠" if t < 5.0 else "✗"
            print(f"  Run {i+1}: {t:.2f}s {status}")
        except Exception as e:
            errors.append(str(e))
            print(f"  Run {i+1}: ERROR — {e}")

    if not latencies:
        print("\n✗ ALL RUNS FAILED — check API key and model availability")
        sys.exit(1)

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)] if len(latencies) >= 20 else max(latencies)

    print(f"\n{'─'*40}")
    print(f"  p50:    {p50:.2f}s")
    print(f"  p95:    {p95:.2f}s")
    print(f"  errors: {len(errors)}/{N_RUNS}")
    print(f"{'─'*40}")

    if p95 < 1.5:
        print("\n✓ EXCELLENT — build PitchMirror, demo will be snappy")
    elif p95 < 3.0:
        print("\n✓ PASS — proceed with PitchMirror (may add loading indicator for demo)")
    elif p95 < 5.0:
        print("\n⚠ MARGINAL — consider ScreenPilot as primary if you want reliability")
    else:
        print("\n✗ FAIL — Live API too slow for live demo. Switch to ScreenPilot.")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
