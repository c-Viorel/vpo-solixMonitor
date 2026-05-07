"""
Background scheduler – wraps APScheduler to run periodic data collection
inside the Flask process (Passenger keeps the process alive on Hostinger).

A cron.py script is also provided as a separate entry point for use with
Hostinger's built-in cron job manager.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_scheduler: Optional[BackgroundScheduler] = None
_mqtt_listener = None   # MqttListener instance (or None)


# ---------------------------------------------------------------------------
# MQTT listener lifecycle
# ---------------------------------------------------------------------------

def _start_mqtt(app) -> None:
    """Start the MQTT listener thread if credentials are configured."""
    global _mqtt_listener
    if _mqtt_listener is not None:
        return  # already running

    with app.app_context():
        from db import get_setting
        from crypto_utils import decrypt

        enc_email    = get_setting("anker_email_enc")
        enc_password = get_setting("anker_password_enc")
        country      = get_setting("anker_country", "us")
        if not enc_email or not enc_password:
            return

        try:
            email    = decrypt(enc_email)
            password = decrypt(enc_password)
        except Exception as exc:
            logger.error("MQTT: failed to decrypt credentials: %s", exc)
            return

        def _on_reading(reading: dict) -> None:
            """Save MQTT-delivered reading to DB inside app context."""
            with app.app_context():
                from db import save_reading, upsert_energy_daily
                try:
                    save_reading(reading)
                    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    logger.debug(
                        "MQTT reading saved – SOC: %s%% | In: %s W | Out: %s W",
                        reading.get("battery_soc"),
                        reading.get("total_in_w"),
                        reading.get("total_out_w"),
                    )
                except Exception as exc:
                    logger.error("MQTT: failed to save reading: %s", exc)

        from collector import MqttListener
        _mqtt_listener = MqttListener(email, password, country, _on_reading)
        _mqtt_listener.start()
        logger.info("MQTT listener started.")


def _stop_mqtt() -> None:
    global _mqtt_listener
    if _mqtt_listener is not None:
        try:
            _mqtt_listener.stop()
        except Exception as exc:
            logger.warning("MQTT stop error: %s", exc)
        _mqtt_listener = None
        logger.info("MQTT listener stopped.")


def _collect_job(app) -> None:
    """Job function called by APScheduler."""
    with app.app_context():
        from db import get_setting, save_reading, upsert_energy_daily
        from collector import run_collection
        from crypto_utils import decrypt

        enc_email = get_setting("anker_email_enc")
        enc_password = get_setting("anker_password_enc")
        country = get_setting("anker_country", "us")

        if not enc_email or not enc_password:
            logger.debug("Anker credentials not configured – skipping collection.")
            return

        try:
            email = decrypt(enc_email)
            password = decrypt(enc_password)
        except Exception as exc:
            logger.error("Failed to decrypt credentials: %s", exc)
            return

        try:
            reading, api_energy = run_collection(email, password, country)
            save_reading(reading)
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            # For standalone PPS devices (C1000X) the Anker REST API never
            # populates site energy stats, so all api_energy values are 0.
            # Compute energy by integrating today's stored power readings instead.
            # If the API does return non-zero values (future-proofing), prefer them.
            from db import compute_energy_from_readings
            computed = compute_energy_from_readings(date_str)
            energy = {
                k: (api_energy.get(k) or 0) if (api_energy.get(k) or 0) > 0 else computed.get(k, 0)
                for k in ("solar_kwh", "charge_kwh", "discharge_kwh", "usage_kwh")
            }

            upsert_energy_daily(date_str, energy)
            logger.info(
                "Collection OK – SOC: %s%% | In: %s W | Out: %s W | solar=%.3f charge=%.3f discharge=%.3f kWh",
                reading.get("battery_soc"),
                reading.get("total_in_w"),
                reading.get("total_out_w"),
                energy["solar_kwh"],
                energy["charge_kwh"],
                energy["discharge_kwh"],
            )
        except Exception as exc:
            logger.error("Collection failed: %s", exc, exc_info=True)


def _purge_job(app) -> None:
    """Weekly: delete readings older than 90 days and VACUUM the DB."""
    with app.app_context():
        try:
            from db import purge_old_readings
            deleted = purge_old_readings()
            logger.info("DB purge: removed %d old readings and VACUUMed.", deleted)
        except Exception as exc:
            logger.error("DB purge failed: %s", exc)


def _thumbgen_job(app) -> None:
    """Generate thumbnail sprites for new recording segments (runs every 5 min)."""
    cameras_enabled = os.environ.get("CAMERAS_ENABLED", "false").lower() == "true"
    if not cameras_enabled:
        return
    try:
        from thumbnail_gen import run_all
        run_all()
    except Exception as exc:
        logger.error("Thumbnail generation failed: %s", exc)


def start_scheduler(app) -> None:
    """Start the APScheduler BackgroundScheduler + MQTT listener."""
    global _scheduler

    from config import Config

    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
    _scheduler.add_job(
        func=_collect_job,
        args=[app],
        trigger=IntervalTrigger(seconds=Config.POLL_INTERVAL),
        id="collect_data",
        name="Anker API data collection",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        func=_purge_job,
        args=[app],
        trigger=IntervalTrigger(weeks=1),
        id="purge_old_data",
        name="Purge old readings",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        func=_thumbgen_job,
        args=[app],
        trigger=IntervalTrigger(minutes=5),
        id="thumbnail_gen",
        name="DVR thumbnail sprite generation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Scheduler started (interval: %d s).", Config.POLL_INTERVAL)

    # Start MQTT listener for real-time SOC/power values (non-blocking thread).
    _start_mqtt(app)


def stop_scheduler() -> None:
    global _scheduler
    _stop_mqtt()
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")


def trigger_now(app) -> None:
    """Trigger an immediate collection (called from admin panel)."""
    _collect_job(app)
    # Restart MQTT if credentials may have just changed.
    _stop_mqtt()
    _start_mqtt(app)
