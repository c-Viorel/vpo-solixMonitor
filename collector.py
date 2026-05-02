"""
Anker Solix data collector.

Fetches device status and energy statistics from the Anker cloud API and
normalises the response into a flat dict that can be stored in the DB.

Important notes about the C1000X (model A1761):
- It is a standalone PPS device.
- The REST cloud API provides only minimal data for standalone devices
  (device info, firmware, and energy statistics via the power_service endpoints).
- Real-time power values (SOC, watts in/out, temps) come via MQTT.
- This module covers both REST polling and an optional MQTT listener.
"""
import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# REST-API based collection
# ---------------------------------------------------------------------------

async def _fetch(email: str, password: str, country: str) -> dict:
    """Call the Anker Solix API and return the raw cache dicts.

    For standalone PPS devices (e.g. SOLIX C1000 A1763) the REST API only
    provides device metadata via get_bind_devices().  Real-time SOC and power
    values are delivered exclusively via MQTT and will be None in this snapshot
    unless the MqttListener has already updated them in the DB.
    """
    try:
        from api import api as solix_api          # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "anker-solix-api is not installed.  Run: pip install -r requirements.txt"
        ) from exc

    async with aiohttp.ClientSession() as session:
        myapi = solix_api.AnkerSolixApi(email, password, country, session, logger)
        # get_bind_devices reliably populates myapi.devices for standalone PPS
        # devices; update_device_details can silently recycle them away.
        await myapi.get_bind_devices()

        return {
            "account": myapi.account,
            "sites": myapi.sites,
            "devices": myapi.devices,
        }


def _extract_pps_reading(raw: dict) -> dict:
    """
    Extract a normalised reading dict from the raw API cache.

    The C1000X appears under raw["devices"] keyed by its serial number.
    We look for the first device whose pn (part number / model) matches
    known C1000X models or any device of type 'pps'.
    """
    C1000X_MODELS = {"A1761", "A1763"}  # C1000X and C1000 Gen2

    reading: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "battery_soc": None,
        "battery_wh": None,
        "battery_temp": None,
        "solar_power_w": None,
        "ac_in_power_w": None,
        "dc_in_power_w": None,
        "total_in_w": None,
        "ac_out_power_w": None,
        "dc_out_power_w": None,
        "total_out_w": None,
        "charging_status": None,
        "device_online": False,
        "data_source": "api",
        "raw": raw,
    }

    devices: dict = raw.get("devices", {})
    if not devices:
        logger.warning("No devices found in API response.")
        return reading

    # Pick the best matching device
    target_device = None
    for sn, dev in devices.items():
        pn = str(dev.get("pn", dev.get("device_pn", ""))).upper()
        dev_type = str(dev.get("device_type", dev.get("type", ""))).lower()
        if pn in C1000X_MODELS or "pps" in dev_type:
            target_device = dev
            break

    if target_device is None:
        # Fall back to the first available device
        target_device = next(iter(devices.values()))

    # ---- battery ----
    # A1763 MQTT field: battery_soc (REST API does not provide this)
    reading["battery_soc"] = _num(
        target_device.get("battery_soc")
        or target_device.get("charge_soc")
        or target_device.get("soc")
    )
    # battery_wh = remaining energy = total_capacity × SOC%
    # Try a direct remaining-energy field first, then calculate from SOC
    raw_wh = _num(
        target_device.get("battery_energy")      # may contain remaining Wh on some firmwares
        or target_device.get("remaining_wh")
        or target_device.get("battery_remaining_wh")
    )
    if raw_wh is None and reading["battery_soc"] is not None:
        total_wh = _num(
            target_device.get("battery_capacity")
            or target_device.get("battery_size")
        )
        if total_wh:
            raw_wh = round(total_wh * reading["battery_soc"] / 100, 1)
    reading["battery_wh"] = raw_wh
    # A1763 MQTT field: temperature
    reading["battery_temp"] = _num(
        target_device.get("temperature")
        or target_device.get("battery_temp")
        or target_device.get("temp")
    )

    # ---- power (W) – A1763 MQTT field names from mqttmap.py ----
    # DC input = solar panels or car charger via Anderson/XT60
    reading["solar_power_w"] = _num(
        target_device.get("dc_input_power_total")   # MQTT a8.01
        or target_device.get("solar_power")
        or target_device.get("solar_input_power")
    )
    # AC input = wall charger
    reading["ac_in_power_w"] = _num(
        target_device.get("ac_input_power")          # MQTT a6.02
        or target_device.get("ac_in_power")
    )
    reading["dc_in_power_w"] = None  # included in solar_power_w above for A1763

    # AC output
    reading["ac_out_power_w"] = _num(
        target_device.get("ac_output_power")         # MQTT a7.01
        or target_device.get("ac_out_power")
    )
    # DC/USB output
    reading["dc_out_power_w"] = _num(
        target_device.get("dc_output_power_total")   # MQTT b2.01
        or target_device.get("dc_out_power")
    )

    # ---- totals ----
    solar  = reading["solar_power_w"] or 0
    ac_in  = reading["ac_in_power_w"] or 0
    ac_out = reading["ac_out_power_w"] or 0
    dc_out = reading["dc_out_power_w"] or 0

    reading["total_in_w"]  = (solar + ac_in) or None
    reading["total_out_w"] = (ac_out + dc_out) or None

    # ---- status ----
    # A1763 bind_devices: charge=false/true
    is_charging = target_device.get("charge", False)
    charge_status = str(
        target_device.get("charging_status")
        or target_device.get("charge_status")
        or ("charging" if is_charging else "")
        or ""
    ).lower()

    STATUS_MAP = {
        "0": "standby", "1": "charging", "2": "discharging", "3": "full",
        "charging": "charging", "discharging": "discharging",
        "standby": "standby", "full": "full",
        "charge": "charging", "discharge": "discharging",
        "true": "charging", "false": "standby",
    }
    reading["charging_status"] = STATUS_MAP.get(charge_status, charge_status or "standby")

    # wifi_online is the field name from get_bind_devices for A1763
    online_val = target_device.get("wifi_online", target_device.get("device_online", True))
    reading["device_online"] = bool(online_val)

    return reading


