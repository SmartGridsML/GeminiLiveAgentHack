"""
PitchMirror — core unit tests.

Run with:  python -m pytest tests/ -v
"""
import asyncio
import json
import time
import types as python_types

import pytest


# ── Scorecard builder ─────────────────────────────────────────────

class _FakeState:
    """Minimal SessionState stub for scorecard tests."""
    session_id = "test-session"
    user_id = "tester"
    coach_mode = "presentation"
    delivery_context = "virtual"
    primary_goal = "balanced"
    persona = "coach"
    screen_enabled = False
    demo_mode = False
    total_slides = 0
    current_slide_index = -1
    session_start = time.time() - 90  # 90-second session
    events = []
    transcript = [{"s": "u", "t": "hello world", "ts": 10.0}]
    filler_count = 0
    eye_contact_drops = 0
    pace_violations = 0
    contradictions = 0
    clarity_flags = 0
    visual_flags = 0
    mismatch_flags = 0
    final_report = "Test report"
    research_tips = ""
    generated_assets = []
    ai_scores: dict = {}

    def duration_seconds(self):
        return time.time() - self.session_start


def test_scorecard_basic():
    from backend.scorecard import build_scorecard
    state = _FakeState()
    sc = build_scorecard(state)
    assert sc["session_id"] == "test-session"
    assert sc["overall_score"] == 100  # zero events → perfect
    assert "filler_words" in sc["categories"]


def test_scorecard_uses_ai_scores_when_present():
    from backend.scorecard import build_scorecard
    state = _FakeState()
    state.ai_scores = {"filler": 30, "pace": 40, "eye": 95, "clarity": 55, "visual": 100}
    sc = build_scorecard(state)
    assert sc["categories"]["filler_words"]["score"] == 30
    assert sc["categories"]["pace"]["score"] == 40
    assert sc["categories"]["eye_contact"]["score"] == 95
    assert sc["categories"]["clarity"]["score"] == 55
    assert sc["categories"]["visual_delivery"]["score"] == 100


def test_scorecard_falls_back_without_ai_scores():
    from backend.scorecard import build_scorecard
    state = _FakeState()
    state.filler_count = 10
    state.ai_scores = {}  # no AI scores → use metric-based calculation
    sc = build_scorecard(state)
    # 10 fillers in 90s = ~6.7/min → falls in 4-7 range → score 35
    assert sc["categories"]["filler_words"]["score"] <= 55


def test_scorecard_short_session():
    from backend.scorecard import build_scorecard
    state = _FakeState()
    state.session_start = time.time() - 3   # only 3 seconds
    state.transcript = []
    state.events = []
    sc = build_scorecard(state)
    assert sc["overall_score"] == 0   # degenerate session guard


def test_scorecard_ai_scores_partial_fallback():
    from backend.scorecard import build_scorecard
    state = _FakeState()
    state.ai_scores = {"filler": 42}  # only filler; rest fall back to metric-based
    sc = build_scorecard(state)
    assert sc["categories"]["filler_words"]["score"] == 42
    assert sc["categories"]["pace"]["score"] == 100  # 0 violations → 100


# ── AI score extraction ───────────────────────────────────────────

def test_extract_ai_scores_full():
    from backend.pipeline_utils import extract_ai_scores as _extract_ai_scores
    text = """
**What Worked**
Some strength.

**IMAGE_PROMPTS**
IMAGE_PROMPT_1: some prompt
IMAGE_PROMPT_2: another prompt

**SCORES**
SCORE_FILLER: 40
SCORE_PACE: 60
SCORE_EYE: 95
SCORE_CLARITY: 55
SCORE_VISUAL: 100
"""
    scores = _extract_ai_scores(text)
    assert scores == {"filler": 40, "pace": 60, "eye": 95, "clarity": 55, "visual": 100}


