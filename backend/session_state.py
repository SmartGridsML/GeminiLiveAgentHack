"""
PitchMirror — per-session state container.

Shared between the ADK tool closures, the WebSocket handler, and the scorecard builder.
Tool functions write into this object synchronously (put_nowait); the WebSocket sender
drains ws_event_queue asynchronously via a sentinel-driven loop.
"""
import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Rolling transcript cap — prevents unbounded context growth in long sessions.
# At ~1 line/5s avg, 80 lines covers ~6 minutes with full fidelity.
MAX_TRANSCRIPT_SEGMENTS = 80
MAX_TIMELINE_EVENTS = 500
_PROSODY_WINDOW_S = 20.0

# Sentinel value: putting this into ws_event_queue signals the drain task to exit.
_DRAIN_STOP = None


@dataclass
class CoachingEvent:
    timestamp: float
    event_type: str   # "filler" | "pace" | "eye_contact" | "contradiction" | "clarity" | slide_*
    description: str
    evidence: dict = field(default_factory=dict)  # metric snapshot: threshold + measured value


@dataclass
class SessionState:
    session_id: str
    user_id: str = "anon"
    coach_mode: str = "general"
    delivery_context: str = "virtual"
    primary_goal: str = "balanced"
    persona: str = "coach"
    screen_enabled: bool = False
    demo_mode: bool = False
    previous_summary: str = ""
    total_slides: int = 0
    current_slide_index: int = -1
    session_start: float = field(default_factory=time.time)

    # Structured coaching events (populated by flag_issue tool calls)
    events: list[CoachingEvent] = field(default_factory=list)
    # Replay-friendly chronological timeline (interruptions, tool actions, slide moves, telemetry).
    timeline_events: list[dict] = field(default_factory=list)

    # Rolling transcript — capped at MAX_TRANSCRIPT_SEGMENTS to bound context size
    transcript: list[dict] = field(default_factory=list)

    # Async queue: tool functions put metric updates here; WebSocket sender drains it.
    # Bounded at 256 to cap memory under backpressure; events dropped when full are
    # non-critical metric/tool-call UI updates — the session continues unaffected.
    # Sentinel None signals the drain task to stop cleanly.
    ws_event_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))

    # Diagnostics — count of events dropped due to queue backpressure
    queue_drop_count: int = 0

    # Metric counters (derived from events for fast access)
    filler_count: int = 0
    eye_contact_drops: int = 0
    pace_violations: int = 0
    contradictions: int = 0
    clarity_flags: int = 0
    visual_flags: int = 0
    mismatch_flags: int = 0
    live_visual_hints_ready: int = 0

    # Set False when the live session ends
    is_active: bool = True

    # monotonic timestamp of the last coach audio chunk forwarded to the browser.
    # Used by _downstream to enforce a hard speech cooldown server-side.
    last_coach_speech_time: float = 0.0
    # Monotonic timestamp + issue type of the last *confirmed* interruption.
    # _downstream suppresses spontaneous model speech unless this was set recently.
    last_confirmed_issue_time: float = 0.0
    last_confirmed_issue_type: str = ""

    # Last slide/screen frame received as base64 JPEG — used as "before" in
    # generate_live_visual_hint before/after slide redesign comparison.
    last_slide_frame_b64: str = ""

    # Written analysis from post-session pipeline
    final_report: str = ""
    research_tips: str = ""
    generated_assets: list[dict] = field(default_factory=list)
    # Holistic scores (0–100) from synthesis agent; override metric-based scoring when present
    ai_scores: dict = field(default_factory=dict)
    # Rolling speech-signal timestamps for two-signal interruption gating.
    signal_hits: dict[str, float] = field(default_factory=dict)
    _signal_emit_ts: dict[str, float] = field(default_factory=dict)
    # Rolling audio features from the raw PCM path for prosody intelligence.
    prosody_window: deque = field(default_factory=lambda: deque(maxlen=320))
    prosody_metrics: dict = field(default_factory=lambda: {
        "pitch_variance_hz": 0.0,
        "pause_ratio_20s": 0.0,
        "speaking_energy": 0.0,
        "monotony_score": 0.0,
        "voiced_seconds_20s": 0.0,
    })

    def _enqueue(self, event: dict) -> None:
        """Non-blocking enqueue; silently drops when the queue is full.

        The drained events are non-critical UI updates (metrics, tool-call badges).
        Dropping them never affects session logic or scoring.
        """
        try:
            self.ws_event_queue.put_nowait(event)
        except asyncio.QueueFull:
            self.queue_drop_count += 1
            logger.debug(
                "ws_event_queue full — dropping %s event (session=%s, drops=%d)",
                event.get("type"), self.session_id, self.queue_drop_count,
            )

        # Keep a lightweight replay timeline for post-session scrubbing.
        etype = str(event.get("type") or "")
        if etype == "tool_call":
            tool = str(event.get("tool") or "tool")
            self.record_timeline("tool_call", tool, {"tool": tool}, enqueue=False)
        elif etype == "slide_change":
            idx = event.get("current_slide_index")
            self.record_timeline(
                "slide_change",
                f"Slide {int(idx) + 1}" if isinstance(idx, int) else "Slide change",
                {"index": idx, "source": event.get("source")},
                enqueue=False,
            )
        elif etype == "telemetry":
            phase = str(event.get("phase") or "telemetry")
            detail = str(event.get("detail") or "")
            self.record_timeline("telemetry", f"{phase}: {detail}".strip(": "), {"phase": phase}, enqueue=False)
        elif etype == "demo_seed":
            self.record_timeline("demo_seed", str(event.get("message") or "Demo checkpoint"), enqueue=False)

    def record_event(self, event_type: str, description: str, evidence: dict | None = None) -> None:
        """Called synchronously from ADK tool functions."""
        ts = round(time.time() - self.session_start, 1)
        self.events.append(CoachingEvent(
            timestamp=ts,
            event_type=event_type,
            description=description,
            evidence=evidence or {},
        ))

        if event_type == "filler":
            self.filler_count += 1
        elif event_type == "eye_contact":
            self.eye_contact_drops += 1
        elif event_type == "pace":
            self.pace_violations += 1
        elif event_type == "contradiction":
            self.contradictions += 1
        elif event_type == "clarity":
            self.clarity_flags += 1
        elif event_type == "slide_clarity":
            self.visual_flags += 1
        elif event_type == "slide_mismatch":
            self.mismatch_flags += 1

        self.record_timeline(
            "interruption",
            description,
            {"issue_type": event_type, "evidence": evidence or {}},
            enqueue=False,
        )

        self._enqueue({
            "type": "metric",
            "key": event_type,
            "value": self._count(event_type),
        })

    def record_timeline(
        self,
        event_type: str,
        label: str,
        data: dict | None = None,
        *,
        enqueue: bool = False,
    ) -> None:
        row = {
            "ts": round(time.time() - self.session_start, 1),
            "event_type": event_type,
            "label": (label or "")[:220],
            "data": data or {},
        }
        self.timeline_events.append(row)
        if len(self.timeline_events) > MAX_TIMELINE_EVENTS:
            self.timeline_events = self.timeline_events[-MAX_TIMELINE_EVENTS:]
        if enqueue:
            self._enqueue({"type": "timeline", "event": row})

    def record_signal(
        self,
        signal: str,
        *,
        measured: float | int | None = None,
        threshold: str | None = None,
        source: str = "",
    ) -> None:
        now = time.monotonic()
        self.signal_hits[signal] = now
        # Emit signal spikes at most once every 8 seconds per signal to avoid UI spam.
        if now - self._signal_emit_ts.get(signal, 0.0) >= 8.0:
            self._signal_emit_ts[signal] = now
            detail_parts = [signal]
            if measured is not None:
                detail_parts.append(f"measured={measured}")
            if threshold:
                detail_parts.append(f"threshold={threshold}")
            if source:
                detail_parts.append(f"source={source}")
            self.record_timeline("signal_spike", " | ".join(detail_parts), {"signal": signal}, enqueue=False)

    def has_recent_signals(self, signal_names: list[str], *, window_s: float) -> list[str]:
        now = time.monotonic()
        hits: list[str] = []
        for sig in signal_names:
            ts = self.signal_hits.get(sig, 0.0)
            if ts and (now - ts) <= window_s:
                hits.append(sig)
        return hits

    def register_confirmed_issue(self, issue_type: str) -> None:
        self.last_confirmed_issue_time = time.monotonic()
        self.last_confirmed_issue_type = issue_type

    def update_prosody(
        self,
        *,
        rms: float,
        pitch_hz: float | None,
        speech_active: bool,
        chunk_duration_s: float = 0.1,
    ) -> dict:
        now_rel = self.duration_seconds()
        self.prosody_window.append({
            "ts": now_rel,
            "rms": max(0.0, float(rms)),
            "pitch": float(pitch_hz) if pitch_hz else None,
            "speech": bool(speech_active),
            "dur": max(0.02, float(chunk_duration_s)),
        })
        cutoff = now_rel - _PROSODY_WINDOW_S
        while self.prosody_window and self.prosody_window[0]["ts"] < cutoff:
            self.prosody_window.popleft()

        rows = list(self.prosody_window)
        total_dur = sum(r["dur"] for r in rows) or 1e-6
        voiced_rows = [r for r in rows if r["speech"]]
        voiced_dur = sum(r["dur"] for r in voiced_rows)
        pause_ratio = max(0.0, min(1.0, (total_dur - voiced_dur) / total_dur))

        voiced_energy = [r["rms"] for r in voiced_rows]
        mean_energy = (sum(voiced_energy) / len(voiced_energy)) if voiced_energy else 0.0

        pitch_vals = [r["pitch"] for r in voiced_rows if r.get("pitch")]
        if len(pitch_vals) >= 3:
            pitch_mean = sum(pitch_vals) / len(pitch_vals)
            pitch_var = sum((p - pitch_mean) ** 2 for p in pitch_vals) / len(pitch_vals)
            pitch_std = pitch_var ** 0.5
        else:
            pitch_std = 0.0

        if voiced_dur < 5.0:
            monotony = 0.0
        else:
            # 0 (expressive) -> 100 (highly monotone)
            monotony = max(0.0, min(100.0, (1.0 - min(pitch_std / 35.0, 1.0)) * 100.0))

        self.prosody_metrics = {
            "pitch_variance_hz": round(pitch_std, 1),
            "pause_ratio_20s": round(pause_ratio, 3),
            "speaking_energy": round(mean_energy, 4),
            "monotony_score": round(monotony, 1),
            "voiced_seconds_20s": round(voiced_dur, 1),
        }
        return self.prosody_metrics

    def add_transcript(self, speaker: str, text: str) -> None:
        self.transcript.append({
            "s": speaker[0],  # "u" or "c" — compact keys reduce context size
            "t": text,
            "ts": round(time.time() - self.session_start, 1),
        })
        # Trim oldest entries when over the rolling cap
        if len(self.transcript) > MAX_TRANSCRIPT_SEGMENTS:
            self.transcript = self.transcript[-MAX_TRANSCRIPT_SEGMENTS:]

    def stop_drain(self) -> None:
        """Put the sentinel into the queue so the drain task exits cleanly.

        Clears one slot if the queue is full so the sentinel is guaranteed delivery.
        """
        if self.ws_event_queue.full():
            try:
                self.ws_event_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.ws_event_queue.put_nowait(_DRAIN_STOP)

    def transcript_text(self) -> str:
        if not self.transcript:
            return "(no transcript captured)"
        label = {"u": "Presenter", "c": "Coach"}
        lines = [
            f"[{seg['ts']}s] {label.get(seg['s'], seg['s'])}: {seg['t']}"
            for seg in self.transcript
        ]
        return "\n".join(lines)

    def events_json(self) -> str:
        # Compact JSON — no indent, abbreviated keys to reduce token count.
        # "ev" included only when evidence is populated so tokens aren't wasted.
        rows = []
        for e in self.events:
            row: dict = {"type": e.event_type, "ts": round(e.timestamp), "desc": e.description}
            if e.evidence:
                row["ev"] = e.evidence
            rows.append(row)
        return json.dumps(rows)

    def timeline_json(self) -> str:
        return json.dumps(self.timeline_events)

    def duration_seconds(self) -> float:
        return time.time() - self.session_start

    def _count(self, event_type: str) -> int:
        return {
            "filler": self.filler_count,
            "eye_contact": self.eye_contact_drops,
            "pace": self.pace_violations,
            "contradiction": self.contradictions,
            "clarity": self.clarity_flags,
            "slide_clarity": self.visual_flags,
            "slide_mismatch": self.mismatch_flags,
        }.get(event_type, 0)
