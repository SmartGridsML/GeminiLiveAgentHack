"""
PitchMirror — post-session analysis agents.

Each agent is a module-level singleton (stateless — all state lives in sessions).
main.py orchestrates them manually with asyncio.gather so it can emit real-time
pipeline_step events to the browser as each one completes, rather than hiding
all work inside a SequentialAgent black box.

Architecture:
  _run_post_session (main.py)
    ├── asyncio.gather(DELIVERY_ANALYST, CONTENT_ANALYST, RESEARCH_AGENT)
    │     ├── pipeline_step: delivery_done  ← sent when delivery finishes
    │     ├── pipeline_step: content_done   ← sent when content finishes
    │     └── pipeline_step: research_done  ← sent when research finishes
    ├── SYNTHESIS_AGENT (runs after all three)
    │     └── pipeline_step: synthesis_done ← sent before analysis_complete
    └── SESSION_SUMMARY_AGENT (runs after synthesis)
          └── pipeline_step: memory_updated ← writes structured user profile

ADK pipeline objects (PARALLEL_ANALYSTS, POST_SESSION_PIPELINE) make the
multi-agent architecture explicit. main.py uses manual asyncio.gather for
real-time WebSocket step events; POST_SESSION_PIPELINE is available for
direct invocation in non-streaming contexts.

google_search cannot be mixed with function tools on the same agent,
so RESEARCH_AGENT is isolated with only google_search.
"""
from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
from google.adk.tools import google_search

from backend.coach import ANALYSIS_MODEL


DELIVERY_ANALYST = LlmAgent(
    name="delivery_analyst",
    model=ANALYSIS_MODEL,
    instruction="""You are a specialist in vocal delivery and physical presence for high-stakes speaking.

The full session transcript and live coaching events are in your context.

Analyze EACH of the following delivery dimensions. Be specific — quote the transcript directly,
include timestamps from coaching events where available, and give a concrete fix for every issue found.

DIMENSIONS TO COVER:
1. Filler words — which words, how many total, which sections were worst?
2. Speaking pace — did they rush? Were there unnatural gaps? Was there variation?
3. Eye contact — when did they look away? How many times flagged by coach?
4. Vocal authority — did they sound confident or tentative? Quote hedging phrases like "I think", "maybe", "sort of".
5. Repetition — did they repeat phrases or sentences verbatim? Quote examples.

Format: one concise bullet per dimension. If a dimension showed no problems, say so in one clause.
No preamble. No summary. Just the 5 bullets.""",
    output_key="delivery_analysis",
)

CONTENT_ANALYST = LlmAgent(
    name="content_analyst",
    model=ANALYSIS_MODEL,
    instruction="""You are a specialist in speaking structure, storytelling, and message clarity.

The full session transcript is in your context. Read it carefully as a whole piece.
Session mode is provided in context (general/presentation/interview/sales_demo/pitch). Adapt analysis to that mode.

Analyze ALL of the following content dimensions. Quote the transcript directly — do not paraphrase.
If slide/visual issues were flagged in coaching events, include concrete visual-delivery critique.

DIMENSIONS TO COVER:
1. Opening hook — did they start with a compelling hook, question, or statistic? Or did they open weakly (e.g., "So today I'm going to talk about...")?
2. Core message — can you distill their message into one clear sentence? If not, why not — what was confusing or contradictory?
3. Audience relevance — did they explain why this matters to the listener now?
4. Main points — were the primary claims/examples explained clearly and concretely?
5. Structure — did the talk have a logical flow (context → point → evidence → close)? Name any missing sections.
6. Evidence & specifics — did they use concrete numbers, examples, or social proof? Or were claims vague?
7. Logical contradictions — did any statement contradict another? Quote both.
8. Closing — how did they end? Was there a clear next step or did they trail off?
9. Visual presentation — if slides were used, assess clutter, readability, hierarchy, and speech-slide alignment using the recorded coaching events.

Format: one concise bullet per dimension. Quote the transcript. No preamble.""",
    output_key="content_analysis",
)

RESEARCH_AGENT = LlmAgent(
    name="research_agent",
    model=ANALYSIS_MODEL,
    tools=[google_search],
    instruction="""You are finding expert-backed, evidence-based speaking improvement recommendations.

The session's coaching events and transcript are in your context. They show the presenter's specific weaknesses.

Based on the actual issues flagged, search for practical techniques:
- Filler words detected → search "how to eliminate filler words presentations expert technique"
- Eye contact drops → search "eye contact technique presentations video conferencing"
- Weak structure detected → search "public speaking structure framework"
- Lack of evidence/specifics → search "using data and stories in presentations research"
- Pace issues → search "optimal speaking pace presentations research"

Find exactly 2 specific, actionable techniques backed by credible sources (research, coaches, TEDx, etc).
For each:
- Name and describe the technique (2 sentences max)
- Cite the source (author, publication, institution, or named framework)

Format as 2 bullets. No fluff. No generic advice.""",
    output_key="research_tips",
)

