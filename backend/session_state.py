"""
PitchMirror — per-session state container.

Shared between the ADK tool closures, the WebSocket handler, and the scorecard builder.
Tool functions write into this object synchronously (put_nowait); the WebSocket sender
drains ws_event_queue asynchronously via a sentinel-driven loop.
"""
import asyncio
import json
import time
from dataclasses import dataclass, field

# Rolling transcript cap — prevents unbounded context growth in long sessions.
# At ~1 line/5s avg, 80 lines covers ~6 minutes with full fidelity.
MAX_TRANSCRIPT_SEGMENTS = 80

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

    # Rolling transcript — capped at MAX_TRANSCRIPT_SEGMENTS to bound context size
    transcript: list[dict] = field(default_factory=list)

    # Async queue: tool functions put metric updates here; WebSocket sender drains it.
    # Bounded at 256 to cap memory under backpressure; events dropped when full are
    # non-critical metric/tool-call UI updates — the session continues unaffected.
    # Sentinel None signals the drain task to stop cleanly.
    ws_event_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))

    # Metric counters (derived from events for fast access)
    filler_count: int = 0
    eye_contact_drops: int = 0
    pace_violations: int = 0
    contradictions: int = 0
    clarity_flags: int = 0
    visual_flags: int = 0
    mismatch_flags: int = 0

    # Set False when the live session ends
    is_active: bool = True

    # monotonic timestamp of the last coach audio chunk forwarded to the browser.
    # Used by _downstream to enforce a hard speech cooldown server-side.
    last_coach_speech_time: float = 0.0

    # Written analysis from post-session pipeline
    final_report: str = ""
    research_tips: str = ""
    generated_assets: list[dict] = field(default_factory=list)
    # Holistic scores (0–100) from synthesis agent; override metric-based scoring when present
    ai_scores: dict = field(default_factory=dict)

    def _enqueue(self, event: dict) -> None:
        """Non-blocking enqueue; silently drops when the queue is full.

        The drained events are non-critical UI updates (metrics, tool-call badges).
        Dropping them never affects session logic or scoring.
        """
        try:
            self.ws_event_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

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

        self._enqueue({
            "type": "metric",
            "key": event_type,
            "value": self._count(event_type),
        })

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
