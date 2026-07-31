from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

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
    due_date = db.Column(db.Date, nullable=True)  # blank until a pub date is assigned on the Board
    date_ordered = db.Column(db.Date)  # when it was placed/produced ("Folder Created" for tracker imports)

    producer_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    status = db.Column(db.String(20), default="ordered")
    drive_links = db.Column(db.Text)  # raw producer input, one link per line
    feedback_note = db.Column(db.Text)

    scheduled_at = db.Column(db.DateTime)
    oneup_response = db.Column(db.Text)

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
