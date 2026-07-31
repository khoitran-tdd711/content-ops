"""
Thin wrapper around the Google Drive API — used when a task's Drive link
points to a .zip or a folder full of photos instead of a single image link,
and to auto-suggest a matching Drive folder/zip by a task's title.

This needs a Google service account key pasted into Settings. If none is
configured, zip/folder links are simply left alone (plain single-image
Drive links still work exactly as before — no Drive API needed for those).
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
    re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([a-zA-Z0-9_-]+)"),
    re.compile(r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)"),
    re.compile(r"drive\.google\.com/uc\?[^ ]*id=([a-zA-Z0-9_-]+)"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]+)"),
]

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
MAX_ZIP_BYTES = 60 * 1024 * 1024  # 60MB safety cap (Render free tier has limited RAM)
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

# Zip filename suffix convention used inside each project's Drive folder:
# '<Title>.zip' for the base/English version, '<Title>-<code>.zip' for every
# other language. None means "no suffix" (the base language).
LANGUAGE_ZIP_SUFFIX = {
    "English": None,
    "Spanish": "es",
    "Italian": "it",
    "French": "fr",
    "Dutch": "nl",
    "German": "de",
    "Polish": "pl",
    "Portuguese": "pt",
    "Romanian": "ro",
    "Greek": "el",
}


class DriveError(Exception):
    pass


def extract_file_id(url):
    for pattern in DRIVE_FILE_ID_PATTERNS:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


def resolve_folder_id(value):
    """Accepts either a bare Drive folder ID or a full Drive folder URL and
    returns just the ID."""
    if not value:
        return None
    value = value.strip()
    if "drive.google.com" in value:
        return extract_file_id(value)
    return value


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


def is_folder(meta):
    return (meta.get("mimeType") or "") == FOLDER_MIME_TYPE


def is_image(meta):
    return (meta.get("mimeType") or "").startswith("image/")


def list_folder_images(service, folder_id):
    """Returns [(name, mimeType, id), ...] for every direct-child image file
    in a Drive folder (not recursive — matches a 'one folder per project,
    photos loose inside it' layout)."""
    try:
        resp = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="files(id,name,mimeType)",
                pageSize=200,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        raise DriveError(
            f"Couldn't list that Drive folder (id {folder_id}). Make sure it's shared with the "
            f"service account's email address. Details: {e}"
        )
    files = resp.get("files", [])
    images = [
        (f["name"], f["mimeType"], f["id"])
        for f in files
        if (f.get("name") or "").lower().endswith(IMAGE_EXTENSIONS)
    ]
    images.sort(key=lambda item: item[0])
    return images


def find_by_name(service, name, root_folder_id=None):
    """Searches Drive for a folder or zip file whose name matches `name`
    (typically an order's title) — used to suggest a Drive link for a task
    that doesn't have one yet. Tries an exact match first, then a looser
    'contains' search. Optionally scoped to the direct children of
    root_folder_id. Returns a dict (id, name, mimeType) or None."""
    if not name or not name.strip():
        return None

    def _search(q):
        try:
            resp = (
                service.files()
                .list(
                    q=q,
                    fields="files(id,name,mimeType)",
                    pageSize=5,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except Exception as e:  # noqa: BLE001
            raise DriveError(f"Drive search failed: {e}")
        return resp.get("files", [])

    escaped = name.strip().replace("\\", "\\\\").replace("'", "\\'")
    type_filter = (
        "(mimeType = 'application/vnd.google-apps.folder' "
        "or mimeType = 'application/zip' or mimeType = 'application/x-zip-compressed')"
    )
    scope = f" and '{root_folder_id}' in parents" if root_folder_id else ""
    base = f"trashed = false and {type_filter}{scope}"

    for q in (
        f"name = '{escaped}' and {base}",
        f"name = '{escaped}.zip' and {base}",
        f"name contains '{escaped}' and {base}",
    ):
        matches = _search(q)
        if matches:
            return matches[0]
    return None


def normalize_name(name):
    """Loosens name comparisons across trailing spaces, hyphens vs. spaces
    vs. underscores, and case — so 'Zara-and-why-it-succeed' and
    'Zara and why it succeed ' are treated as the same name."""
    return re.sub(r"[-_\s]+", " ", (name or "")).strip().lower()


def list_child_folders(service, parent_id):
    """Every direct sub-folder of parent_id (paginated) — used to match the
    mother project folder against each task's title."""
    folders, page_token = [], None
    while True:
        try:
            resp = (
                service.files()
                .list(
                    q=f"'{parent_id}' in parents and trashed = false and mimeType = '{FOLDER_MIME_TYPE}'",
                    fields="nextPageToken, files(id,name)",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except Exception as e:  # noqa: BLE001
            raise DriveError(f"Couldn't list folders under that Drive folder: {e}")
        folders.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return folders


def find_all_zips(service, folder_id, max_depth=4):
    """Recursively walks folder_id's sub-folders and returns every zip file
    found anywhere inside, as dicts with id/name. Depth-limited so a
    mis-shared huge Drive tree can't spin forever."""
    zips = []
    queue = [(folder_id, 0)]
    while queue:
        fid, depth = queue.pop(0)
        try:
            resp = (
                service.files()
                .list(
                    q=f"'{fid}' in parents and trashed = false",
                    fields="files(id,name,mimeType)",
                    pageSize=1000,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except Exception as e:  # noqa: BLE001
            raise DriveError(f"Couldn't list a Drive folder while searching for zips: {e}")
        for f in resp.get("files", []):
            if f.get("mimeType") == FOLDER_MIME_TYPE:
                if depth < max_depth:
                    queue.append((f["id"], depth + 1))
            elif is_zip(f):
                zips.append(f)
    return zips


# Every recognized "this zip is for language X" suffix code, e.g. 'nl', 'it'.
KNOWN_SUFFIX_CODES = {code for code in LANGUAGE_ZIP_SUFFIX.values() if code}


def _zip_suffix_code(stem):
    """Returns the trailing language-code word of a zip filename (without
    its extension), e.g. 'nl' from 'How-long-companies-stay-private-nl', or
    None if the name doesn't end in one of our known codes."""
    words = normalize_name(stem).split(" ")
    if words and words[-1] in KNOWN_SUFFIX_CODES:
        return words[-1]
    return None


def find_project_zip(zips, language):
    """Picks the right zip for `language` out of every zip found inside one
    project's Drive folder (see find_all_zips). The project folder is
    already matched to the task's title one level up, so the zip's own
    filename does NOT need to match the title at all — e.g. a project named
    'How long companies goes public' can contain a zip called
    'How-long-do-companies-stay-private-before-going-public.zip' and it's
    still correct, because it's the only zip in that project's folder.
    Only the trailing language-code suffix matters: '-nl', '-it', etc. (see
    LANGUAGE_ZIP_SUFFIX) — no suffix at all means the base/English version.
    If more than one zip could plausibly match, that's treated as
    ambiguous (returns None) rather than guessing wrong."""
    wanted_code = LANGUAGE_ZIP_SUFFIX.get(language) if language else None
    matches = []
    for f in zips:
        stem = f["name"]
        if stem.lower().endswith(".zip"):
            stem = stem[:-4]
        code = _zip_suffix_code(stem)
        if code == wanted_code:
            matches.append(f)
    if len(matches) == 1:
        return matches[0]
    return None


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
