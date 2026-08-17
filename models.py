import json
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

db = SQLAlchemy()

PLATFORMS = ["instagram", "linkedin", "facebook", "tiktok"]
CONTENT_TYPES = ["carousel", "single_image", "video", "reel", "story"]
LANGUAGES = [
    "English", "Spanish", "Italian", "French", "Dutch",
    "German", "Polish", "Portuguese", "Romanian", "Greek",
]

# Just for a nicer Settings page (the "Account handle per language" section).
LANGUAGE_FLAGS = {
    "English": "\U0001F1EC\U0001F1E7",
    "Spanish": "\U0001F1EA\U0001F1F8",
    "Italian": "\U0001F1EE\U0001F1F9",
    "French": "\U0001F1EB\U0001F1F7",
    "Dutch": "\U0001F1F3\U0001F1F1",
    "German": "\U0001F1E9\U0001F1EA",
    "Polish": "\U0001F1F5\U0001F1F1",
    "Portuguese": "\U0001F1F5\U0001F1F9",
    "Romanian": "\U0001F1F7\U0001F1F4",
    "Greek": "\U0001F1EC\U0001F1F7",
}

# Status flow:
# ordered -> submitted -> (ready | to_modify)
#   to_modify -> ordered (producer reworks, resubmits -> submitted)
#   ready -> scheduled (boss scheduled it, either via OneUp API or manual OneUp handoff)
#   scheduled -> published
# "in_production" is a boss-settable catch-all (from the Board) for anything
# being actively worked on outside the structured submit/review flow above.
STATUSES = [
    "ordered",
    "submitted",
    "to_modify",
    "in_production",
    "ready",
    "scheduled",
    "sent_manually",
    "failed",
    "published",
]

STATUS_LABELS = {
    "ordered": "Ordered",
    "submitted": "Submitted for review",
    "to_modify": "Needs changes",
    "in_production": "In Production",
    "ready": "Ready",
    "scheduled": "Scheduled",
    "sent_manually": "Scheduled (manual upload)",
    "failed": "Publish failed",
    "published": "Published",
}

STATUS_COLORS = {
    # Deliberately red so a freshly-created task pops out on the Board/
    # Calendar legend, sitting visually between "Scheduled" and "Published"
    # in the status list even though it's really the very first stage.
    "ordered": "#DC4C4C",
    "submitted": "#6F8EFF",
    "to_modify": "#DC4C4C",
    "in_production": "#6F8EFF",
    "ready": "#F5B942",
    "scheduled": "#134440",
    "sent_manually": "#134440",
    "failed": "#DC4C4C",
    "published": "#3FD27E",
}

# Text color to pair with each status badge/pill above — light-green
# backgrounds (ready/published) need dark Foundation text for contrast,
# per Akka's "Foundation on Highlight" pairing rule; everything else is
# dark enough for white text.
STATUS_TEXT_COLORS = {
    "ordered": "#FFFFFF",
    "submitted": "#FFFFFF",
    "to_modify": "#FFFFFF",
    "in_production": "#FFFFFF",
    "ready": "#0D302D",
    "scheduled": "#FFFFFF",
    "sent_manually": "#FFFFFF",
    "failed": "#FFFFFF",
    "published": "#0D302D",
}

# The statuses a boss can pick directly from the Board's status dropdown.
# The others (submitted / to_modify / sent_manually / failed) are set
# automatically as part of the producer submit/review/publish flow.
BOARD_STATUSES = ["ordered", "in_production", "ready", "scheduled", "published"]


def now():
    return datetime.now(timezone.utc)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    role = db.Column(db.String(20), nullable=False, default="producer")  # 'boss' or 'producer'
    password_hash = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, default=now)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def is_boss(self):
        return self.role == "boss"


