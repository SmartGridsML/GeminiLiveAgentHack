"""
PitchMirror — post-session multimodal asset generation.

Creates low-cost visual artifacts (max 2 images) for the scorecard:
1) Ideal delivery posture/camera setup
2) Improved slide layout concept
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
from typing import Any

from PIL import Image, ImageDraw

from backend.session_state import SessionState

logger = logging.getLogger(__name__)

DEFAULT_IMAGE_MODEL = os.getenv("PITCHMIRROR_IMAGE_MODEL", "imagen-4.0-fast-generate-001")
_IMAGE_PROMPT_LINE_RE = re.compile(r"^\s*IMAGE_PROMPT_(1|2)\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _clean_prompt_text(prompt: str) -> str:
    text = (prompt or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    return text


def extract_image_prompts(final_report: str) -> tuple[str, list[str]]:
    """
    Extract IMAGE_PROMPT_1/2 from synthesis output and return:
      (clean_report_without_prompt_section, [prompt1, prompt2?])
    """
    report = final_report or ""
    matches = sorted(
        _IMAGE_PROMPT_LINE_RE.findall(report),
        key=lambda x: int(x[0]),
    )
    prompts = [_clean_prompt_text(text) for _, text in matches if _clean_prompt_text(text)]

    cleaned = report
    if "**IMAGE_PROMPTS**" in cleaned:
        cleaned = cleaned.split("**IMAGE_PROMPTS**", 1)[0].rstrip()
    else:
        cleaned = _IMAGE_PROMPT_LINE_RE.sub("", cleaned).strip()

    return cleaned, prompts[:2]


def _extract_generated_image_bytes(response: Any) -> bytes | None:
    """
    Handle minor SDK response-shape differences across google-genai releases.
    """
    generated = getattr(response, "generated_images", None)
    if not generated:
        return None
    first = generated[0]
    image_obj = getattr(first, "image", None)
    if image_obj is None:
        return None

    for field in ("image_bytes", "data"):
        value = getattr(image_obj, field, None)
        if value:
            return bytes(value)

    # Some releases expose a raw bytes property directly.
    if isinstance(image_obj, (bytes, bytearray)):
        return bytes(image_obj)
    return None


def _render_fallback_card(title: str, subtitle: str) -> bytes:
    """
    Deterministic fallback when image APIs are unavailable.
    """
    img = Image.new("RGB", (1280, 720), color=(16, 24, 39))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((44, 44, 1236, 676), radius=28, outline=(59, 130, 246), width=4)
    draw.text((86, 96), title, fill=(240, 249, 255))
    draw.text((86, 172), subtitle, fill=(147, 197, 253))
    draw.text((86, 612), "PitchMirror demo fallback visual", fill=(148, 163, 184))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=84, optimize=True)
    return buf.getvalue()


def _compress_to_jpeg(raw_image_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(raw_image_bytes)) as img:
        img = img.convert("RGB")
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=84, optimize=True, progressive=True)
        return buf.getvalue()


def _sync_generate_image(prompt: str, model: str) -> bytes:
    # Imported lazily so local dev can run without model calls.
    from google import genai
    from google.genai import types as genai_types

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not set — skipping image generation")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_images(
        model=model,
        prompt=prompt,
        config=genai_types.GenerateImagesConfig(
            number_of_images=1,
            output_mime_type="image/png",
            aspect_ratio="16:9",
        ),
    )
    raw = _extract_generated_image_bytes(response)
    if not raw:
        raise RuntimeError("No generated image bytes returned")
    return _compress_to_jpeg(raw)


async def _generate_with_retry(
    prompt: str,
    *,
    model: str,
    timeout_s: int,
    retries: int,
) -> bytes | None:
    attempts = max(1, retries + 1)
    for idx in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_sync_generate_image, prompt, model),
                timeout=max(5, timeout_s),
            )
        except Exception as exc:
            logger.warning("Image generation attempt %d/%d failed: %s", idx, attempts, exc)
            if idx >= attempts:
                return None
            await asyncio.sleep(0.35 * idx)
    return None


def _prompt_delivery_setup(state: SessionState, final_report: str) -> str:
    focus = {
        "general": "confident public speaking posture",
        "presentation": "professional presentation delivery",
        "interview": "concise interview response delivery",
        "sales_demo": "trustworthy demo presenter posture",
        "pitch": "founder pitch confidence on camera",
    }.get(state.coach_mode, "confident speaking posture")
    return (
        "Create a clean coaching visual for webcam speaking setup. "
        f"Subject: {focus}. "
        "Show eye line at camera level, open posture, simple background, balanced lighting, and clear framing. "
        "Style: practical coaching board, minimalist, high readability, no text overlays. "
        f"Context cue: {final_report[:280]}"
    )


def _prompt_slide_layout(state: SessionState, final_report: str) -> str:
    mode_label = state.coach_mode.replace("_", " ")
    return (
        "Create an improved presentation slide concept as a visual layout mock. "
        f"Use case: {mode_label}. "
        "One headline, three supporting bullets, one visual/chart area, strong hierarchy, large readable typography. "
        "Style: modern, white background, blue accents, boardroom-ready. "
        "No logos, no watermarks, no tiny text. "
        f"Coaching context: {final_report[:280]}"
    )


async def generate_session_assets(
    state: SessionState,
    final_report: str,
    *,
    custom_prompts: list[str] | None = None,
    model: str = DEFAULT_IMAGE_MODEL,
    timeout_s: int = 24,
    retries: int = 1,
) -> list[dict]:
    """
    Returns at most two image assets with inline base64 payloads.
    """
    prompt_list = [p for p in (custom_prompts or []) if p and p.strip()]
    if len(prompt_list) >= 2:
        prompts = [
            (
                "report_visual_1",
                "Generated Visual 1",
                "Synthesis-driven visual concept.",
                prompt_list[0],
            ),
            (
                "report_visual_2",
                "Generated Visual 2",
                "Synthesis-driven visual concept.",
                prompt_list[1],
            ),
        ]
    else:
        prompts = [
            (
                "delivery_setup",
                "Ideal Delivery Setup",
                "Recommended camera framing and presenter posture.",
                _prompt_delivery_setup(state, final_report),
            ),
            (
                "slide_layout",
                "Improved Slide Layout",
                "Cleaner visual hierarchy for higher audience comprehension.",
                _prompt_slide_layout(state, final_report),
            ),
        ]

    assets: list[dict] = []
    for asset_id, title, description, prompt in prompts[:2]:
        img_bytes = await _generate_with_retry(
            prompt,
            model=model,
            timeout_s=timeout_s,
            retries=retries,
        )
        source = "imagen"
        if not img_bytes:
            source = "fallback"
            img_bytes = _render_fallback_card(title, description)

        assets.append(
            {
                "id": asset_id,
                "title": title,
                "description": description,
                "mime_type": "image/jpeg",
                "data_base64": base64.b64encode(img_bytes).decode("ascii"),
                "source": source,
            }
        )
    return assets