def test_extract_ai_scores_clamps_values():
    from backend.pipeline_utils import extract_ai_scores as _extract_ai_scores
    # Values > 100 are clamped to 100; negative values aren't matched by regex
    # (model never outputs them, regex requires \d+)
    text = "SCORE_FILLER: 150\nSCORE_PACE: 0\n"
    scores = _extract_ai_scores(text)
    assert scores["filler"] == 100   # clamped from 150
    assert scores["pace"] == 0       # exactly at floor


def test_extract_ai_scores_empty():
    from backend.pipeline_utils import extract_ai_scores as _extract_ai_scores
    assert _extract_ai_scores("") == {}
    assert _extract_ai_scores("no scores here") == {}


# ── Synthesis validation ──────────────────────────────────────────

def test_validate_synthesis_passes():
    from backend.pipeline_utils import validate_synthesis as _validate_synthesis
    text = (
        "**Opening & Core Message**\nOK\n"
        "**Content & Structure**\nOK\n"
        "**Delivery**\nOK\n"
        "**Top Fixes**\n1. Fix\n"
        "**What Worked**\nGood.\n"
    )
    assert _validate_synthesis(text) is True


def test_validate_synthesis_fails_missing_section():
    from backend.pipeline_utils import validate_synthesis as _validate_synthesis
    text = "**Opening & Core Message**\nOK\n**Delivery**\nOK\n"
    assert _validate_synthesis(text) is False


def test_validate_synthesis_empty():
    from backend.pipeline_utils import validate_synthesis as _validate_synthesis
    assert _validate_synthesis("") is False
    assert _validate_synthesis(None) is False


def test_missing_synthesis_sections():
    from backend.pipeline_utils import missing_synthesis_sections as _missing_synthesis_sections
    text = "**Opening & Core Message**\nOK\n**What Worked**\nGood.\n"
    missing = _missing_synthesis_sections(text)
    assert "**Content & Structure**" in missing
    assert "**Delivery**" in missing
    assert "**Top Fixes**" in missing
    assert "**Opening & Core Message**" not in missing


# ── Image prompt extraction ───────────────────────────────────────

def test_extract_image_prompts_strips_section():
    from backend.multimodal import extract_image_prompts
    report = (
        "**What Worked**\nStrong opener.\n\n"
        "**IMAGE_PROMPTS**\n"
        "IMAGE_PROMPT_1: A clean slide with headline and 3 bullets.\n"
        "IMAGE_PROMPT_2: A data visualization slide.\n"
        "SCORE_FILLER: 45\n"
    )
    clean, prompts = extract_image_prompts(report)
    assert "**IMAGE_PROMPTS**" not in clean
    assert "SCORE_FILLER" not in clean
    assert len(prompts) == 2
    assert "headline" in prompts[0]


def test_extract_image_prompts_no_section():
    from backend.multimodal import extract_image_prompts
    report = "Just a plain report with no image prompts."
    clean, prompts = extract_image_prompts(report)
    assert clean == report
    assert prompts == []


def test_extract_image_prompts_empty_text():
    from backend.multimodal import extract_image_prompts
    clean, prompts = extract_image_prompts("")
    assert clean == ""
    assert prompts == []


# ── CoachingEvent evidence ────────────────────────────────────────

def test_coaching_event_evidence_serialized():
    from backend.session_state import SessionState
    state = SessionState(session_id="ev-test")
    state.record_event("filler", "Said um 3 times", evidence={"metric": "filler_count_30s", "measured": 3})
    serialized = json.loads(state.events_json())
    assert len(serialized) == 1
    assert serialized[0]["ev"]["metric"] == "filler_count_30s"
    assert serialized[0]["ev"]["measured"] == 3


def test_coaching_event_no_evidence_omitted():
    from backend.session_state import SessionState
    state = SessionState(session_id="ev-test-2")
    state.record_event("clarity", "Confusing sentence")
    serialized = json.loads(state.events_json())
    assert "ev" not in serialized[0]


# ── flag_issue cooldown logic ─────────────────────────────────────