def _extract_energy_daily(raw: dict, date_str: str) -> dict:
    """Pull today's energy totals (kWh) from the API energy cache."""
    energy: dict = {"solar_kwh": 0, "charge_kwh": 0, "discharge_kwh": 0, "usage_kwh": 0}

    for site_id, site in raw.get("sites", {}).items():
        stat = site.get("energy_statistics", site.get("energy_details", {}))
        if not stat:
            continue
        energy["solar_kwh"] += _num(stat.get("solar_production_kwh") or stat.get("solar_kwh") or 0) or 0
        energy["charge_kwh"] += _num(stat.get("charge_kwh") or stat.get("solarbank_charge_kwh") or 0) or 0
        energy["discharge_kwh"] += _num(stat.get("discharge_kwh") or 0) or 0
        energy["usage_kwh"] += _num(stat.get("home_usage_kwh") or stat.get("usage_kwh") or 0) or 0

    return energy


def _num(value) -> Optional[float]:
    """Safely convert a value to float, return None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public entry point (sync wrapper used by scheduler / cron)
# ---------------------------------------------------------------------------

def run_collection(email: str, password: str, country: str) -> tuple[dict, dict]:
    """
    Run a full collection cycle synchronously.

    Returns
    -------
    (reading, energy_today)  – both are normalised dicts ready for the DB.
    """
    loop = asyncio.new_event_loop()
    try:
        raw = loop.run_until_complete(_fetch(email, password, country))
    finally:
        loop.close()

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    reading = _extract_pps_reading(raw)
    energy = _extract_energy_daily(raw, date_str)
    return reading, energy


# ---------------------------------------------------------------------------
# Credential test (sync wrapper – used by admin test-connection button)
# ---------------------------------------------------------------------------

async def _test_auth(email: str, password: str, country: str) -> dict:
    """Authenticate and return basic account/device info without storing anything."""
    try:
        from api import api as solix_api  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError("anker-solix-api not installed.") from exc

    async with aiohttp.ClientSession() as session:
        myapi = solix_api.AnkerSolixApi(email, password, country, session, logger)
        # restart=True clears the cached token file so we always hit the server
        # with the supplied credentials rather than reusing a previous session.
        authenticated = await myapi.apisession.async_authenticate(restart=True)
        if not authenticated:
            return {"ok": False, "message": "Authentication failed. Check your email, password and country code."}

        await myapi.get_bind_devices()

        devices = myapi.devices or {}
        sites   = myapi.sites or {}
        account = myapi.account or {}

        device_list = []
        for sn, dev in devices.items():
            name = (dev.get("name") or dev.get("alias") or dev.get("device_name") or sn)
            pn   = dev.get("device_pn") or dev.get("pn") or ""
            device_list.append({"sn": sn[-4:], "name": name, "pn": pn})

        return {
            "ok": True,
            "nickname": account.get("nickname") or account.get("email", email)[:3] + "***",
            "sites": len(sites),
            "devices": len(devices),
            "device_list": device_list[:6],
        }


def test_connection(email: str, password: str, country: str) -> dict:
    """Synchronous wrapper around _test_auth. Returns result dict."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_test_auth(email, password, country))
    except Exception as exc:
        logger.warning("Credential test failed: %s", exc)
        cls = type(exc).__name__
        msg = str(exc)
        if "InvalidCredentials" in cls or "Authorization" in cls or "401" in msg:
            msg = "Invalid email or password."
        elif "NeedVerifyCode" in cls:
            msg = "Anker requires email verification. Log into the Anker app first, then retry."
        elif "VerifyCode" in cls:
            msg = f"Verification required: {msg}"
        elif "country" in msg.lower() or "region" in msg.lower():
            msg = "Country code mismatch. Try a different 2-letter ISO code (e.g. us, de, gb)."
        elif "timeout" in msg.lower() or "connect" in msg.lower() or "ConnectError" in cls:
            msg = "Connection timeout. Check your internet connection."
        return {"ok": False, "message": msg}
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# MQTT real-time listener (optional – runs as a background thread)
# ---------------------------------------------------------------------------

