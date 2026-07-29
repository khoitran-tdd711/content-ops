"""
Thin wrapper around the OneUp API (https://docs.oneupapp.io).

OneUp is a social scheduling/publishing platform. Rather than building and
maintaining our own Meta/TikTok/LinkedIn app-review integrations (each of
which requires a separate weeks-long business approval process per platform),
this app hands the actual publishing off to OneUp, which already has those
integrations approved. Our tool owns the ordering / production / approval
workflow; OneUp owns pushing pixels to the social networks.

If no API key is configured, every function here is simply unused and the
app falls back to a manual "open OneUp and upload it yourself" flow.
"""

import json
import re

import requests

BASE_URL = "https://www.oneupapp.io/api"
TIMEOUT = 20

DRIVE_FILE_ID_PATTERNS = [
    re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)"),
    re.compile(r"drive\.google\.com/uc\?[^ ]*id=([a-zA-Z0-9_-]+)"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]+)"),
]


class OneUpError(Exception):
    pass


def normalize_drive_link(url):
    """Convert a Google Drive share link into a direct-content URL.

    OneUp's API needs a publicly reachable URL that resolves straight to the
    image bytes. A normal "Share" link (.../file/d/FILE_ID/view?usp=sharing)
    is an HTML viewer page, not the image itself, so we rewrite it to the
    direct-download form. The Drive file must still be shared as
    "Anyone with the link can view" for this to work.
    """
    url = url.strip()
    if "drive.google.com" not in url:
        return url  # already a direct URL (e.g. hosted elsewhere)

    for pattern in DRIVE_FILE_ID_PATTERNS:
        m = pattern.search(url)
        if m:
            file_id = m.group(1)
            return f"https://drive.google.com/uc?export=view&id={file_id}"

    return url


def list_categories(api_key):
    r = requests.get(f"{BASE_URL}/listcategory", params={"apiKey": api_key}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def list_social_accounts(api_key):
    r = requests.get(
        f"{BASE_URL}/listsocialaccounts", params={"apiKey": api_key}, timeout=TIMEOUT
    )
    r.raise_for_status()
    return r.json()


def schedule_image_post(
    api_key,
    category_id,
    social_network_ids,
    scheduled_date_time,
    content,
    image_urls,
    title=None,
):
    """
    scheduled_date_time: 'YYYY-MM-DD HH:MM' (24h) string, local to your OneUp
        account's configured timezone.
    image_urls: list of direct image URLs. Use normalize_drive_link() on any
        Google Drive links first. Multiple images = one carousel post.
    social_network_ids: list of OneUp social_network_id strings for the
        target accounts (see list_social_accounts / your Settings page).
    """
    data = {
        "apiKey": api_key,
        "category_id": category_id,
        "social_network_id": json.dumps(social_network_ids),
        "scheduled_date_time": scheduled_date_time,
        "content": content or "",
        "image_url": "~~".join(image_urls),
    }
    if title:
        data["title"] = title

    r = requests.post(f"{BASE_URL}/scheduleimagepost", data=data, timeout=TIMEOUT)
    try:
        payload = r.json()
    except ValueError:
        raise OneUpError(f"Non-JSON response from OneUp (HTTP {r.status_code}): {r.text[:300]}")

    if r.status_code >= 400 or payload.get("error"):
        raise OneUpError(payload.get("message", f"OneUp API error (HTTP {r.status_code})"))

    return payload
