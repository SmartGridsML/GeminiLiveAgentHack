"""
PitchMirror — scorecard builder.
Converts SessionState into a structured display-ready scorecard dict.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.session_state import SessionState

# Minimum meaningful session length; anything shorter gets a null scorecard.
_MIN_SESSION_SEC = 5.0


def build_scorecard(state: "SessionState") -> dict:
    duration_sec = state.duration_seconds()
    duration_min = duration_sec / 60
    created_at = int(state.session_start)

    # Guard: degenerate sessions (immediate disconnect / no audio) must not
    # score 100 just because all counters happen to be zero.
    if duration_sec < _MIN_SESSION_SEC or (not state.transcript and not state.events):
        return {
            "session_id": state.session_id,
            "user_id": state.user_id,
            "coach_mode": state.coach_mode,
            "delivery_context": state.delivery_context,
            "primary_goal": state.primary_goal,
            "screen_enabled": state.screen_enabled,
            "demo_mode": state.demo_mode,
            "total_slides": state.total_slides,
            "current_slide_index": state.current_slide_index,
            "created_at": created_at,
            "duration_seconds": round(duration_sec, 1),
            "overall_score": 0,
            "categories": {},
            "coaching_events": [],
            "transcript": state.transcript,
            "final_report": state.final_report or "(Session too short to evaluate.)",
            "research_tips": state.research_tips,
            "generated_assets": state.generated_assets,
        }

    # Use holistic AI scores from the synthesis agent when available;
    # fall back to metric-based scoring for any dimension not covered.
    ai = getattr(state, "ai_scores", {})
    filler_score = ai["filler"] if "filler" in ai else _score_filler(state.filler_count, duration_min)
    eye_score    = ai["eye"]    if "eye"    in ai else _score_eye(state.eye_contact_drops, duration_min)
    pace_score   = ai["pace"]   if "pace"   in ai else _score_pace(state.pace_violations, duration_min)
    clarity_score = ai["clarity"] if "clarity" in ai else _score_clarity(state.contradictions, state.clarity_flags)
    visual_score  = ai["visual"]  if "visual"  in ai else _score_visual(state.visual_flags, state.mismatch_flags, duration_min)

    overall = int(
        filler_score * 0.30
        + eye_score * 0.25
        + pace_score * 0.20
        + clarity_score * 0.15
        + visual_score * 0.10
    )

    return {
        "session_id": state.session_id,
        "user_id": state.user_id,
        "coach_mode": state.coach_mode,
        "delivery_context": state.delivery_context,
        "primary_goal": state.primary_goal,
        "screen_enabled": state.screen_enabled,
        "demo_mode": state.demo_mode,
        "total_slides": state.total_slides,
        "current_slide_index": state.current_slide_index,
        "created_at": created_at,
        "duration_seconds": round(duration_sec, 1),
        "overall_score": overall,
        "categories": {
            "filler_words": {
                "score": filler_score,
                "count": state.filler_count,
                "label": _grade(filler_score),
            },
            "eye_contact": {
                "score": eye_score,
                "drops": state.eye_contact_drops,
                "label": _grade(eye_score),
            },
            "pace": {
                "score": pace_score,
                "violations": state.pace_violations,
                "label": _grade(pace_score),
            },
            "clarity": {
                "score": clarity_score,
                "contradictions": state.contradictions,
                "flags": state.clarity_flags,
                "label": _grade(clarity_score),
            },
            "visual_delivery": {
                "score": visual_score,
                "slide_clarity_flags": state.visual_flags,
                "slide_mismatch_flags": state.mismatch_flags,
                "label": _grade(visual_score),
            },
        },
        "coaching_events": [
            {
                "timestamp": round(e.timestamp, 1),
                "type": e.event_type,
                "text": e.description,
                "evidence": e.evidence,
            }
            for e in state.events
        ],
        "transcript": state.transcript,
        "final_report": state.final_report,
        "research_tips": state.research_tips,
        "generated_assets": state.generated_assets,
    }


def _score_filler(count: int, duration_min: float) -> int:
    if duration_min <= 0:
        return 100
    rate = count / duration_min
    if rate == 0:   return 100
    if rate < 1:    return 90
    if rate < 2:    return 75
    if rate < 4:    return 55
    if rate < 7:    return 35
    return 15


def _score_eye(drops: int, duration_min: float) -> int:
    if duration_min <= 0:
        return 100
    rate = drops / duration_min
    if rate == 0:   return 100
    if rate < 1:    return 85
    if rate < 2:    return 65
    if rate < 4:    return 40
    return 20


def _score_pace(violations: int, duration_min: float) -> int:
    if duration_min <= 0:
        return 100
    rate = violations / duration_min
    if rate == 0:   return 100
    if rate < 1:    return 80
    if rate < 2:    return 60
    return 30


def _score_clarity(contradictions: int, clarity_flags: int) -> int:
    total = contradictions + clarity_flags
    if total == 0:  return 100
    if total == 1:  return 75
    if total == 2:  return 55
    if total <= 4:  return 35
    return 15


def _score_visual(slide_flags: int, mismatch_flags: int, duration_min: float) -> int:
    if duration_min <= 0:
        return 100
    weighted = (slide_flags * 0.7) + (mismatch_flags * 1.1)
    rate = weighted / duration_min
    if rate == 0:
        return 100
    if rate < 0.7:
        return 85
    if rate < 1.4:
        return 65
    if rate < 2.2:
        return 45
    return 25


def _grade(score: int) -> str:
    if score >= 90: return "Excellent"
    if score >= 75: return "Good"
    if score >= 55: return "Needs Work"
    if score >= 35: return "Poor"
    return "Critical"
