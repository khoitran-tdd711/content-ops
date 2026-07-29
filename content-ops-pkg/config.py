import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def _normalize_db_url(url):
    # Some providers (Neon, old Heroku-style URLs) hand out "postgres://",
    # but SQLAlchemy 2.x/psycopg2 require the "postgresql://" scheme.
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = _normalize_db_url(
        os.environ.get(
            "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'social_ops.db')}"
        )
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}  # avoids stale-connection
    # errors after a free-tier Postgres instance auto-suspends and resumes.
    BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

    # Email: pick ONE of these two backends.
    # 1) Resend (HTTP API, recommended for free hosting since Render's free
    #    tier blocks outbound SMTP ports).
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
    RESEND_FROM = os.environ.get("RESEND_FROM", "").strip()

    # 2) Classic SMTP (works if you're on a paid host / your own server).
    SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
    SMTP_USER = os.environ.get("SMTP_USER", "").strip()
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
    SMTP_FROM = os.environ.get("SMTP_FROM", "orders@yourcompany.com").strip()

    ONEUP_API_KEY_DEFAULT = os.environ.get("ONEUP_API_KEY", "").strip()