class Setting(db.Model):
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text)

    @staticmethod
    def get(key, default=None):
        row = Setting.query.get(key)
        return row.value if row else default

    @staticmethod
    def set(key, value):
        row = Setting.query.get(key)
        if row:
            row.value = value
        else:
            row = Setting(key=key, value=value)
            db.session.add(row)
        db.session.commit()


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(30), nullable=True)  # nullable: bulk-imported rows may not have one yet
    language = db.Column(db.String(30))  # optional, e.g. for language-tracker imports
    content_type = db.Column(db.String(30), default="carousel")
    quantity = db.Column(db.Integer, default=1)
    title = db.Column(db.String(200))
    caption = db.Column(db.Text)
    # Auto-posted as the first comment on the published post (OneUp's own
    # scheduleimagepost API supports this natively) -- the common "keep
    # hashtags/CTAs out of the main caption" trick, especially on
    # Instagram. Only Facebook, Instagram, LinkedIn, and YouTube actually
    # support it on OneUp's end; harmless (just ignored) if set for a
    # platform that doesn't. Blank means no first comment gets sent.
    first_comment = db.Column(db.Text, nullable=True)
    due_date = db.Column(db.Date, nullable=True)  # blank until a pub date is assigned on the Board
    date_ordered = db.Column(db.Date)  # when it was placed/produced ("Folder Created" for tracker imports)

    producer_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    status = db.Column(db.String(20), default="ordered")
    drive_links = db.Column(db.Text)  # raw producer input, one link per line
    feedback_note = db.Column(db.Text)

    scheduled_at = db.Column(db.DateTime)
    oneup_response = db.Column(db.Text)
    # OneUp's own numeric ID for the post it created when this order was
    # scheduled — captured right after scheduling (their API doesn't return
    # it directly, see oneup.find_scheduled_post_id). Needed to cancel/redo
    # that exact post when its pub date gets dragged to a new time on the
    # Calendar, since OneUp has no "just change the time" endpoint.
    oneup_post_id = db.Column(db.Integer, nullable=True)
    # The boss's chosen photo selection/order from the Calendar's publish
    # popup — a JSON list of filenames (as shown in the preview strip), in
    # the order they should actually be sent. Null/blank means "no
    # customization yet, use every photo in its natural Drive order" (the
    # original behavior). Filenames no longer found in Drive are just
    # skipped rather than erroring, in case the source zip changes later.
    media_order = db.Column(db.Text, nullable=True)
    # The boss's chosen TikTok trending sound for this post (from OneUp's
    # gettiktoktrendingsound lookup) -- a small JSON object with the exact
    # fields OneUp's scheduleimagepost needs to attach it (title, sound_id,
    # url, thumbnail, author). Null/blank means "post with no added sound",
    # the original behavior. Only meaningful for platform == "tiktok";
    # harmless if set on any other platform (never read/sent for those).
    tiktok_sound = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now, onupdate=now)

    producer = db.relationship("User", foreign_keys=[producer_id])
    creator = db.relationship("User", foreign_keys=[created_by_id])

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def status_color(self):
        return STATUS_COLORS.get(self.status, "#6B7280")

    @property
    def status_text_color(self):
        return STATUS_TEXT_COLORS.get(self.status, "#FFFFFF")

    @property
    def drive_link_list(self):
        if not self.drive_links:
            return []
        parts = []
        for chunk in self.drive_links.replace(",", "\n").split("\n"):
            chunk = chunk.strip()
            if chunk:
                parts.append(chunk)
        return parts

    @property
    def tiktok_sound_dict(self):
        """Parsed tiktok_sound JSON, or None if unset/unparseable. Centralized
        here so every caller (JSON API, publish step) handles a corrupt/old
        value the same safe way instead of each doing their own try/except."""
        if not self.tiktok_sound:
            return None
        try:
            parsed = json.loads(self.tiktok_sound)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None


class OrderMedia(db.Model):
    """A photo manually uploaded straight into Content Ops for one task, on
    top of (not instead of) whatever's already pulled from its Drive
    link(s) — the Calendar publish popup's "+ Add photo" control. Stored
    directly in Postgres (bytes in the row) rather than on local disk,
    since Render's free web dyno's filesystem doesn't survive a restart or
    redeploy, and rather than a separate object-storage service, to keep
    the stack as simple/free as it already is. sort_order is just the
    natural "added in this order" position — the boss's own drag-to-
    reorder customization (Order.media_order) is layered on top of this,
    same as it is for Drive photos."""

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(100), default="image/jpeg")
    data = db.Column(db.LargeBinary, nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=now)

    order = db.relationship(
        "Order",
        backref=db.backref(
            "uploaded_media",
            order_by="OrderMedia.sort_order, OrderMedia.id",
            cascade="all, delete-orphan",
        ),
    )

    @property
    def media_key(self):
        """Stable identifier for this photo — embedded as the last path
        segment of its serving URL (see app.py's _uploaded_media_url),
        which is also exactly what the boss's saved photo order/selection
        (Order.media_order) matches against, alongside Drive photos'
        secure_filename. Prefixed with this row's own id so it can never
        collide with a Drive-derived key."""
        safe = secure_filename(self.filename) or "photo.jpg"
        return f"upload-{self.id}-{safe}"
