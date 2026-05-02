"""
Configuration management for Solix Performance Monitor.
Values are loaded from environment variables or the .env file.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


class Config:
    # Flask
    SECRET_KEY: str = os.environ.get("SECRET_KEY") or os.urandom(32).hex()
    DEBUG: bool = os.environ.get("DEBUG", "false").lower() == "true"
    SESSION_TYPE: str = "filesystem"
    SESSION_FILE_DIR: str = str(DATA_DIR / "flask_sessions")

    # Database
    DATABASE_PATH: str = str(DATA_DIR / "solix.db")

    # Polling
    # How often (seconds) to collect data via the REST API.
    # The Anker cloud updates at most once per minute, so 300 s (5 min) is enough.
    POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", 300))

    # Energy stats are rate-limited to 10–12 req/min, so query them less often.
    ENERGY_POLL_INTERVAL: int = int(os.environ.get("ENERGY_POLL_INTERVAL", 900))

    # File that holds the Fernet encryption key used to protect stored credentials.
    ENCRYPTION_KEY_FILE: str = str(DATA_DIR / ".enc_key")

    # Disable static file caching so JS/CSS changes are always picked up.
    SEND_FILE_MAX_AGE_DEFAULT: int = 0

    # Session cookie
    SESSION_COOKIE_HTTPONLY: bool = True
    SESSION_COOKIE_SAMESITE: str = "Lax"
    PERMANENT_SESSION_LIFETIME: int = 86400  # 1 day
