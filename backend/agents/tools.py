"""
PitchMirror — ADK tool definitions.

Tools are created as closures over SessionState so they can:
  1. Update structured metrics (synchronously)
  2. Push real-time events to the browser via ws_event_queue (non-blocking)

ADK calls these functions automatically when Gemini emits a function_call.
No manual tool dispatch loop needed.

Tool inventory:
  flag_issue(issue_type, description)
      Record a detected problem. Hard-gated by global + per-type cooldowns.
      Attaches machine-readable evidence snapshot (metric, threshold, measured value).

  get_speech_metrics()
      Return objective WPM, filler word counts, and duration computed from
      the live transcript.  Use this to confirm pace/filler thresholds before
      flagging — grounds feedback in real numbers rather than impressions.

  get_recent_transcript(n_words)
      Return the last N words the presenter spoke.  Use this when you need
      to re-read a passage before deciding whether to flag a contradiction
      or clarity issue.

  check_slide_clarity(signal, evidence)
      Debounced validation for screen-share issues before raising a slide
      clarity/mismatch interruption.

  navigate_practice_slides(action)
      Execute a real UI action in the frontend slide viewer (uploaded deck or practice deck).

  jump_to_slide(index)
      Jump directly to a specific slide by 0-based index.

  mark_slide_issue(issue_type, label)
      Silently annotate the current slide with a visual issue marker (no cooldown).

  generate_live_visual_hint(hint_type, context)
      Fire-and-forget Imagen call: returns immediately, image delivered via ws_event_queue.
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.session_state import SessionState

# Cooldown constants — enforced server-side regardless of model behaviour.
# The model's system prompt instructs the same limits, but this is the hard gate.
_GLOBAL_COOLDOWN_S = 30.0    # minimum seconds between any two interruptions
_PER_TYPE_COOLDOWN_S = 90.0  # minimum seconds before repeating the same issue type

_VALID_ISSUE_TYPES = frozenset({
    "filler",
    "pace",
    "eye_contact",
    "contradiction",
    "clarity",
    "slide_clarity",
    "slide_mismatch",
})

# Filler word regex — covers common spoken fillers.
_FILLER_RE = re.compile(
    r"\b(um+|uh+|like|basically|you know|so|actually|literally|right|kind of|sort of)\b",
    re.IGNORECASE,
)


def make_coaching_tools(state: "SessionState") -> list:
    """
    Return a list of ADK-compatible tool functions bound to this session's state.
    Pass the returned list directly to LlmAgent(tools=...).
    """
    _last_any: list[float] = [0.0]           # mutable cell so closure can write
    _last_by_type: dict[str, float] = {}
    _last_slide_signal: dict[str, float] = {}
    # Late-binding ref: set after generate_live_visual_hint is defined so
    # flag_issue can auto-trigger it on slide issues without relying on the model.
    _visual_hint_fn: list = [None]
    # Grounding gate: tracks last time a measurement tool was called.
    # contradiction and clarity require recent grounding before flag_issue accepts them.
    _last_grounding_time: list[float] = [0.0]
    _GROUNDING_WINDOW_S = 90.0   # must have called a measurement tool within this window
    _GROUNDING_REQUIRED_TYPES = frozenset({"contradiction", "clarity"})
    _COMPOSITE_SIGNAL_WINDOW_S = 35.0
    _SLIDE_SIGNAL_WINDOW_S = 45.0
    _valid_slide_signals = frozenset({
        "clutter",
        "unreadable_text",
        "weak_hierarchy",
        "speech_mismatch",
    })
    global_cooldown_s = 5.0 if state.demo_mode else _GLOBAL_COOLDOWN_S
    per_type_cooldown_s = 20.0 if state.demo_mode else _PER_TYPE_COOLDOWN_S

    def _emit_telemetry(phase: str, detail: str, data: dict | None = None) -> None:
        state._enqueue({
            "type": "telemetry",
            "phase": phase,
            "detail": detail,
            "data": data or {},
        })

    def _confidence_gate(issue_type: str) -> dict:
        """
        Require composite evidence before interrupting.
        This keeps interruptions precise and prevents "nagging AI" behavior.
        """
        if issue_type in {"pace", "filler"}:
            candidates = ["pace_high", "filler_burst", "pause_low", "monotony_high"]
            hits = state.has_recent_signals(candidates, window_s=_COMPOSITE_SIGNAL_WINDOW_S)
            return {
                "ok": len(hits) >= 2,
                "window_s": _COMPOSITE_SIGNAL_WINDOW_S,
                "required": candidates,
                "hits": hits,
            }

        if issue_type in {"slide_clarity", "slide_mismatch"}:
            all_hits = state.has_recent_signals(
                [
                    "slide_clutter",
                    "slide_unreadable_text",
                    "slide_weak_hierarchy",
                    "slide_speech_mismatch",
                ],
                window_s=_SLIDE_SIGNAL_WINDOW_S,
            )
            structural = [h for h in all_hits if h != "slide_speech_mismatch"]
            if issue_type == "slide_mismatch":
                ok = ("slide_speech_mismatch" in all_hits) and len(all_hits) >= 2
            else:
                ok = bool(structural) and len(all_hits) >= 2
            return {
                "ok": ok,
                "window_s": _SLIDE_SIGNAL_WINDOW_S,
                "required": ["any 2 of slide_{clutter|unreadable_text|weak_hierarchy|speech_mismatch}"],
                "hits": all_hits,
            }

        if issue_type == "eye_contact":
            hits = state.has_recent_signals(["eye_contact_confirmed"], window_s=18.0)
            return {
                "ok": bool(hits),
                "window_s": 18.0,
                "required": ["eye_contact_confirmed"],
                "hits": hits,
            }

        return {"ok": True, "window_s": 0.0, "required": [], "hits": []}

    def _compute_measured(issue_type: str) -> dict:
        """
        Compute the actual measured metric value at the moment flag_issue fires.
        This ensures evidence reflects what triggered the call, not a static map.
        """
        now_rel = state.duration_seconds()
        all_segs = [s for s in state.transcript if s.get("s") == "u"]

        if issue_type == "filler":
            cutoff = now_rel - 30.0
            recent_text = " ".join(s["t"] for s in all_segs if s.get("ts", 0) >= cutoff)
            count_30s = sum(1 for _ in _FILLER_RE.finditer(recent_text))
            return {
                "metric": "filler_count_30s",
                "threshold": "≥3",
                "measured": count_30s,
                "session_total": state.filler_count + 1,
            }

        if issue_type == "pace":
            cutoff = now_rel - 20.0
            recent = [s for s in all_segs if s.get("ts", 0) >= cutoff]
            words = sum(len(s["t"].split()) for s in recent)
            elapsed = min(now_rel, 20.0)
            wpm = round(words / elapsed * 60, 1) if elapsed > 2 else 0.0
            return {"metric": "wpm_20s", "threshold": ">180", "measured": wpm}

        if issue_type == "eye_contact":
            threshold_s = {"virtual": 5.0, "hybrid": 6.5, "in_person": 8.0}.get(
                state.delivery_context, 5.0
            )
            secs = (
                round(time.monotonic() - _gaze_away_since[0], 1)
                if _gaze_currently_away[0] else 0.0
            )
            return {
                "metric": "seconds_away",
                "threshold_s": threshold_s,
                "measured": secs,
                "context": state.delivery_context,
            }

        # Qualitative issues — no hard numeric threshold
        return {"metric": "qualitative", "threshold": "n/a"}

    def flag_issue(issue_type: str, description: str) -> dict:
        """
        Record a specific presentation delivery problem you have just detected.
        Call this BEFORE speaking your coaching feedback so the issue is tracked.

        IMPORTANT: If this returns {"recorded": false}, you MUST stay completely
        silent — do NOT speak any feedback for this issue.

        Args:
            issue_type: Exactly one of:
                "filler"        - filler words (um, uh, like, basically, you know)
                "pace"          - speaking too fast (>180 WPM sustained)
                "eye_contact"   - looking away from camera for >5 seconds
                "contradiction" - statement contradicts something said earlier
                "clarity"       - sentence is incomprehensible or meaningless
                "slide_clarity" - slide is cluttered or visually unreadable
                "slide_mismatch"- spoken narrative does not match current slide

            description: One specific sentence describing what was observed.
                Good: "Said 'um' 4 times in 15 seconds during the market size section."
                Bad: "Filler word detected."

        Returns:
            {"recorded": true}  — call draw_overlay next, then speak once after ALL
                                   tool calls are done. Do NOT speak immediately here.
            {"recorded": false, "reason": "...", "wait_seconds": N}
                — stay completely silent, do NOT speak any feedback.
        """
        if issue_type not in _VALID_ISSUE_TYPES:
            return {"recorded": False, "reason": "invalid_issue_type", "valid_types": sorted(_VALID_ISSUE_TYPES)}

        now = time.monotonic()

        # Confidence gate: subjective types require a recent measurement-tool call
        # to ground the claim in observed evidence, not just model impressions.
        if issue_type in _GROUNDING_REQUIRED_TYPES:
            grounding_age = now - _last_grounding_time[0]
            if grounding_age > _GROUNDING_WINDOW_S:
                _emit_telemetry(
                    "gate_blocked",
                    "grounding_required",
                    {"issue_type": issue_type, "grounding_age_s": round(grounding_age)},
                )
                return {
                    "recorded": False,
                    "reason": "grounding_required",
                    "hint": "Call get_recent_transcript() first to ground the observation.",
                    "grounding_age_s": round(grounding_age),
                }

        conf = _confidence_gate(issue_type)
        if not conf.get("ok", False):
            _emit_telemetry(
                "gate_blocked",
                "two_signal_gate",
                {
                    "issue_type": issue_type,
                    "required": conf.get("required", []),
                    "hits": conf.get("hits", []),
                    "window_s": conf.get("window_s", 0),
                },
            )
            return {
                "recorded": False,
                "reason": "two_signal_gate",
                "required_signals": conf.get("required", []),
                "observed_signals": conf.get("hits", []),
                "window_s": conf.get("window_s", 0),
            }

        # Hard gate: global cooldown across all issue types
        global_wait = global_cooldown_s - (now - _last_any[0])
        if global_wait > 0:
            _emit_telemetry(
                "gate_blocked",
                "global_cooldown",
                {"issue_type": issue_type, "wait_seconds": round(global_wait)},
            )
            return {
                "recorded": False,
                "reason": "global_cooldown",
                "wait_seconds": round(global_wait),
            }

        # Hard gate: per-type cooldown prevents repeating the same feedback
        type_wait = per_type_cooldown_s - (now - _last_by_type.get(issue_type, 0.0))
        if type_wait > 0:
            _emit_telemetry(
                "gate_blocked",
                "type_cooldown",
                {"issue_type": issue_type, "wait_seconds": round(type_wait)},
            )
            return {
                "recorded": False,
                "reason": "type_cooldown",
                "wait_seconds": round(type_wait),
            }

        # Passed both gates — record and notify
        _last_any[0] = now
        _last_by_type[issue_type] = now

        # Compute measured evidence at trigger time — actual metric values, not a static map
        evidence = _compute_measured(issue_type)
        evidence["confidence_signals"] = conf.get("hits", [])

        state.record_event(issue_type, description, evidence=evidence)
        state.register_confirmed_issue(issue_type)
        _emit_telemetry(
            "interrupt_confirmed",
            issue_type,
            {"issue_type": issue_type, "signals": conf.get("hits", [])},
        )

        # Push a visible tool-call event to the browser for architecture legibility
        state._enqueue({
            "type": "tool_call",
            "tool": "flag_issue",
            "args": {"issue_type": issue_type, "description": description, "evidence": evidence},
        })

        # Hard-enforce visual hint on slide issues — don't rely on model variance
        if issue_type in ("slide_clarity", "slide_mismatch") and _visual_hint_fn[0] is not None:
            _visual_hint_fn[0]("ideal_slide", description)

        return {"recorded": True, "issue_type": issue_type}

    def get_speech_metrics() -> dict:
        """
        Get objective speech metrics computed from the session transcript.

        Returns BOTH a rolling 20-second window and the session total so you
        can detect sustained fast pace or a burst of filler words accurately.

        Use these thresholds:
        - Flag "pace" only if wpm_20s > 180  (sustained fast speech right now)
        - Flag "filler" only if filler_count_30s >= 3  (burst in the last 30 seconds)
        - Cite wpm_20s in your feedback, not the session average.

        Returns:
            wpm_20s: WPM computed over the last 20 seconds of presenter speech.
            wpm_session: Average WPM for the full session (context only).
            filler_count_30s: Filler words in the last 30 seconds (use for triggering).
            filler_count_total: Lifetime filler count (context only).
            filler_breakdown_30s: Per-word counts in the last 30 seconds.
            duration_s: Session duration in seconds.
            word_count: Total presenter words spoken.
        """
        now_rel = state.duration_seconds()   # seconds since session start
        all_segs = [seg for seg in state.transcript if seg.get("s") == "u"]

        # ── Rolling 20s window for WPM ────────────────────────────
        cutoff_20s = now_rel - 20.0
        recent_20s = [seg for seg in all_segs if seg.get("ts", 0) >= cutoff_20s]
        recent_20s_words = sum(len(seg["t"].split()) for seg in recent_20s)
        elapsed_window = min(now_rel, 20.0)
        wpm_20s = round(recent_20s_words / elapsed_window * 60, 1) if elapsed_window > 2 else 0.0

        # ── Session-average WPM ───────────────────────────────────
        all_words = " ".join(seg["t"] for seg in all_segs)
        total_word_count = len(all_words.split()) if all_words.strip() else 0
        wpm_session = round(total_word_count / now_rel * 60, 1) if now_rel > 5 else 0.0

        # ── Rolling 30s filler window ─────────────────────────────
        cutoff_30s = now_rel - 30.0
        recent_30s_text = " ".join(
            seg["t"] for seg in all_segs if seg.get("ts", 0) >= cutoff_30s
        )
        filler_breakdown_30s: dict[str, int] = {}
        for match in _FILLER_RE.finditer(recent_30s_text):
            w = match.group(0).lower()
            filler_breakdown_30s[w] = filler_breakdown_30s.get(w, 0) + 1

        # ── Session-total filler count ────────────────────────────
        total_fillers: dict[str, int] = {}
        for match in _FILLER_RE.finditer(all_words):
            w = match.group(0).lower()
            total_fillers[w] = total_fillers.get(w, 0) + 1

        result = {
            "wpm_20s": wpm_20s,
            "wpm_session": wpm_session,
            "filler_count_30s": sum(filler_breakdown_30s.values()),
            "filler_count_total": sum(total_fillers.values()),
            "filler_breakdown_30s": filler_breakdown_30s,
            "duration_s": round(now_rel, 1),
            "word_count": total_word_count,
            "pitch_variance_hz": float(state.prosody_metrics.get("pitch_variance_hz", 0.0)),
            "pause_ratio_20s": float(state.prosody_metrics.get("pause_ratio_20s", 0.0)),
            "speaking_energy": float(state.prosody_metrics.get("speaking_energy", 0.0)),
            "monotony_score": float(state.prosody_metrics.get("monotony_score", 0.0)),
            "voiced_seconds_20s": float(state.prosody_metrics.get("voiced_seconds_20s", 0.0)),
        }

        if result["wpm_20s"] > 180:
            state.record_signal(
                "pace_high",
                measured=round(result["wpm_20s"], 1),
                threshold=">180 WPM",
                source="speech_metrics",
            )
        if result["filler_count_30s"] >= 3:
            state.record_signal(
                "filler_burst",
                measured=int(result["filler_count_30s"]),
                threshold=">=3 in 30s",
                source="speech_metrics",
            )
        if result["voiced_seconds_20s"] >= 6 and result["pause_ratio_20s"] < 0.10:
            state.record_signal(
                "pause_low",
                measured=round(result["pause_ratio_20s"], 3),
                threshold="<0.10",
                source="prosody",
            )
        if result["voiced_seconds_20s"] >= 6 and result["monotony_score"] >= 70:
            state.record_signal(
                "monotony_high",
                measured=round(result["monotony_score"], 1),
                threshold=">=70",
                source="prosody",
            )

        # Push tool call to browser so judges can see objective measurement
        state._enqueue({
            "type": "tool_call",
            "tool": "get_speech_metrics",
            "args": result,
        })
        _last_grounding_time[0] = time.monotonic()
        return result

    def get_recent_transcript(n_words: int = 60) -> str:
        """
        Return the last N words the presenter said.

        Use this when you need to re-read a recent passage before deciding
        whether to flag a contradiction or clarity issue.  Avoids flagging
        based on a vague impression.

        Args:
            n_words: Number of words to return (default 60, max 200).

        Returns:
            A string of the last N presenter words, or "(no transcript yet)".
        """
        n_words = min(max(1, n_words), 200)
        user_segs = [seg["t"] for seg in state.transcript if seg.get("s") == "u"]
        all_words = " ".join(user_segs).split()
        text = " ".join(all_words[-n_words:]) if all_words else "(no transcript yet)"

        state._enqueue({
            "type": "tool_call",
            "tool": "get_recent_transcript",
            "args": {"n_words": n_words, "returned_words": len(all_words[-n_words:])},
        })
        state.record_signal("transcript_grounded", measured=n_words, threshold="recent_window", source="transcript")
        _last_grounding_time[0] = time.monotonic()
        return text

    # Mutable state for eye contact debounce — shared across check_eye_contact calls
    _gaze_away_since: list[float] = [0.0]   # monotonic timestamp when gaze drop started
    _gaze_currently_away: list[bool] = [False]

    def check_eye_contact(gaze_direction: str) -> dict:
        """
        Report the current gaze direction from a single video frame and get a
        server-confirmed verdict on whether the eye contact rule is triggered.

        The server tracks how long the presenter has been continuously disengaged
        and confirms a violation only after a context-aware threshold. This
        prevents false positives from brief glances.

        Call this every time you observe the presenter's gaze in a video frame.
        Only call flag_issue(issue_type="eye_contact") if this returns
        {"confirmed": true}.

        Args:
            gaze_direction: One of:
                "camera"   — camera-facing engagement
                "audience" — engaged with room/audience (healthy for in-person/hybrid)
                "notes"    — looking down at notes
                "away"     — disengaged/off-point gaze

        Returns:
            confirmed: true only when disengaged gaze exceeds threshold.
            seconds_away: Current disengaged streak (float).
            threshold_s: Context-dependent threshold.
            delivery_context: virtual / in_person / hybrid
        """
        now = time.monotonic()
        _EYE_CONTACT_THRESHOLD_S = {
            "virtual": 5.0,
            "hybrid": 6.5,
            "in_person": 8.0,
        }.get(state.delivery_context, 5.0)
        norm_gaze = (gaze_direction or "").strip().lower()

        # In in-person/hybrid sessions, "audience" is considered engaged, not away.
        is_disengaged = norm_gaze in {"away", "notes"}

        if is_disengaged:
            if not _gaze_currently_away[0]:
                # Start of a new away streak
                _gaze_currently_away[0] = True
                _gaze_away_since[0] = now
            seconds_away = now - _gaze_away_since[0]
            confirmed = seconds_away >= _EYE_CONTACT_THRESHOLD_S
        else:
            # Gaze returned to camera — reset streak
            _gaze_currently_away[0] = False
            _gaze_away_since[0] = 0.0
            seconds_away = 0.0
            confirmed = False

        result = {
            "confirmed": confirmed,
            "seconds_away": round(seconds_away, 1),
            "threshold_s": _EYE_CONTACT_THRESHOLD_S,
            "delivery_context": state.delivery_context,
        }

        state._enqueue({
            "type": "tool_call",
            "tool": "check_eye_contact",
            "args": {"gaze": norm_gaze or gaze_direction, **result},
        })
        if confirmed:
            state.record_signal(
                "eye_contact_confirmed",
                measured=round(seconds_away, 1),
                threshold=f">={_EYE_CONTACT_THRESHOLD_S}s",
                source="eye_contact",
            )
        _last_grounding_time[0] = time.monotonic()
        return result

    def check_slide_clarity(signal: str, evidence: str = "") -> dict:
        """
        Validate a screen-observed slide issue before raising an interruption.

        Use this when screen-sharing is enabled and you spot clutter, unreadable
        text, weak visual hierarchy, or mismatch between narration and slide.
        """
        norm_signal = (signal or "").strip().lower()
        if norm_signal not in _valid_slide_signals:
            return {
                "confirmed": False,
                "reason": "invalid_signal",
                "valid_signals": sorted(_valid_slide_signals),
            }

        now = time.monotonic()
        wait = 25.0 - (now - _last_slide_signal.get(norm_signal, 0.0))
        if wait > 0:
            result = {
                "confirmed": False,
                "reason": "slide_signal_cooldown",
                "wait_seconds": round(wait),
                "signal": norm_signal,
            }
            state._enqueue({
                "type": "tool_call",
                "tool": "check_slide_clarity",
                "args": {"signal": norm_signal, **result},
            })
            return result

        _last_slide_signal[norm_signal] = now
        state.record_signal(
            f"slide_{norm_signal}",
            measured=1,
            threshold="confirmed",
            source="slide_clarity",
        )
        suggestion_map = {
            "clutter": "Cut to three bullets max and one visual.",
            "unreadable_text": "Increase font size and simplify chart labels.",
            "weak_hierarchy": "Use one headline takeaway plus supporting points.",
            "speech_mismatch": "Align spoken message with this slide takeaway.",
        }
        result = {
            "confirmed": True,
            "signal": norm_signal,
            "suggested_callout": suggestion_map[norm_signal],
            "evidence": (evidence or "")[:220],
        }
        state._enqueue({
            "type": "tool_call",
            "tool": "check_slide_clarity",
            "args": result,
        })
        _last_grounding_time[0] = time.monotonic()
        return result

    def draw_overlay(x: float, y: float, label: str = "") -> dict:
        """
        Draw a temporary visual highlight (a glowing ring) on the presenter's
        video feed at the specified (x, y) coordinates.

        Use this to visually direct the presenter's attention, for example:
        - Highlight the camera lens (e.g. 0.5, 0.1) if they are looking away.
        - Highlight a cluttered area on a slide during screen sharing.

        Args:
            x: Horizontal coordinate from 0.0 (left) to 1.0 (right).
            y: Vertical coordinate from 0.0 (top) to 1.0 (bottom).
            label: Short text to display next to the highlight (max 20 chars).

        Returns:
            {"status": "drawn"}
        """
        state._enqueue({
            "type": "tool_call",
            "tool": "draw_overlay",
            "args": {
                "x": max(0.0, min(1.0, float(x))),
                "y": max(0.0, min(1.0, float(y))),
                "label": (label or "")[:20],
            },
        })
        return {"status": "drawn"}

    def navigate_practice_slides(action: str = "next") -> dict:
        """
        Trigger a frontend slide-navigation action in the built-in practice deck.

        Args:
            action: One of "next", "previous", "first", "last".
        """
        norm = (action or "").strip().lower()
        if norm not in {"next", "previous", "first", "last"}:
            return {"status": "ignored", "reason": "invalid_action"}

        if state.total_slides > 0:
            prev = state.current_slide_index
            last_index = max(0, state.total_slides - 1)
            if norm == "next":
                state.current_slide_index = min(last_index, state.current_slide_index + 1)
            elif norm == "previous":
                state.current_slide_index = max(0, state.current_slide_index - 1)
            elif norm == "first":
                state.current_slide_index = 0
            elif norm == "last":
                state.current_slide_index = last_index

            changed = state.current_slide_index != prev
            state._enqueue({
                "type": "slide_change",
                "source": "agent",
                "action": norm,
                "current_slide_index": state.current_slide_index,
                "total_slides": state.total_slides,
                "changed": changed,
            })

        state._enqueue({
            "type": "tool_call",
            "tool": "navigate_practice_slides",
            "args": {"action": norm},
        })
        return {
            "status": "ok",
            "action": norm,
            "current_slide_index": state.current_slide_index,
            "total_slides": state.total_slides,
        }

    _VALID_MARK_TYPES = frozenset({
        "clutter", "font_too_small", "low_contrast", "off_topic", "missing_data",
    })

    def jump_to_slide(index: int) -> dict:
        """
        Jump directly to a specific slide by index (0-based) in the active deck.

        Use when you need to navigate directly to a slide to review or critique it,
        rather than paging through with next/previous.

        Args:
            index: Zero-based slide index (0 = first slide).

        Returns:
            {"status": "ok", "current_slide_index": int, "total_slides": int}
            {"status": "ignored", "reason": "no_slides_loaded"|"out_of_range"}
        """
        if state.total_slides <= 0:
            return {"status": "ignored", "reason": "no_slides_loaded"}
        idx = max(0, min(state.total_slides - 1, int(index)))
        state.current_slide_index = idx
        state._enqueue({
            "type": "slide_change",
            "source": "agent",
            "action": "jump",
            "current_slide_index": idx,
            "total_slides": state.total_slides,
            "changed": True,
        })
        state._enqueue({
            "type": "tool_call",
            "tool": "jump_to_slide",
            "args": {"index": idx, "total_slides": state.total_slides},
        })
        return {"status": "ok", "current_slide_index": idx, "total_slides": state.total_slides}

    def mark_slide_issue(issue_type: str, label: str = "") -> dict:
        """
        Annotate the current slide with a specific visual issue marker in the UI.

        Use to flag problems detected while reviewing a slide.
        Unlike flag_issue, this does NOT count against interruption cooldowns —
        it's a silent annotation, not a spoken correction.

        Args:
            issue_type: One of: "clutter", "font_too_small", "low_contrast",
                        "off_topic", "missing_data"
            label: Optional short annotation text (max 40 chars).

        Returns:
            {"status": "marked", "slide_index": int, "issue_type": str}
        """
        norm = (issue_type or "").strip().lower()
        if norm not in _VALID_MARK_TYPES:
            return {"status": "error", "reason": "invalid_issue_type", "valid_types": sorted(_VALID_MARK_TYPES)}
        label_clean = (label or "")[:40]
        slide_idx = state.current_slide_index
        state._enqueue({
            "type": "slide_mark",
            "issue_type": norm,
            "label": label_clean,
            "slide_index": slide_idx,
        })
        state._enqueue({
            "type": "tool_call",
            "tool": "mark_slide_issue",
            "args": {"issue_type": norm, "label": label_clean, "slide_index": slide_idx},
        })
        return {"status": "marked", "slide_index": slide_idx, "issue_type": norm}

    def generate_live_visual_hint(hint_type: str = "ideal_slide", context: str = "") -> dict:
        """
        Generate a visual coaching image and display it live in the session UI.

        Call when the presenter asks to see a visual example:
        - "Show me what an ideal slide looks like"
        - "Show me proper camera/posture setup"
        - "Give me a visual for this concept"

        The image generates asynchronously (10-20s) — this returns immediately
        so you can speak your explanation while the image loads in the UI.

        Args:
            hint_type: One of:
                "ideal_slide"   — clean slide layout for the current topic
                "ideal_posture" — optimal camera and presenter setup
                "key_concept"   — visual representing the concept being discussed
            context: 1-2 sentence description of what the visual should show.
                     Good: "A clean slide comparing O(n) vs O(n^2) algorithm complexity"

        Returns:
            {"status": "generating", "hint_type": ..., "title": ...}
            The actual image arrives via a live_visual_hint event within ~15 seconds.
        """
        import asyncio
        import base64
        import logging as _logging

        _ht = (hint_type or "ideal_slide").strip().lower()
        if _ht not in {"ideal_slide", "ideal_posture", "key_concept"}:
            _ht = "ideal_slide"

        _title_map = {
            "ideal_slide":   "Ideal Slide Layout",
            "ideal_posture": "Ideal Delivery Setup",
            "key_concept":   "Key Concept Visual",
        }
        title = _title_map[_ht]
        _ctx = (context or "")[:300]
        rationale = _ctx or "Reduce clutter and emphasize one clear takeaway."

        # Notify frontend immediately so a spinner appears in the transcript feed
        state._enqueue({
            "type": "live_visual_hint",
            "status": "generating",
            "hint_type": _ht,
            "title": title,
        })
        state._enqueue({
            "type": "tool_call",
            "tool": "generate_live_visual_hint",
            "args": {"hint_type": _ht, "title": title, "context_preview": _ctx[:80]},
        })

        async def _generate_and_deliver():
            _logger = _logging.getLogger(__name__)
            try:
                from backend.multimodal import (
                    _generate_with_retry, DEFAULT_IMAGE_MODEL,
                    _render_fallback_card, _prompt_delivery_setup, _prompt_slide_layout,
                )
                if _ht == "ideal_posture":
                    prompt = _prompt_delivery_setup(state, _ctx)
                elif _ht == "ideal_slide" and _ctx:
                    mode_label = state.coach_mode.replace("_", " ")
                    prompt = (
                        f"Create a clean, professional presentation slide. "
                        f"Context: {_ctx}. Mode: {mode_label}. "
                        "One clear headline, supporting bullets, strong visual hierarchy. "
                        "Style: modern, white background, blue accents, no clutter."
                    )
                elif _ht == "ideal_slide":
                    prompt = _prompt_slide_layout(state, _ctx)
                else:  # key_concept
                    prompt = (
                        f"Create an engaging educational visual explaining: {_ctx}. "
                        "Clean infographic style, strong typography, instantly clear concept."
                    )

                img_bytes = await _generate_with_retry(
                    prompt, model=DEFAULT_IMAGE_MODEL, timeout_s=22, retries=0,
                )
                source = "imagen"
            except Exception as exc:
                _logger.warning("Live visual hint generation failed: %s", exc)
                img_bytes = None
                source = "fallback"

            if not img_bytes:
                from backend.multimodal import _render_fallback_card
                img_bytes = _render_fallback_card(title, _ctx[:80] or "Live coaching visual")
                source = "fallback"

            before_b64 = (state.last_slide_frame_b64 or "") if _ht == "ideal_slide" else ""
            state.live_visual_hints_ready += 1
            state._enqueue({
                "type": "live_visual_hint",
                "status": "ready",
                "hint_type": _ht,
                "title": title,
                "mime_type": "image/jpeg",
                "data_base64": base64.b64encode(img_bytes).decode("ascii"),
                "source": source,
                "before_b64": before_b64,  # non-empty → render before/after side-by-side
                "rationale": rationale[:160],
            })

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_generate_and_deliver())
        except RuntimeError:
            pass  # No event loop (e.g. tests) — skip silently

        return {"status": "generating", "hint_type": _ht, "title": title}

    # Bind the late reference so flag_issue can auto-trigger on slide issues
    _visual_hint_fn[0] = generate_live_visual_hint

    return [
        flag_issue,
        get_speech_metrics,
        get_recent_transcript,
        check_eye_contact,
        check_slide_clarity,
        draw_overlay,
        navigate_practice_slides,
        jump_to_slide,
        mark_slide_issue,
        generate_live_visual_hint,
    ]
