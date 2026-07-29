import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'social_ops.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

    SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
    SMTP_USER = os.environ.get("SMTP_USER", "").strip()
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
    SMTP_FROM = os.environ.get("SMTP_FROM", "orders@yourcompany.com").strip()

    ONEUP_API_KEY_DEFAULT = os.environ.get("ONEUP_API_KEY", "").strip()
