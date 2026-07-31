"""Generates a draft Instagram/LinkedIn caption straight from a task's
actual carousel photos, using Claude's vision API (Anthropic Messages API).

Needs its own API key pasted into Settings — separate from the OneUp key,
since this calls a different service (console.anthropic.com). If no key is
configured, the "Generate caption" button in the Calendar just shows an
error asking to set one up; nothing else in the app depends on this.
"""

import base64
import mimetypes

import requests

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-5"
TIMEOUT = 60
MAX_TOKENS = 600

SUPPORTED_MEDIA_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")


class CaptionError(Exception):
    pass


def _image_block(filename, data):
    media_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
    if media_type not in SUPPORTED_MEDIA_TYPES:
        media_type = "image/jpeg"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def build_prompt(language, handle):
    """Encodes Akka's caption rules. `handle` is the exact @handle to use
    in the call-to-action for this task's language (e.g. '@akka_france')."""
    lang_line = f"Write the entire caption in {language}." if language else "Write the entire caption in English."
    return (
        "You're writing an Instagram/LinkedIn caption for Akka, an investment-education brand, "
        "based on the attached carousel photos for this specific post.\n\n"
        "Rules to follow exactly:\n"
        "- Hook the reader in the very first sentence — make them stop scrolling. Lead with a "
        "fact, a surprising figure, or a striking detail, but stay grounded in real facts and "
        "figures (use whatever numbers or details are actually visible in the photos, if any).\n"
        "- Do not just copy the text off the photos verbatim — write an actual caption, not a "
        "robotic transcription of the slides.\n"
        f"- {lang_line}\n"
        f"- End with a call-to-action carrying the same meaning as: \"Follow {handle} for more "
        f"insights about the new economy and start investing in startups.\" — phrased naturally "
        f"in that language, but keep the handle exactly as written: {handle}\n"
        "- After the call-to-action, add at most 5 hashtags chosen for SEO relevance to the topic.\n"
        "- Output only the caption text itself — no preamble, no explanation, no surrounding "
        "quotation marks."
    )


def generate_caption(api_key, images, language, handle):
    """images: a list of (filename, bytes) tuples — the actual carousel
    photos for this task. Returns the generated caption text."""
    if not api_key:
        raise CaptionError("No Anthropic API key configured — add one in Settings first.")
    if not images:
        raise CaptionError("No photos found to generate a caption from.")

    content = [_image_block(name, data) for name, data in images]
    content.append({"type": "text", "text": build_prompt(language, handle)})

    try:
        resp = requests.post(
            API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": MAX_TOKENS,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise CaptionError(f"Couldn't reach Claude: {e}")

    if resp.status_code != 200:
        raise CaptionError(f"Claude API error ({resp.status_code}): {resp.text[:300]}")

    try:
        data = resp.json()
        text = "".join(block["text"] for block in data["content"] if block.get("type") == "text")
    except (ValueError, KeyError, TypeError):
        raise CaptionError("Unexpected response from Claude.")

    text = text.strip()
    if not text:
        raise CaptionError("Claude returned an empty caption.")
    return text
