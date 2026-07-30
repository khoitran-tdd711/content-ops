"""
Thin wrapper around the Google Drive API — used ONLY when a task's Drive
link points to a .zip full of photos instead of a single image link.

This needs a Google service account key pasted into Settings. If none is
configured, zip links are simply left alone (plain single-image Drive links
still work exactly as before — no Drive API needed for those).
"""

import io
import json
import re
import zipfile

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

DRIVE_FILE_ID_PATTERNS = [
    re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)"),
    re.compile(r"drive\.google\.com/uc\?[^ ]*id=([a-zA-Z0-9_-]+)"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]+)"),
]

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
MAX_ZIP_BYTES = 60 * 1024 * 1024  # 60MB safety cap (Render free tier has limited RAM)


class DriveError(Exception):
    pass


def extract_file_id(url):
    for pattern in DRIVE_FILE_ID_PATTERNS:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


def get_service(service_account_json):
    try:
        info = json.loads(service_account_json)
    except ValueError:
        raise DriveError(
            "The Google service account key isn't valid JSON — paste the whole file's contents."
        )
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_file_metadata(service, file_id):
    try:
        return (
            service.files()
            .get(fileId=file_id, fields="id,name,mimeType,size", supportsAllDrives=True)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        raise DriveError(
            f"Couldn't read that Drive file (id {file_id}). Make sure it's shared with the "
            f"service account's email address. Details: {e}"
        )


def is_zip(meta):
    name = (meta.get("name") or "").lower()
    mime = meta.get("mimeType") or ""
    return name.endswith(".zip") or mime in ("application/zip", "application/x-zip-compressed")


def download_bytes(service, file_id, max_bytes=MAX_ZIP_BYTES):
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        try:
            _, done = downloader.next_chunk()
        except Exception as e:  # noqa: BLE001
            raise DriveError(f"Couldn't download that file from Drive: {e}")
        if buf.tell() > max_bytes:
            raise DriveError("That zip is too large to process (over 60MB).")
    return buf.getvalue()


def extract_images(zip_bytes):
    """Returns a list of (filename, bytes) for every photo found in the zip,
    skipping folders, hidden files, and macOS junk entries. Sorted by name
    so the carousel order matches what's in the zip."""
    images = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in zf.infolist():
                name = info.filename
                base = name.rsplit("/", 1)[-1]
                if info.is_dir() or not base or base.startswith(".") or "__MACOSX" in name:
                    continue
                if not base.lower().endswith(IMAGE_EXTENSIONS):
                    continue
                images.append((base, zf.read(info)))
    except zipfile.BadZipFile:
        raise DriveError("That file looked like a zip but couldn't be opened — it may be corrupted.")
    except DriveError:
        raise
    except Exception as e:  # noqa: BLE001 - never let a weird zip crash the whole publish request
        raise DriveError(f"Couldn't read the photos out of that zip: {e}")
    images.sort(key=lambda pair: pair[0])
    return images
