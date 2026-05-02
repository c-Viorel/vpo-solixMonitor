"""
Solix Performance Monitor – Flask application.

Routes
------
GET  /                  → dashboard (latest reading + today's stats)
GET  /history           → charts for past 24 h / 7 d / 30 d
GET  /login             → login form
POST /login             → authenticate admin
GET  /logout            → clear session
GET  /admin             → admin panel (requires login)
POST /admin/save-credentials  → save Anker credentials
POST /admin/save-password     → change admin password
POST /admin/trigger-collect   → run collection now

JSON API (used by JS)
---------------------
GET  /api/current       → latest reading as JSON
GET  /api/readings      → readings for ?hours=N (default 24)
GET  /api/energy        → daily energy for ?days=N (default 30)
"""
import logging
import os
from datetime import datetime, timezone
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_session import Session
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app():
    app = Flask(__name__)
    app.config.from_object("config.Config")

    # Server-side sessions (avoids storing secrets in the cookie).
    os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)
    Session(app)

    # Initialise DB tables.
    from db import init_db
    init_db()

    # Register Jinja2 helpers.
    app.jinja_env.globals["now"] = lambda: datetime.now(timezone.utc).isoformat()
    # Cache-buster: changes every server restart so browsers always fetch new JS/CSS.
    import time as _time
    app.jinja_env.globals["static_ver"] = str(int(_time.time()))

    # Start background scheduler (skipped if credentials not yet configured).
    from scheduler import start_scheduler
    start_scheduler(app)

    # -----------------------------------------------------------------------
    # Auth helpers
    # -----------------------------------------------------------------------

    def login_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("admin_logged_in"):
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return decorated

    def _get_admin_hash():
        from db import get_setting
        return get_setting("admin_password_hash")

    def _admin_configured():
        return bool(_get_admin_hash())

    # -----------------------------------------------------------------------
    # Public routes
    # -----------------------------------------------------------------------

    @app.route("/")
    def dashboard():
        from db import get_latest_reading, get_energy_daily, get_readings
        reading = get_latest_reading()
        energy_today = None
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        energy_rows = get_energy_daily(days=1)
        for row in energy_rows:
            if row.get("date") == today_str:
                energy_today = row
                break

        # Attach today's energy totals into the reading dict for initial render
        if reading and energy_today:
            reading["solar_today_kwh"]     = energy_today.get("solar_kwh")
            reading["discharge_today_kwh"] = energy_today.get("discharge_kwh")

        # Sparkline data: Power Flow last 24 hours (solar + AC out), downsampled to 1pt/5min
        # Sparkline data: Power Flow last 24 hours, downsampled to 1pt/5min
        raw_rows = get_readings(hours=24)
        seen_buckets: dict = {}
        for r in raw_rows:
            bucket = r["timestamp"][:15] + "0"  # floor to 10-min bucket
            if bucket not in seen_buckets:
                seen_buckets[bucket] = {
                    "t": r["timestamp"],
                    "solar":  r.get("solar_power_w"),
                    "ac_out": r.get("ac_out_power_w"),
                    "ac_in":  r.get("ac_in_power_w"),
                }
        sparkline = list(seen_buckets.values())

        from db import get_setting
        configured = bool(get_setting("anker_email_enc"))

        return render_template(
            "dashboard.html",
            reading=reading,
            energy_today=energy_today,
            sparkline=sparkline,
            configured=configured,
        )

    @app.route("/history")
    def history():
        return render_template("history.html")

    # -----------------------------------------------------------------------
    # Auth routes
    # -----------------------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("admin_logged_in"):
            return redirect(url_for("admin"))

        if request.method == "POST":
            password = request.form.get("password", "")

            if not _admin_configured():
                # First-time setup: the submitted password becomes the admin password.
                from db import set_setting
                set_setting("admin_password_hash", generate_password_hash(password))
                session["admin_logged_in"] = True
                session.permanent = True
                flash("Admin password set. Welcome!", "success")
                return redirect(url_for("admin"))

            stored_hash = _get_admin_hash()
            if stored_hash and check_password_hash(stored_hash, password):
                session["admin_logged_in"] = True
                session.permanent = True
                return redirect(url_for("admin"))

            flash("Invalid password.", "error")

        first_time = not _admin_configured()
        return render_template("login.html", first_time=first_time)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # -----------------------------------------------------------------------
    # Admin routes
    # -----------------------------------------------------------------------

    @app.route("/admin")
    @login_required
    def admin():
        from db import get_setting
        from config import Config

        # Show masked email (first 3 chars + *** + domain) for display.
        enc_email = get_setting("anker_email_enc")
        display_email = ""
        if enc_email:
            try:
                from crypto_utils import decrypt
                raw_email = decrypt(enc_email)
                parts = raw_email.split("@")
                display_email = parts[0][:3] + "***@" + parts[1] if len(parts) == 2 else "***"
            except Exception:
                display_email = "*** (saved)"

        country = get_setting("anker_country", "us")
        poll_interval = get_setting("poll_interval", str(Config.POLL_INTERVAL))

        from db import get_latest_reading
        last_reading = get_latest_reading()

        return render_template(
            "admin.html",
            display_email=display_email,
            country=country,
            poll_interval=poll_interval,
            last_reading=last_reading,
        )

    @app.route("/admin/save-credentials", methods=["POST"])
    @login_required
    def admin_save_credentials():
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()
        country = request.form.get("country", "us").strip().lower()

        if not email or not password:
            flash("Email and password are required.", "error")
            return redirect(url_for("admin"))

        if len(country) != 2:
            flash("Country must be a 2-letter ISO code (e.g. us, de, gb).", "error")
            return redirect(url_for("admin"))

        from db import set_setting
        from crypto_utils import encrypt

        set_setting("anker_email_enc", encrypt(email))
        set_setting("anker_password_enc", encrypt(password))
        set_setting("anker_country", country)

        flash("Credentials saved and encrypted.", "success")

        # Restart scheduler so it picks up new credentials immediately.
        from scheduler import stop_scheduler, start_scheduler
        stop_scheduler()
        start_scheduler(app)

        return redirect(url_for("admin"))

    @app.route("/admin/save-password", methods=["POST"])
    @login_required
    def admin_save_password():
        current = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        stored_hash = _get_admin_hash()
        if not check_password_hash(stored_hash, current):
            flash("Current password is incorrect.", "error")
            return redirect(url_for("admin"))

        if len(new_pw) < 8:
            flash("New password must be at least 8 characters.", "error")
            return redirect(url_for("admin"))

        if new_pw != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("admin"))

        from db import set_setting
        set_setting("admin_password_hash", generate_password_hash(new_pw))
        flash("Admin password updated.", "success")
        return redirect(url_for("admin"))

    @app.route("/admin/trigger-collect", methods=["POST"])
    @login_required
    def admin_trigger_collect():
        from db import get_setting
        enc_email = get_setting("anker_email_enc")
        if not enc_email:
            flash("No credentials saved. Please configure them first.", "error")
            return redirect(url_for("admin"))

        try:
            from scheduler import trigger_now
            trigger_now(app)
            flash("Data collection triggered successfully.", "success")
        except Exception as exc:
            flash(f"Collection failed: {exc}", "error")
        return redirect(url_for("admin"))

    @app.route("/admin/clear-data", methods=["POST"])
    @login_required
    def admin_clear_data():
        confirm = request.form.get("confirm", "")
        if confirm != "DELETE":
            flash('Type DELETE to confirm.', "error")
            return redirect(url_for("admin"))
        from db import get_db
        conn = get_db()
        with conn:
            conn.execute("DELETE FROM readings")
            conn.execute("DELETE FROM energy_daily")
        conn.close()
        flash("All historical data cleared.", "success")
        return redirect(url_for("admin"))

    @app.route("/api/test-credentials", methods=["POST"])
    @login_required
    def api_test_credentials():
        data = request.get_json(silent=True) or {}
        email   = data.get("email", "").strip()
        password = data.get("password", "").strip()
        country = data.get("country", "us").strip().lower()

        if not email or not password:
            return jsonify({"ok": False, "message": "Email and password are required."})
        if len(country) != 2:
            return jsonify({"ok": False, "message": "Country must be a 2-letter ISO code."})

        from collector import test_connection
        result = test_connection(email, password, country)
        return jsonify(result)

    # -----------------------------------------------------------------------
    # JSON API
    # -----------------------------------------------------------------------

    @app.route("/api/current")
    def api_current():
        from db import get_latest_reading, get_energy_daily
        reading = get_latest_reading() or {}
        reading.pop("raw_json", None)  # keep response light
        # Attach today's energy totals so the dashboard can display them
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for row in get_energy_daily(days=1):
            if row.get("date") == today_str:
                reading["solar_today_kwh"]     = row.get("solar_kwh")
                reading["discharge_today_kwh"] = row.get("discharge_kwh")
                break
        return jsonify(reading)

    @app.route("/api/readings")
    def api_readings():
        hours = min(int(request.args.get("hours", 24)), 720)  # cap at 30 days
        step  = int(request.args.get("step", 0))  # downsample: seconds per bucket (0=all)
        from db import get_readings
        rows = get_readings(hours=hours)
        # Strip raw JSON from each row.
        for r in rows:
            r.pop("raw_json", None)
        # Optional downsampling: keep last point per time bucket
        if step > 0:
            seen: dict = {}
            for r in rows:
                ts = r["timestamp"]  # e.g. "2026-04-28T13:34:21.219550+00:00"
                # Bucket key: truncate to minute (step<=60), 5-min, 30-min, or hour
                if step <= 60:
                    key = ts[:16]        # YYYY-MM-DDTHH:MM
                elif step <= 300:
                    minute = int(ts[14:16]) // 5 * 5
                    key = f"{ts[:13]}:{minute:02d}"
                elif step <= 1800:
                    minute = int(ts[14:16]) // 30 * 30
                    key = f"{ts[:13]}:{minute:02d}"
                else:
                    key = ts[:13]        # YYYY-MM-DDTHH
                seen[key] = r
            rows = list(seen.values())
        return jsonify(rows)

    @app.route("/api/energy")
    def api_energy():
        days = min(int(request.args.get("days", 30)), 365)
        from db import get_energy_daily
        return jsonify(get_energy_daily(days=days))

    @app.route("/api/debug-device")
    @login_required
    def api_debug_device():
        """Fetch raw device data from Anker API and return it as JSON (admin only)."""
        from db import get_setting
        from crypto_utils import decrypt
        enc_email = get_setting("anker_email_enc")
        enc_pass  = get_setting("anker_password_enc")
        country   = get_setting("anker_country", "us")
        if not enc_email:
            return jsonify({"error": "Credentials not saved yet."})
        try:
            from collector import _fetch
            import asyncio
            loop = asyncio.new_event_loop()
            raw = loop.run_until_complete(_fetch(decrypt(enc_email), decrypt(enc_pass), country))
            loop.close()
            # Strip circular 'raw' field from nested devices before returning
            devices = {sn: {k: v for k, v in d.items() if k != "raw"} for sn, d in raw.get("devices", {}).items()}
            return jsonify({"devices": devices, "site_count": len(raw.get("sites", {}))})
        except Exception as exc:
            return jsonify({"error": str(exc)})

    # -----------------------------------------------------------------------
    # Error handlers
    # -----------------------------------------------------------------------

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template("error.html", code=500, message="Internal server error."), 500

    return app


# ---------------------------------------------------------------------------
# Development entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    application = create_app()
    port = int(os.environ.get("PORT", 8080))
    application.run(debug=application.config.get("DEBUG", False), host="0.0.0.0", port=port)