class MqttListener:
    """
    Subscribe to the Anker MQTT cloud server for real-time device messages.

    When a new message arrives the provided `on_reading` callback is called
    with a normalised reading dict.  Falls back silently if MQTT is not
    available or credentials are wrong.
    """

    def __init__(self, email: str, password: str, country: str, on_reading):
        self.email = email
        self.password = password
        self.country = country
        self.on_reading = on_reading
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mqtt-listener")
        self._thread.start()
        logger.info("MQTT listener thread started.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("MQTT listener thread stopped.")

    def _run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._async_run())
        except Exception as exc:
            logger.error("MQTT listener error: %s", exc)

    async def _async_run(self) -> None:
        try:
            from api import api as solix_api  # type: ignore[import]
            from api.mqtt_pps import SolixMqttDevicePps  # type: ignore[import]
        except ImportError:
            logger.warning("anker-solix-api not installed – MQTT disabled.")
            return

        try:
            async with aiohttp.ClientSession() as session:
                myapi = solix_api.AnkerSolixApi(
                    self.email, self.password, self.country, session, logger
                )
                # Populate device cache with the reliable bind_devices endpoint.
                await myapi.get_bind_devices()

                if not myapi.devices:
                    logger.warning("MQTT: no devices found – aborting.")
                    return

                # Start MQTT session (connects, registers mqtt_received callback).
                mqtt_session = await myapi.startMqttSession()
                if mqtt_session is None:
                    logger.warning("MQTT: failed to start session.")
                    return

                # For each device: subscribe to its data topic and register a
                # callback that fires whenever the library merges new MQTT values.
                pps_devices = []
                for sn, dev in myapi.devices.items():
                    pn = dev.get("device_pn") or ""
                    # Build subscribe topic: dt/{app_name}/{pn}/{sn}/#
                    prefix = mqtt_session.get_topic_prefix(
                        deviceDict={"device_sn": sn, "device_pn": pn}
                    )
                    if prefix:
                        topic = prefix + "#"
                        mqtt_session.subscribe(topic)
                        logger.debug("MQTT subscribed to %s", topic)

                    # Register update callback on the api (fires on every new mqtt value).
                    def _make_callback(device_sn):
                        def _cb(deviceSn=None, **kwargs):
                            d = myapi.devices.get(device_sn, {})
                            # MQTT values live under d["mqtt_data"]; flatten them
                            # so _extract_pps_reading can find them at top level.
                            d_flat = {**d, **(d.get("mqtt_data") or {})}
                            reading = _extract_pps_reading(
                                {"devices": {device_sn: d_flat}}
                            )
                            reading["data_source"] = "mqtt"
                            try:
                                self.on_reading(reading)
                            except Exception as cb_exc:
                                logger.error("MQTT on_reading error: %s", cb_exc)
                        return _cb

                    myapi.mqtt_update_callback(func=_make_callback(sn))

                    # Create the PPS device helper and request real-time data.
                    try:
                        pps_dev = SolixMqttDevicePps(
                            api_instance=myapi, device_sn=sn
                        )
                        pps_devices.append(pps_dev)
                        # Trigger device to publish real-time data for 600 s,
                        # then re-trigger in the keep-alive loop below.
                        await pps_dev.realtime_trigger(timeout=600)
                        logger.info(
                            "MQTT realtime_trigger sent to %s (%s).", sn[-6:], pn
                        )
                    except Exception as trig_exc:
                        logger.warning(
                            "MQTT realtime_trigger failed for %s: %s", sn[-6:], trig_exc
                        )

                logger.info(
                    "MQTT listener active (%d device(s)).", len(myapi.devices)
                )

                # Keep-alive loop: re-trigger every 5 minutes so the device
                # keeps sending data beyond the 10-minute default timeout.
                retrigger_interval = 290  # slightly under 5 min
                elapsed = 0
                while not self._stop_event.is_set():
                    await asyncio.sleep(5)
                    elapsed += 5
                    if elapsed >= retrigger_interval:
                        elapsed = 0
                        for pps_dev in pps_devices:
                            try:
                                await pps_dev.realtime_trigger(timeout=600)
                            except Exception:
                                pass

                myapi.stopMqttSession()
        except Exception as exc:
            logger.warning("MQTT async run failed: %s", exc)
