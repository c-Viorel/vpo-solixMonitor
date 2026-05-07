"""
Solix Performance Monitor – Flask application.

Routes
------
GET  /                  → dashboard (latest reading + today's stats)
GET  /history           → charts for past 24 h / 7 d / 30 d
GET  /cameras           → live camera feeds (requires login + mediamtx)
GET  /playback          → DVR playback — 24h timeline with thumbnail scrubbing
GET  /playback/<cam>    → DVR playback for specific camera
GET  /login             → login form
POST /login             → authenticate admin
GET  /logout            → clear session
GET  /admin             → admin panel (requires login)
POST /admin/save-credentials  → save Anker credentials
POST /admin/dvr-settings      → save DVR recording quality + retention
GET  /api/dvr-storage         → DVR disk usage per camera
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

        dvr_retention = get_setting("dvr_retention_hours", "24")
        dvr_quality   = get_setting("dvr_record_quality", "sd")

        return render_template(
            "admin.html",
            display_email=display_email,
            country=country,
            poll_interval=poll_interval,
            last_reading=last_reading,
            dvr_retention=dvr_retention,
            dvr_quality=dvr_quality,
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

    @app.route("/admin/dvr-settings", methods=["POST"])
    @login_required
    def admin_dvr_settings():
        from db import set_setting
        retention = request.form.get("dvr_retention", "24")
        quality   = request.form.get("dvr_quality", "sd")
        if retention not in ("12", "24", "48", "72"):
            retention = "24"
        if quality not in ("sd", "hd"):
            quality = "sd"
        set_setting("dvr_retention_hours", retention)
        set_setting("dvr_record_quality", quality)
        flash("DVR settings saved.", "success")
        return redirect(url_for("admin"))

    @app.route("/api/dvr-storage")
    @login_required
    def api_dvr_storage():
        import os as _os
        recordings_dir = _os.environ.get("RECORDINGS_DIR", "/recordings")
        cameras = ["camera1lo", "camera2lo"]
        result = {"cameras": {}, "total_bytes": 0, "total_mb": 0}
        for cam in cameras:
            cam_path = _os.path.join(recordings_dir, cam)
            size_bytes = 0
            file_count = 0
            if _os.path.isdir(cam_path):
                for fname in _os.listdir(cam_path):
                    fpath = _os.path.join(cam_path, fname)
                    try:
                        size_bytes += _os.path.getsize(fpath)
                        if fname.endswith(".mp4"):
                            file_count += 1
                    except OSError:
                        pass
            result["cameras"][cam] = {
                "bytes": size_bytes,
                "mb": round(size_bytes / 1_048_576, 1),
                "segments": file_count,
            }
            result["total_bytes"] += size_bytes
        result["total_mb"] = round(result["total_bytes"] / 1_048_576, 1)
        result["total_gb"] = round(result["total_bytes"] / 1_073_741_824, 2)
        return jsonify(result)

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

    @app.route("/api/energy/lifetime")
    def api_energy_lifetime():
        from db import get_lifetime_energy
        return jsonify(get_lifetime_energy())

    @app.route("/playback")
    @app.route("/playback/<cam>")
    @login_required
    def playback(cam="camera1"):
        """DVR playback page — 24h timeline with thumbnail scrubbing."""
        enabled = os.environ.get("CAMERAS_ENABLED", "false").lower() == "true"
        cam1_name = os.environ.get("CAMERA1_NAME", "Camera 1")
        cam2_name = os.environ.get("CAMERA2_NAME", "Camera 2")
        from db import get_setting
        dvr_quality = get_setting("dvr_record_quality", "sd")
        return render_template(
            "playback.html",
            cameras_enabled=enabled,
            cam=cam,
            cam1_name=cam1_name,
            cam2_name=cam2_name,
            dvr_quality=dvr_quality,
        )

    @app.route("/recordings/list/<cam>")
    @login_required
    def recordings_list(cam):
        """List recording segments by scanning the filesystem directly."""
        import re, os as _os
        from datetime import timezone as _tz, datetime as _dt
        allowed = {"camera1", "camera2"}
        if cam not in allowed:
            return jsonify({"error": "invalid camera"}), 400
        from db import get_setting
        quality = get_setting("dvr_record_quality", "sd")
        mtx_cam = cam if quality == "hd" else cam + "lo"
        recordings_dir = _os.environ.get("RECORDINGS_DIR", "/recordings")
        cam_dir = _os.path.join(recordings_dir, mtx_cam)
        # Fallback to SD directory if HD directory doesn't exist yet
        if not _os.path.isdir(cam_dir):
            mtx_cam = cam + "lo"
            cam_dir = _os.path.join(recordings_dir, mtx_cam)
        if not _os.path.isdir(cam_dir):
            return jsonify([])

        # Parse filenames: 2026-05-07_21-18-41-765667.mp4
        pattern = re.compile(r'^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})-\d+\.mp4$')
        entries = []
        for fname in sorted(_os.listdir(cam_dir)):
            m = pattern.match(fname)
            if not m:
                continue
            fpath = _os.path.join(cam_dir, fname)
            try:
                fsize = _os.path.getsize(fpath)
            except OSError:
                continue
            # Skip tiny in-progress segments (< 500 KB)
            if fsize < 500_000:
                continue
            date_str, hh, mm, ss = m.group(1), m.group(2), m.group(3), m.group(4)
            start_iso = f"{date_str}T{hh}:{mm}:{ss}Z"
            entries.append({"start_iso": start_iso, "fname": fname, "fsize": fsize})

        # Calculate duration: diff to next segment start, or 300s fallback
        segments = []
        for i, e in enumerate(entries):
            if i + 1 < len(entries):
                t0 = _dt.fromisoformat(e["start_iso"].replace("Z", "+00:00"))
                t1 = _dt.fromisoformat(entries[i+1]["start_iso"].replace("Z", "+00:00"))
                dur = (t1 - t0).total_seconds()
            else:
                dur = 300.0  # last (possibly in-progress) segment
            seg = {
                "start":     e["start_iso"],
                "duration":  dur,
                "proxy_url": f"/recordings/get/{cam}?file={e['fname']}",
                "sprite_url": f"/recordings/sprite/{cam}/{e['start_iso'][:19].replace(':', '-')}",
            }
            segments.append(seg)
        return jsonify(segments), 200, {"Cache-Control": "no-store"}

    @app.route("/recordings/get/<cam>")
    @login_required
    def recordings_get(cam):
        """Serve an MP4 recording file directly from disk."""
        import re, os as _os
        from flask import send_file
        allowed = {"camera1", "camera2"}
        if cam not in allowed:
            return jsonify({"error": "invalid camera"}), 400
        fname = request.args.get("file", "")
        if not re.match(r'^[\d_\-]+\.mp4$', fname):
            return jsonify({"error": "invalid filename"}), 400
        from db import get_setting
        quality = get_setting("dvr_record_quality", "sd")
        mtx_cam = cam if quality == "hd" else cam + "lo"
        recordings_dir = _os.environ.get("RECORDINGS_DIR", "/recordings")
        fpath = _os.path.join(recordings_dir, mtx_cam, fname)
        # Fallback to SD directory
        if not _os.path.isfile(fpath):
            fpath = _os.path.join(recordings_dir, cam + "lo", fname)
        if not _os.path.isfile(fpath):
            return jsonify({"error": "not found"}), 404
        return send_file(fpath, mimetype="video/mp4", conditional=True)

    @app.route("/recordings/sprite/<cam>/<ts>")
    @login_required
    def recordings_sprite(cam, ts):
        """Serve pre-generated thumbnail sprite for a recording segment."""
        import re, glob as _glob, json as _json
        from flask import send_file
        allowed = {"camera1", "camera2"}
        if cam not in allowed or not re.match(r'^[\d\-_T:Z]+$', ts):
            return jsonify({"error": "invalid"}), 400
        recordings_dir = os.environ.get("RECORDINGS_DIR", "/recordings")
        from db import get_setting as _get_setting
        quality = _get_setting("dvr_record_quality", "sd")
        mtx_cam = cam if quality == "hd" else cam + "lo"
        cam_dir = os.path.join(recordings_dir, mtx_cam)
        if not os.path.isdir(cam_dir):
            cam_dir = os.path.join(recordings_dir, cam + "lo")
        if not os.path.isdir(cam_dir):
            return jsonify({"error": "no recordings"}), 404
        # ts: "2026-05-07T21-18-41" → normalize and glob (microseconds in filename)
        ts_norm = ts.replace("T", "_").replace(":", "-")
        matches = sorted(_glob.glob(os.path.join(cam_dir, ts_norm + "*.sprite.jpg")))
        if not matches:
            return jsonify({"error": "sprite not ready"}), 404
        sprite = matches[0]
        meta = sprite.replace(".sprite.jpg", ".sprite.json")
        resp = send_file(sprite, mimetype="image/jpeg")
        if os.path.exists(meta):
            with open(meta) as f:
                resp.headers["X-Sprite-Meta"] = _json.dumps(_json.load(f))
        return resp

    @app.route("/recordings/sprite-meta/<cam>/<ts>")
    @login_required
    def recordings_sprite_meta(cam, ts):
        """Return sprite metadata JSON."""
        import re, json as _json, glob as _glob
        allowed = {"camera1", "camera2"}
        if cam not in allowed or not re.match(r'^[\d\-_T:Z]+$', ts):
            return jsonify({"error": "invalid"}), 400
        recordings_dir = os.environ.get("RECORDINGS_DIR", "/recordings")
        mtx_cam = cam + "lo"
        ts_norm = ts.replace("T", "_").replace(":", "-")
        matches = sorted(_glob.glob(os.path.join(recordings_dir, mtx_cam, ts_norm + "*.sprite.json")))
        if not matches:
            return jsonify({"error": "not ready"}), 404
        with open(matches[0]) as f:
            return jsonify(_json.load(f))

    @app.route("/cameras")
    @login_required
    def cameras():
        """Live camera feeds via HLS (requires mediamtx service running)."""
        enabled = os.environ.get("CAMERAS_ENABLED", "false").lower() == "true"
        cam1_name = os.environ.get("CAMERA1_NAME", "Camera 1")
        cam2_name = os.environ.get("CAMERA2_NAME", "Camera 2")
        hls_pass = os.environ.get("HLS_READ_PASS", "")
        return render_template(
            "cameras.html",
            cameras_enabled=enabled,
            cam1_name=cam1_name,
            cam2_name=cam2_name,
            hls_pass=hls_pass,
        )

    @app.route("/webhook/deploy", methods=["POST"])
    def webhook_deploy():
        """GitHub Actions calls this after every push to trigger an immediate redeploy."""
        import hmac, subprocess, threading
        token = os.environ.get("DEPLOY_WEBHOOK_TOKEN", "")
        if not token:
            return jsonify({"error": "Webhook not configured"}), 503
        provided = request.headers.get("X-Deploy-Token", "")
        if not hmac.compare_digest(provided, token):
            return jsonify({"error": "Unauthorized"}), 401
        deploy_script = os.path.join(os.path.dirname(__file__), "deploy.sh")
        if not os.path.exists(deploy_script):
            return jsonify({"error": "deploy.sh not found"}), 500
        def run_deploy():
            subprocess.Popen(
                ["bash", deploy_script],
                stdout=open("/tmp/deploy_webhook.log", "w"),
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
        threading.Thread(target=run_deploy, daemon=True).start()
        return jsonify({"status": "deploy triggered"}), 202

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
