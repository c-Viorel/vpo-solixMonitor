"""
Database layer – SQLite via Python's built-in sqlite3.

Tables
------
readings       – per-poll snapshot of power metrics
energy_daily   – daily energy totals (kWh)
settings       – key/value store (credentials, admin password hash, etc.)
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from config import Config


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(Config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # better concurrent read performance
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create tables if they don't exist, then run housekeeping."""
    conn = get_db()
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS readings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       DATETIME DEFAULT CURRENT_TIMESTAMP,

                -- Battery
                battery_soc     REAL,         -- % 0-100
                battery_wh      REAL,         -- remaining Wh
                battery_temp    REAL,         -- °C

                -- Input power (W)
                solar_power_w   REAL,
                ac_in_power_w   REAL,
                dc_in_power_w   REAL,
                total_in_w      REAL,

                -- Output power (W)
                ac_out_power_w  REAL,
                dc_out_power_w  REAL,
                total_out_w     REAL,

                -- Status
                charging_status TEXT,         -- charging / discharging / standby / full
                device_online   INTEGER,      -- 1 = online
                data_source     TEXT,         -- 'api' or 'mqtt'
                raw_json        TEXT
            );

            CREATE TABLE IF NOT EXISTS energy_daily (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                date            TEXT UNIQUE,   -- YYYY-MM-DD
                solar_kwh       REAL DEFAULT 0,
                charge_kwh      REAL DEFAULT 0,
                discharge_kwh   REAL DEFAULT 0,
                usage_kwh       REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS settings (
                key     TEXT PRIMARY KEY,
                value   TEXT
            );

            CREATE TABLE IF NOT EXISTS motion_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                camera        TEXT NOT NULL,
                segment_start TEXT NOT NULL,
                offset_sec    REAL NOT NULL,
                motion_type   TEXT NOT NULL      -- 'motion' or 'person'
            );
            CREATE INDEX IF NOT EXISTS idx_motion_cam_seg
                ON motion_events (camera, segment_start);

            -- Tracks which segments have been analyzed (even if no motion found)
            CREATE TABLE IF NOT EXISTS motion_analyzed (
                camera        TEXT NOT NULL,
                segment_start TEXT NOT NULL,
                PRIMARY KEY (camera, segment_start)
            );

            CREATE INDEX IF NOT EXISTS idx_readings_ts
                ON readings (timestamp);
        """)

        # One-time migration: wipe raw_json column (was filling up disk)
        conn.execute("UPDATE readings SET raw_json = NULL WHERE raw_json IS NOT NULL")

    # Purge readings older than 90 days and VACUUM to reclaim disk space
    purge_old_readings(conn)
    conn.close()


def purge_old_readings(conn: sqlite3.Connection | None = None) -> int:
    """Delete readings older than 90 days and VACUUM. Returns rows deleted."""
    close = conn is None
    if conn is None:
        conn = get_db()
    cutoff = (datetime.utcnow() - timedelta(days=90)).isoformat()
    cur = conn.execute("DELETE FROM readings WHERE timestamp < ?", (cutoff,))
    deleted = cur.rowcount
    conn.commit()
    conn.execute("VACUUM")
    if close:
        conn.close()
    return deleted


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------

def save_reading(data: dict) -> None:
    conn = get_db()
    with conn:
        conn.execute(
            """
            INSERT INTO readings (
                timestamp, battery_soc, battery_wh, battery_temp,
                solar_power_w, ac_in_power_w, dc_in_power_w, total_in_w,
                ac_out_power_w, dc_out_power_w, total_out_w,
                charging_status, device_online, data_source
            ) VALUES (
                :timestamp, :battery_soc, :battery_wh, :battery_temp,
                :solar_power_w, :ac_in_power_w, :dc_in_power_w, :total_in_w,
                :ac_out_power_w, :dc_out_power_w, :total_out_w,
                :charging_status, :device_online, :data_source
            )
            """,
            {
                "timestamp": data.get("timestamp", datetime.utcnow().isoformat()),
                "battery_soc": data.get("battery_soc"),
                "battery_wh": data.get("battery_wh"),
                "battery_temp": data.get("battery_temp"),
                "solar_power_w": data.get("solar_power_w"),
                "ac_in_power_w": data.get("ac_in_power_w"),
                "dc_in_power_w": data.get("dc_in_power_w"),
                "total_in_w": data.get("total_in_w"),
                "ac_out_power_w": data.get("ac_out_power_w"),
                "dc_out_power_w": data.get("dc_out_power_w"),
                "total_out_w": data.get("total_out_w"),
                "charging_status": data.get("charging_status"),
                "device_online": 1 if data.get("device_online") else 0,
                "data_source": data.get("data_source", "api"),
            },
        )
    conn.close()


def get_latest_reading() -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM readings ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_readings(hours: int = 24) -> list[dict]:
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM readings WHERE timestamp >= ? ORDER BY timestamp ASC",
        (since,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_readings_for_range(start: str, end: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM readings WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp ASC",
        (start, end),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Energy daily
# ---------------------------------------------------------------------------

def upsert_energy_daily(date: str, data: dict) -> None:
    conn = get_db()
    with conn:
        conn.execute(
            """
            INSERT INTO energy_daily (date, solar_kwh, charge_kwh, discharge_kwh, usage_kwh)
            VALUES (:date, :solar_kwh, :charge_kwh, :discharge_kwh, :usage_kwh)
            ON CONFLICT(date) DO UPDATE SET
                solar_kwh     = excluded.solar_kwh,
                charge_kwh    = excluded.charge_kwh,
                discharge_kwh = excluded.discharge_kwh,
                usage_kwh     = excluded.usage_kwh
            """,
            {
                "date": date,
                "solar_kwh": data.get("solar_kwh", 0),
                "charge_kwh": data.get("charge_kwh", 0),
                "discharge_kwh": data.get("discharge_kwh", 0),
                "usage_kwh": data.get("usage_kwh", 0),
            },
        )
    conn.close()


def get_energy_daily(days: int = 30) -> list[dict]:
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM energy_daily WHERE date >= ? ORDER BY date ASC",
        (since,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_lifetime_energy() -> dict:
    """Sum of all energy_daily rows — all-time totals since first recording."""
    conn = get_db()
    row = conn.execute(
        """SELECT
               SUM(solar_kwh)     AS solar_kwh,
               SUM(charge_kwh)    AS charge_kwh,
               SUM(discharge_kwh) AS discharge_kwh,
               SUM(usage_kwh)     AS usage_kwh,
               MIN(date)          AS since_date,
               COUNT(*)           AS days_recorded
           FROM energy_daily"""
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def compute_energy_from_readings(date_str: str) -> dict:
    """
    Compute daily energy totals by integrating instantaneous power readings.

    Each power stream is tracked independently so that simultaneous solar
    charging and AC/DC discharging (the normal C1000X operating mode) are
    both counted correctly.

    Definitions:
      solar_kwh     – energy produced by the solar panels
      charge_kwh    – energy drawn from the wall (AC input)
      discharge_kwh – energy delivered to AC + DC/USB loads
      usage_kwh     – same as discharge_kwh (alias kept for UI compat)

    Gaps longer than 10 minutes (e.g. app was offline) are ignored so a
    period of missing data doesn't artificially inflate the totals.
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT timestamp, solar_power_w, ac_in_power_w, ac_out_power_w, dc_out_power_w
           FROM readings
           WHERE timestamp >= ? AND timestamp < ?
           ORDER BY timestamp ASC""",
        (date_str + "T00:00:00", date_str + "T23:59:59.999"),
    ).fetchall()
    conn.close()

    energy = {"solar_kwh": 0.0, "charge_kwh": 0.0, "discharge_kwh": 0.0, "usage_kwh": 0.0}
    prev_ts = None

    for row in rows:
        try:
            ts_raw = row["timestamp"]
            # Normalise to UTC-aware datetime
            ts_raw = ts_raw.replace("Z", "+00:00")
            if "+" not in ts_raw[10:] and ts_raw[-6] not in ("+", "-"):
                ts_raw += "+00:00"
            ts = datetime.fromisoformat(ts_raw)
        except (ValueError, AttributeError):
            continue

        if prev_ts is not None:
            interval_h = (ts - prev_ts).total_seconds() / 3600.0
            # Skip gaps > 10 min — device was offline or app was restarted
            if 0 < interval_h <= (10 / 60):
                solar  = max(0.0, float(row["solar_power_w"]  or 0))
                ac_in  = max(0.0, float(row["ac_in_power_w"]  or 0))
                ac_out = max(0.0, float(row["ac_out_power_w"] or 0))
                dc_out = max(0.0, float(row["dc_out_power_w"] or 0))
                energy["solar_kwh"]     += solar          * interval_h / 1000.0
                energy["charge_kwh"]    += ac_in          * interval_h / 1000.0
                energy["discharge_kwh"] += (ac_out + dc_out) * interval_h / 1000.0
                energy["usage_kwh"]     += (ac_out + dc_out) * interval_h / 1000.0

        prev_ts = ts

    return {k: round(v, 4) for k, v in energy.items()}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(key: str, default: Any = None) -> Any:
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = get_db()
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.close()


def delete_setting(key: str) -> None:
    conn = get_db()
    with conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    conn.close()


# ---------------------------------------------------------------------------
# Motion events
# ---------------------------------------------------------------------------

def has_motion_analyzed(camera: str, segment_start: str) -> bool:
    """Return True if motion detection has already been run for this segment."""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM motion_analyzed WHERE camera=? AND segment_start=?",
        (camera, segment_start),
    ).fetchone()
    conn.close()
    return row is not None


def save_motion_events(camera: str, segment_start: str, events: list[dict]) -> None:
    """Persist motion events for a segment and mark it as analyzed."""
    conn = get_db()
    with conn:
        conn.execute(
            "DELETE FROM motion_events WHERE camera=? AND segment_start=?",
            (camera, segment_start),
        )
        for ev in events:
            conn.execute(
                "INSERT INTO motion_events (camera, segment_start, offset_sec, motion_type) "
                "VALUES (?,?,?,?)",
                (camera, segment_start, ev["offset_sec"], ev["motion_type"]),
            )
        conn.execute(
            "INSERT OR REPLACE INTO motion_analyzed (camera, segment_start) VALUES (?,?)",
            (camera, segment_start),
        )
    conn.close()


def get_motion_events(camera: str, since_iso: Optional[str] = None) -> list[dict]:
    """Return motion events for a camera, newest-first by segment."""
    conn = get_db()
    if since_iso:
        rows = conn.execute(
            "SELECT segment_start, offset_sec, motion_type FROM motion_events "
            "WHERE camera=? AND segment_start >= ? ORDER BY segment_start, offset_sec",
            (camera, since_iso),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT segment_start, offset_sec, motion_type FROM motion_events "
            "WHERE camera=? ORDER BY segment_start, offset_sec",
            (camera,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_motion_status(camera: str) -> dict[str, str]:
    """
    Return a dict of {segment_start: status} for all analyzed segments.

    Status values:
      'person'  – at least one person detected
      'motion'  – motion detected, no person
      'clear'   – analyzed, no motion found
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT
            ma.segment_start,
            CASE
                WHEN SUM(CASE WHEN me.motion_type = 'person' THEN 1 ELSE 0 END) > 0 THEN 'person'
                WHEN COUNT(me.id) > 0 THEN 'motion'
                ELSE 'clear'
            END AS status
        FROM motion_analyzed ma
        LEFT JOIN motion_events me
               ON ma.camera = me.camera AND ma.segment_start = me.segment_start
        WHERE ma.camera = ?
        GROUP BY ma.segment_start
        """,
        (camera,),
    ).fetchall()
    conn.close()
    return {r["segment_start"]: r["status"] for r in rows}


def purge_old_motion_events(camera: str, since_iso: str) -> None:
    """Delete motion events for segments older than since_iso."""
    conn = get_db()
    with conn:
        conn.execute(
            "DELETE FROM motion_events WHERE camera=? AND segment_start < ?",
            (camera, since_iso),
        )
        conn.execute(
            "DELETE FROM motion_analyzed WHERE camera=? AND segment_start < ?",
            (camera, since_iso),
        )
    conn.close()