SYNTHESIS_AGENT = LlmAgent(
    name="synthesis_agent",
    model=ANALYSIS_MODEL,
    instruction="""You are writing the final PitchMirror speaking coaching report.

You have three analyses provided to you:
- delivery_analysis: delivery dimensions (filler words, pace, eye contact, authority, repetition)
- content_analysis: content dimensions (opening, core message, problem, solution, structure, evidence, contradictions, closing)
- research_tips: evidence-based techniques
Session mode is provided in context; adapt language to that mode.

Write a coaching report using EXACTLY these sections (use the bold headings as shown):

**Opening & Core Message**
1-2 sentences. Was the opening strong? Is the core message clear? Quote the transcript.

**Content & Structure**
3-4 bullet points covering: logical flow, message clarity, evidence quality, closing strength.
Use direct quotes from the transcript. Be specific about what was missing or weak.

**Delivery**
3 bullet points: filler words (count + pattern), pace and confidence, eye contact and authority.
Use data from the coaching events (timestamps, counts).

**Top Fixes** (ordered by impact — fix these first)
1. [Most critical fix — specific and actionable]
2. [Second fix]
3. [Third fix]
4. [Fourth fix, if warranted]

**Evidence-Based Techniques**
The 2 research-backed techniques from the research agent. Include citations.

**Visual Presentation** (only if screen sharing was used or if content was dense)
2 bullet points describing how to visually improve the slides or visual aids.
Focus on: hierarchy, font size, contrast, or reducing clutter.

**What Worked**
1-2 sentences on genuine strengths. If nothing stood out, write exactly: "No standout strengths this session — clean slate to build from."

**IMAGE_PROMPTS**
Provide EXACTLY TWO highly detailed image generation prompts for Imagen 3.
These should represent the "ideal" version of the presenter's most critical slides (especially any slide that had clarity or mismatch issues).
Format:
IMAGE_PROMPT_1: [Description of a clean, professional slide for the 'Core Message' or 'Problem' section. Use 'high-quality minimalist presentation slide' as a base style.]
IMAGE_PROMPT_2: [Description of a clean, professional slide for the 'Solution' or 'Data' section.]

**SCORES**
Output these 5 quality scores as plain integers — NO brackets, NO extra text on the score lines:
SCORE_FILLER: [0–100; 100=zero fillers; 80=1-2 total; 60=3-5 total; 40=6-10 total; 20=11+; scale by session length]
SCORE_PACE: [0–100; 100=smooth/measured throughout; 80=minor inconsistency; 60=noticeable rushing or dragging; 40=frequent choppy bursts or long pauses; 20=erratic/unintelligible]
SCORE_EYE: [0–100; 100=no drops flagged; 80=1 brief drop; 60=2-3 drops; 40=4+ drops; 20=persistent camera avoidance]
SCORE_CLARITY: [0–100; 100=crisp logic and clear structure; 80=minor vagueness; 60=key claims unexplained; 40=contradictions present; 20=incoherent]
SCORE_VISUAL: [0–100; 100=no slide issues or no slides used; 80=minor clutter; 60=readability concerns; 40=significant hierarchy problems; 20=slides unusable]

Rules:
- Quote the transcript to support every claim
- Never use vague language like "consider improving" — say exactly what to do
- All seven prose sections plus IMAGE_PROMPTS and SCORES are required — do not truncate
- No intro sentence, no concluding sentence — go straight into sections""",
    output_key="final_report",
)

SESSION_SUMMARY_AGENT = LlmAgent(
    name="session_summary_agent",
    model=ANALYSIS_MODEL,
    instruction="""Given this session's coaching events, AI scores, and synthesis report,
output a compact JSON skill profile for this presenter. Output ONLY valid JSON, no markdown fences:

{
  "recurring_issues": ["filler_words", "pace"],
  "strengths": ["eye_contact"],
  "trajectory": "First session. Pace and filler words are primary targets.",
  "next_focus": "reduce_fillers",
  "session_score": 72
}

Rules:
- recurring_issues: event_types from coaching events (use underscore form: filler_words not filler)
- strengths: dimensions scoring >= 80 (filler_words / pace / eye_contact / clarity / visual_delivery)
- trajectory: 1 sentence comparing to prior sessions if visible in context, else note this is session 1
- next_focus: one of: reduce_fillers / improve_pacing / improve_eye_contact / improve_structure / improve_clarity
- session_score: integer 0-100 from the overall scorecard
- Output ONLY the JSON object — no commentary, no code fences, no explanation""",
    output_key="session_profile_json",
)

# ── ADK pipeline declarations ──────────────────────────────────────────────
# These objects make the multi-agent coordination structure explicit for
# architecture reviewers. main.py uses manual asyncio.gather to preserve
# real-time pipeline_step WebSocket events; POST_SESSION_PIPELINE is
# available for direct invocation in non-streaming contexts.

PARALLEL_ANALYSTS = ParallelAgent(
    name="parallel_analysts",
    sub_agents=[DELIVERY_ANALYST, CONTENT_ANALYST, RESEARCH_AGENT],
)

POST_SESSION_PIPELINE = SequentialAgent(
    name="post_session_pipeline",
    sub_agents=[PARALLEL_ANALYSTS, SYNTHESIS_AGENT, SESSION_SUMMARY_AGENT],
)
