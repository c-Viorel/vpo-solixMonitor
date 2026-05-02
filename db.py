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
    """Create tables if they don't exist."""
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

            CREATE INDEX IF NOT EXISTS idx_readings_ts
                ON readings (timestamp);
        """)
    conn.close()


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
                charging_status, device_online, data_source, raw_json
            ) VALUES (
                :timestamp, :battery_soc, :battery_wh, :battery_temp,
                :solar_power_w, :ac_in_power_w, :dc_in_power_w, :total_in_w,
                :ac_out_power_w, :dc_out_power_w, :total_out_w,
                :charging_status, :device_online, :data_source, :raw_json
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
                "raw_json": json.dumps(data.get("raw")),
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