def test_flag_issue_cooldown():
    from backend.session_state import SessionState
    from backend.agents.tools import make_coaching_tools

    state = SessionState(session_id="cooldown-test")
    # Add a transcript segment so metrics compute correctly
    state.add_transcript("user", "um um um basically like you know")

    tools = make_coaching_tools(state)
    flag = next(t for t in tools if t.__name__ == "flag_issue")

    # First call should succeed
    result1 = flag("filler", "Said um 3 times in 30 seconds")
    assert result1["recorded"] is True

    # Immediate second call hits global cooldown (30s gate)
    result2 = flag("pace", "Speaking too fast")
    assert result2["recorded"] is False
    assert result2["reason"] == "global_cooldown"


def test_flag_issue_invalid_type():
    from backend.session_state import SessionState
    from backend.agents.tools import make_coaching_tools

    state = SessionState(session_id="invalid-test")
    tools = make_coaching_tools(state)
    flag = next(t for t in tools if t.__name__ == "flag_issue")
    result = flag("not_a_real_type", "some description")
    assert result["recorded"] is False
    assert "invalid_issue_type" in result["reason"]


def test_flag_issue_evidence_populated():
    from backend.session_state import SessionState
    from backend.agents.tools import make_coaching_tools

    state = SessionState(session_id="evidence-test")
    state.add_transcript("user", "um uh like basically um")
    tools = make_coaching_tools(state)
    flag = next(t for t in tools if t.__name__ == "flag_issue")

    flag("filler", "Many fillers detected")
    assert len(state.events) == 1
    ev = state.events[0].evidence
    assert ev["metric"] == "filler_count_30s"
    assert "measured" in ev
    assert ev["threshold"] == "≥3"


# ── jump_to_slide ─────────────────────────────────────────────────

def test_jump_to_slide_basic():
    from backend.session_state import SessionState
    from backend.agents.tools import make_coaching_tools

    state = SessionState(session_id="slide-test")
    state.total_slides = 5
    state.current_slide_index = 0
    tools = make_coaching_tools(state)
    jump = next(t for t in tools if t.__name__ == "jump_to_slide")

    result = jump(3)
    assert result["status"] == "ok"
    assert result["current_slide_index"] == 3
    assert state.current_slide_index == 3


def test_jump_to_slide_no_slides():
    from backend.session_state import SessionState
    from backend.agents.tools import make_coaching_tools

    state = SessionState(session_id="slide-test-2")
    state.total_slides = 0
    tools = make_coaching_tools(state)
    jump = next(t for t in tools if t.__name__ == "jump_to_slide")
    result = jump(2)
    assert result["status"] == "ignored"


def test_jump_to_slide_clamps_bounds():
    from backend.session_state import SessionState
    from backend.agents.tools import make_coaching_tools

    state = SessionState(session_id="slide-test-3")
    state.total_slides = 3
    state.current_slide_index = 0
    tools = make_coaching_tools(state)
    jump = next(t for t in tools if t.__name__ == "jump_to_slide")

    # Out of range → clamp to last valid index
    result = jump(99)
    assert result["status"] == "ok"
    assert result["current_slide_index"] == 2  # max index for 3 slides


# ── mark_slide_issue ──────────────────────────────────────────────

def test_mark_slide_issue_valid():
    from backend.session_state import SessionState
    from backend.agents.tools import make_coaching_tools

    state = SessionState(session_id="mark-test")
    state.current_slide_index = 2
    tools = make_coaching_tools(state)
    mark = next(t for t in tools if t.__name__ == "mark_slide_issue")

    result = mark("clutter", "Too many bullets")
    assert result["status"] == "marked"
    assert result["slide_index"] == 2
    assert result["issue_type"] == "clutter"


def test_mark_slide_issue_invalid_type():
    from backend.session_state import SessionState
    from backend.agents.tools import make_coaching_tools

    state = SessionState(session_id="mark-test-2")
    tools = make_coaching_tools(state)
    mark = next(t for t in tools if t.__name__ == "mark_slide_issue")
    result = mark("not_a_real_type")
    assert result["status"] == "error"
