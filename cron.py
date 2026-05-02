"""
Standalone data collection script – designed to be called by a cron job.

Usage (Hostinger hPanel → Cron Jobs):
    /path/to/python /path/to/solix_performance/cron.py

This script does the same as one scheduler tick but without a running Flask
process, so it works even when the web app is sleeping on shared hosting.
"""
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the app directory is on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    from db import init_db, get_setting, save_reading, upsert_energy_daily
    from collector import run_collection
    from crypto_utils import decrypt

    init_db()

    enc_email = get_setting("anker_email_enc")
    enc_password = get_setting("anker_password_enc")
    country = get_setting("anker_country", "us")

    if not enc_email or not enc_password:
        logger.error(
            "Anker credentials not configured. "
            "Log in to the admin panel and save your credentials first."
        )
        sys.exit(1)

    try:
        email = decrypt(enc_email)
        password = decrypt(enc_password)
    except Exception as exc:
        logger.error("Failed to decrypt credentials: %s", exc)
        sys.exit(1)

    logger.info("Starting data collection for %s...", email[:3] + "***")

    try:
        reading, energy = run_collection(email, password, country)
        save_reading(reading)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        upsert_energy_daily(date_str, energy)
        logger.info(
            "Collection OK – SOC: %s%% | In: %s W | Out: %s W",
            reading.get("battery_soc"),
            reading.get("total_in_w"),
            reading.get("total_out_w"),
        )
    except Exception as exc:
        logger.error("Collection failed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
