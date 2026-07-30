"""Parses the "Language Coverage & Publish Tracker" Google Sheet export
(the HTML export you get from File > Download > Web page (.html, zipped),
or a plain single-file HTML export) into a simple list of
per-project/per-language records.

Expected shape (one row per project, two sub-columns "Ready"/"Published"
per language): Project | Folder Created | (blank) | <Language> Ready |
<Language> Published | ... x10 languages ... | Missing | Export Status |
Published | Publish Status | Files Found.
"""

from datetime import date, datetime

from bs4 import BeautifulSoup

LANGUAGES = [
    "English", "Spanish", "Italian", "French", "Dutch",
    "German", "Polish", "Portuguese", "Romanian", "Greek",
]


def parse_tracker_html(html_content):
    """Returns a list of dicts:
    {"project": str, "folder_created": date|None,
     "languages": {lang: {"ready": bool, "published": bool}, ...}}
    """
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table")
    if table is None:
        return []

    results = []
    header_seen = False
    for row in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if not cells:
            continue
        # The sheet export prefixes every row with a spreadsheet row number
        # (or blank) as the first cell — drop it if present.
        body = cells[1:] if cells[0].isdigit() or cells[0] == "" else cells
        if not body:
            continue
        if body[0] == "Project":
            header_seen = True
            continue
        if not header_seen:
            continue

        project = body[0].strip() if body else ""
        if not project:
            continue

        folder_created_raw = body[1].strip() if len(body) > 1 else ""
        lang_vals = body[3:23]  # body[2] is a blank spacer column
        if len(lang_vals) < 20:
            continue

        folder_created = _parse_date(folder_created_raw)

        languages = {}
        for i, lang in enumerate(LANGUAGES):
            ready = lang_vals[i * 2] == "Ready"
            published = lang_vals[i * 2 + 1] == "Yes"
            languages[lang] = {"ready": ready, "published": published}

        results.append(
            {"project": project, "folder_created": folder_created, "languages": languages}
        )

    return results


def _parse_date(raw):
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None
