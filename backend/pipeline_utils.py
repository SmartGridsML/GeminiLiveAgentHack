"""
PitchMirror — post-session pipeline utilities.

Pure functions with no heavy dependencies so they can be imported
independently in tests without pulling in the ADK/FastAPI stack.
"""
from __future__ import annotations
import re

# ── AI score extraction ───────────────────────────────────────────

_SCORE_LINE_RE = re.compile(
    r"^SCORE_(FILLER|PACE|EYE|CLARITY|VISUAL)\s*:\s*(\d+)\b.*$",
    re.MULTILINE | re.IGNORECASE,
)


def extract_ai_scores(text: str) -> dict[str, int]:
    """Parse SCORE_* lines emitted by the synthesis agent into a name→int dict."""
    result: dict[str, int] = {}
    for m in _SCORE_LINE_RE.finditer(text or ""):
        result[m.group(1).lower()] = max(0, min(100, int(m.group(2))))
    return result


# ── Synthesis output validation ───────────────────────────────────

REQUIRED_SYNTHESIS_SECTIONS = [
    "**Opening & Core Message**",
    "**Content & Structure**",
    "**Delivery**",
    "**Top Fixes**",
    "**What Worked**",
]


def validate_synthesis(text: str) -> bool:
    """Return True only if all required synthesis section headers are present."""
    return bool(text) and all(s in text for s in REQUIRED_SYNTHESIS_SECTIONS)


def missing_synthesis_sections(text: str) -> list[str]:
    """Return list of section headers absent from the synthesis output."""
    return [s for s in REQUIRED_SYNTHESIS_SECTIONS if s not in (text or "")]
