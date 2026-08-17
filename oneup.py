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
# Scheduling a carousel makes OneUp fetch + validate every photo URL itself
# before it responds, so it needs much longer than a simple lookup call —
# especially for multi-photo zips. Retry once on a timeout/network blip
# before giving up, since these are often transient.
SCHEDULE_TIMEOUT = 45
SCHEDULE_RETRIES = 1

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
    try:
        r = requests.get(f"{BASE_URL}/listcategory", params={"apiKey": api_key}, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise OneUpError(f"Couldn't reach OneUp: {e}")
    return r.json()


def list_social_accounts(api_key):
    try:
        r = requests.get(
            f"{BASE_URL}/listsocialaccounts", params={"apiKey": api_key}, timeout=TIMEOUT
        )
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise OneUpError(f"Couldn't reach OneUp: {e}")
    return r.json()


def get_tiktok_trending_sound(api_key, social_account_id, country_code="US", date_range="7DAY", genre="ALL"):
    """Looks up currently-trending TikTok sounds via OneUp's own trending-sound
    charts, so the boss can pick one to attach to a post from inside Content
    Ops instead of going to OneUp/TikTok directly.

    date_range: one of "1DAY", "7DAY", "30DAY", "90DAY".
    Returns a list of dicts already reshaped to exactly the fields
    schedule_image_post(tiktok_music=...) expects, per OneUp's own
    field-mapping table -- plus "artist"/"thumbnail_url"/"duration" kept
    around for display in the picker UI.
    """
    try:
        r = requests.get(
            f"{BASE_URL}/gettiktoktrendingsound",
            params={
                "apiKey": api_key,
                "social_account_id": social_account_id,
                "country_code": country_code,
                "date_range": date_range,
                "genre": genre,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise OneUpError(f"Couldn't reach OneUp: {e}")
    try:
        payload = r.json()
    except ValueError:
        raise OneUpError(f"Non-JSON response from OneUp (HTTP {r.status_code}): {r.text[:300]}")
    if payload.get("error"):
        raise OneUpError(payload.get("message", "OneUp API error"))

    sounds = []
    for item in payload.get("data", []) or []:
        clip = item.get("trending_song_clip") or {}
        sounds.append(
            {
                "music_title": item.get("commercial_music_name") or "",
                "music_author": item.get("artist") or "",
                "music_sound_id": clip.get("song_clip_id") or "",
                "music_url": clip.get("preview_url") or item.get("preview_url") or "",
                "music_thumbnail": item.get("thumbnail_url") or "",
                # Display-only extras, not sent to scheduleimagepost:
                "duration": clip.get("duration") or item.get("duration"),
            }
        )
    return sounds


def schedule_image_post(
    api_key,
    category_id,
    social_network_ids,
    scheduled_date_time,
    content,
    image_urls,
    title=None,
    first_comment=None,
    tiktok_music=None,
):
    """
    scheduled_date_time: 'YYYY-MM-DD HH:MM' (24h) string, local to your OneUp
        account's configured timezone.
    image_urls: list of direct image URLs. Use normalize_drive_link() on any
        Google Drive links first. Multiple images = one carousel post.
    social_network_ids: list of OneUp social_network_id strings for the
        target accounts (see list_social_accounts / your Settings page).
    first_comment: optional text OneUp will auto-post as the first comment
        right after publishing -- per their docs this only actually takes
        effect on Facebook, Instagram, LinkedIn, and YouTube. Harmless to
        send for any other platform; OneUp just ignores it there.
    tiktok_music: optional dict with the trending-sound fields OneUp wants
        (music_title, music_sound_id, music_url, music_thumbnail,
        music_author -- see get_tiktok_trending_sound()). Sent as the
        `tiktok` platform-specific param; OneUp only reads it for TikTok
        posts, so it's harmless (just ignored) to send for any other
        platform, but callers should only pass it for TikTok orders.
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
    if first_comment:
        data["first_comment"] = first_comment
    if tiktok_music:
        # OneUp's Create Image Post docs nest trending-sound fields under a
        # "musicOption" object inside the "tiktok" param, but their own Get
        # TikTok Trending Sound doc's worked example for image posts sends
        # the same fields flat, directly under "tiktok" (no musicOption
        # wrapper). We send both shapes at once -- cheap, and guarantees
        # this works regardless of which doc page is the stale one.
        tiktok_payload = dict(tiktok_music)
        tiktok_payload["musicOption"] = dict(tiktok_music)
        data["tiktok"] = json.dumps(tiktok_payload)

    attempt = 0
    while True:
        try:
            r = requests.post(f"{BASE_URL}/scheduleimagepost", data=data, timeout=SCHEDULE_TIMEOUT)
            break
        except requests.exceptions.RequestException as e:
            if attempt >= SCHEDULE_RETRIES:
                raise OneUpError(
                    f"Couldn't reach OneUp after {attempt + 1} attempt(s) "
                    f"(timeout={SCHEDULE_TIMEOUT}s): {e}"
                )
            attempt += 1

    try:
        payload = r.json()
    except ValueError:
        raise OneUpError(f"Non-JSON response from OneUp (HTTP {r.status_code}): {r.text[:300]}")

    if r.status_code >= 400 or payload.get("error"):
        raise OneUpError(payload.get("message", f"OneUp API error (HTTP {r.status_code})"))

    return payload


def get_scheduled_posts(api_key, start=0):
    """Lists posts currently sitting in OneUp's scheduled queue (up to 50 at
    a time). scheduleimagepost's own response never includes the post_id it
    just created — this is how we look it up right afterward, by matching
    on content + scheduled time, so we can store it and later cancel/redo
    that exact post if its date gets dragged around on the Calendar."""
    try:
        r = requests.get(
            f"{BASE_URL}/getscheduledposts", params={"apiKey": api_key, "start": start}, timeout=TIMEOUT
        )
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise OneUpError(f"Couldn't reach OneUp: {e}")
    try:
        payload = r.json()
    except ValueError:
        raise OneUpError(f"Non-JSON response from OneUp (HTTP {r.status_code}): {r.text[:300]}")
    if payload.get("error"):
        raise OneUpError(payload.get("message", "OneUp API error"))
    return payload.get("data", [])


def find_scheduled_post_id(api_key, content, scheduled_date_time):
    """Best-effort lookup of the post_id OneUp assigned to a just-scheduled
    post — matches on exact caption text and scheduled minute (OneUp's
    date_time includes seconds we don't track). Only checks the first page
    of the queue; if a match isn't found there (very large queue, or OneUp
    hasn't indexed it yet), returns None rather than guessing wrong — the
    caller just won't be able to auto-resync this particular post's time
    later, which is a safe degrade, not a crash."""
    try:
        posts = get_scheduled_posts(api_key)
    except OneUpError:
        return None
    wanted_prefix = scheduled_date_time  # 'YYYY-MM-DD HH:MM'
    for p in posts:
        if p.get("content") == (content or "") and str(p.get("date_time", "")).startswith(wanted_prefix):
            return p.get("post_id")
    return None


def delete_scheduled_post(api_key, post_id):
    """Cancels a post still sitting in OneUp's scheduled queue. Used when a
    task's pub date gets dragged to a new day/time on the Calendar — OneUp
    has no "just change the time" endpoint for a post that's already
    scheduled, so the fix is delete-then-reschedule."""
    try:
        r = requests.post(
            f"{BASE_URL}/deletescheduledpost", data={"apiKey": api_key, "post_id": post_id}, timeout=TIMEOUT
        )
    except requests.exceptions.RequestException as e:
        raise OneUpError(f"Couldn't reach OneUp: {e}")
    try:
        payload = r.json()
    except ValueError:
        raise OneUpError(f"Non-JSON response from OneUp (HTTP {r.status_code}): {r.text[:300]}")
    if r.status_code >= 400 or payload.get("error"):
        raise OneUpError(payload.get("message", f"OneUp API error (HTTP {r.status_code})"))
    return payload
