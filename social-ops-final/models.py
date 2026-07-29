from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

PLATFORMS = ["instagram", "linkedin", "facebook", "tiktok"]
CONTENT_TYPES = ["carousel", "single_image", "video", "reel", "story"]

# Status flow:
# ordered -> submitted -> (approved | to_modify)
#   to_modify -> ordered (producer reworks, resubmits -> submitted)
#   approved -> scheduled (boss scheduled it, either via OneUp API or manual OneUp handoff)
STATUSES = [
    "ordered",
    "submitted",
    "to_modify",
    "approved",
    "scheduled",
    "sent_manually",
    "failed",
]

STATUS_LABELS = {
    "ordered": "Ordered",
    "submitted": "Submitted for review",
    "to_modify": "Needs changes",
    "approved": "Approved",
    "scheduled": "Scheduled (auto-publish)",
    "sent_manually": "Sent to OneUp (manual upload)",
    "failed": "Publish failed",
}

STATUS_COLORS = {
    "ordered": "#9CA3AF",
    "submitted": "#F59E0B",
    "to_modify": "#EF4444",
    "approved": "#3B82F6",
    "scheduled": "#10B981",
    "sent_manually": "#8B5CF6",
    "failed": "#DC2626",
}


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
    platform = db.Column(db.String(30), nullable=False)
    content_type = db.Column(db.String(30), default="carousel")
    quantity = db.Column(db.Integer, default=1)
    title = db.Column(db.String(200))
    caption = db.Column(db.Text)
    due_date = db.Column(db.Date, nullable=False)

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
    def drive_link_list(self):
        if not self.drive_links:
            return []
        parts = []
        for chunk in self.drive_links.replace(",", "\n").split("\n"):
            chunk = chunk.strip()
            if chunk:
                parts.append(chunk)
        return parts
