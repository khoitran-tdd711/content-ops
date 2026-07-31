"""Generates a draft Instagram/LinkedIn caption straight from a task's
actual carousel photos, using Claude's vision API (Anthropic Messages API).

Needs its own API key pasted into Settings — separate from the OneUp key,
since this calls a different service (console.anthropic.com). If no key is
configured, the "Generate caption" button in the Calendar just shows an
error asking to set one up; nothing else in the app depends on this.
"""

import base64
import io
import mimetypes

import requests
from PIL import Image

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-5"
TIMEOUT = 60
MAX_TOKENS = 600

SUPPORTED_MEDIA_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")

# Claude's vision API rejects any single image once its *base64-encoded*
# size passes 10MB. A carousel slide exported as a high-res PNG can easily
# blow past that on its own (base64 also inflates size by ~33% on top of
# the original file). Rather than let that surface as a cryptic API error,
# every photo gets resized/re-compressed to a safe size before it's ever
# sent — this also keeps token usage (and $ cost) down, since Claude gets
# no benefit from anything sharper than ~1568px on the long edge anyway.
MAX_EDGE_PX = 1568
JPEG_QUALITY = 85


class CaptionError(Exception):
    pass


def _prepare_image_bytes(data):
    """Resizes/re-compresses one photo to a safe size for Claude's vision
    API, always re-encoding as JPEG (simplest way to reliably shrink a
    file, and Claude doesn't need lossless quality just to read a slide).
    Falls back to the original bytes untouched if Pillow can't open it for
    some reason — the API call itself will then surface a clear enough
    error instead of this failing silently."""
    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        if max(img.size) > MAX_EDGE_PX:
            img.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - a weird/corrupt image must never crash caption generation
        return data, None


def _image_block(filename, data):
    resized, forced_media_type = _prepare_image_bytes(data)
    if forced_media_type:
        media_type = forced_media_type
    else:
        media_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
        if media_type not in SUPPORTED_MEDIA_TYPES:
            media_type = "image/jpeg"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(resized).decode("ascii"),
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
        "- Hook the reader in the very first sentence. Make them stop scrolling. Lead with a fact, "
        "a surprising figure, or a striking detail, but stay grounded in real facts and figures "
        "(use whatever numbers or details are actually visible in the photos, if any).\n"
        "- Do not just copy the text off the photos verbatim. Write an actual caption, not a "
        "robotic transcription of the slides.\n"
        "- Write like a person talking to a friend, not like a press release or an ad. Keep it "
        "natural and conversational, in plain, everyday sentences.\n"
        "- Do not use hyphens or dashes anywhere in the caption. Not to join words into compound "
        "terms (like 'game-changing' or 'data-driven'), and not as a dash for a dramatic pause. "
        "Use plain separate words, or split it into a new sentence, instead. Commas and periods "
        "only, no hyphens or dashes of any kind.\n"
        f"- {lang_line}\n"
        f"- End with a call-to-action carrying the same meaning as: \"Follow {handle} for more "
        f"insights about the new economy and start investing in startups.\" Phrase it naturally "
        f"in that language, but keep the handle exactly as written: {handle}\n"
        "- After the call-to-action, add at most 5 hashtags chosen for SEO relevance to the topic.\n"
        "- Output only the caption text itself. No preamble, no explanation, no surrounding "
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
