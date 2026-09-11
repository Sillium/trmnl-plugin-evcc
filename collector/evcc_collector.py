#!/usr/bin/env python3
"""
TRMNL EVCC Collector

Collects data from EVCC (EV Charge Controller) API and sends to TRMNL webhook
or serves it via HTTP for Terminus/BYOS.

Usage:
    # With config file
    python evcc_collector.py --config config.yaml

    # CLI only
    python evcc_collector.py -u http://evcc:7070 -w https://webhook_url

    # Dry run (print JSON, don't send)
    python evcc_collector.py -u http://evcc:7070 --dry-run
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests
import yaml

VERSION = "1.1.0"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class HomeAssistantClient:
    """Reads daily energy totals from Home Assistant.

    EVCC only publishes lifetime counters (and none at all for the grid
    meter), so day totals come from Home Assistant's utility_meter helpers
    instead. Each configured key maps to one entity id or a list of entity
    ids that get summed.
    """

    # Keys emitted into the payload, in display order.
    FIELDS = (
        "pv",
        "home",
        "home_without_wallbox",
        "grid_import",
        "grid_export",
        "battery_charged",
        "battery_discharged",
        "ev_charged",
    )

    def __init__(
        self,
        url: str,
        token: str,
        entities: Dict[str, Any],
        verbose: bool = False,
    ):
        self.url = url.rstrip('/')
        self.entities = entities or {}
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def _entity_value(self, entity_id: str) -> Optional[float]:
        """Fetch one entity's state as a float, or None if unusable."""
        try:
            response = self.session.get(
                f"{self.url}/api/states/{entity_id}", timeout=15
            )
            response.raise_for_status()
            state = response.json().get("state")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Home Assistant: {entity_id} failed: {e}")
            return None
        except ValueError:
            logger.warning(f"Home Assistant: {entity_id} returned invalid JSON")
            return None

        # HA reports these for entities that are restarting or broken
        if state in (None, "unavailable", "unknown", ""):
            logger.warning(f"Home Assistant: {entity_id} is '{state}'")
            return None

        try:
            return float(state)
        except (TypeError, ValueError):
            logger.warning(f"Home Assistant: {entity_id} is not numeric: {state!r}")
            return None

    def _sum(self, spec: Any) -> Optional[float]:
        """Resolve a config value (entity id, or list of them) to a total.

        Returns None only if every entity failed, so one dead sensor in a
        list doesn't discard the others.
        """
        if not spec:
            return None
        ids = [spec] if isinstance(spec, str) else list(spec)
        values = [v for v in (self._entity_value(i) for i in ids) if v is not None]
        if not values:
            return None
        return sum(values)

    @staticmethod
    def format_energy(kwh: Optional[float]) -> Optional[str]:
        """Format a kWh value for display: '9.4 kWh', '104 kWh'."""
        if kwh is None:
            return None
        if abs(kwh) >= 100:
            return f"{kwh:.0f} kWh"
        return f"{kwh:.1f} kWh"

    def collect(self) -> Dict[str, Any]:
        """Fetch all configured entities and build the energy_today block."""
        logger.info(f"Collecting daily energy from {self.url}")

        values = {f: self._sum(self.entities.get(f)) for f in self.FIELDS}

        result: Dict[str, Any] = {"available": any(v is not None for v in values.values())}
        for field, value in values.items():
            result[f"{field}_kwh"] = round(value, 1) if value is not None else None
            result[f"{field}_formatted"] = self.format_energy(value)

        # Autarkie: share of house consumption not taken from the grid.
        # The daily analogue of the live view's green share.
        home = values.get("home")
        grid_import = values.get("grid_import")
        if home and home > 0 and grid_import is not None:
            self_sufficiency = (home - grid_import) / home * 100
            result["self_sufficiency_pct"] = round(max(0.0, min(100.0, self_sufficiency)))
        else:
            result["self_sufficiency_pct"] = None

        return result


class EVCCCollector:
    """Collector for an EVCC instance."""

    MODE_LABELS = {
        "off": "Off",
        "pv": "Solar",
        "minpv": "Min+Solar",
        "now": "Fast",
    }

    BATTERY_MODE_LABELS = {
        "normal": "Normal",
        "hold": "Hold",
        "charge": "Grid charge",
        "unknown": "",
    }

    # Ignore tiny standby values so the display doesn't flip between
    # charging/discharging for a few watts of noise.
    BATTERY_IDLE_THRESHOLD_W = 10

    def __init__(
        self,
        url: str,
        webhook: Optional[str] = None,
        timezone: Optional[str] = None,
        max_loadpoints: int = 4,
        max_batteries: int = 4,
        power_unit: str = "auto",
        homeassistant: Optional["HomeAssistantClient"] = None,
        verbose: bool = False,
        dry_run: bool = False,
    ):
        self.url = url.rstrip('/')
        self.webhook = webhook
        self.timezone = timezone or os.environ.get('TZ', '')
        self.max_loadpoints = max_loadpoints
        self.max_batteries = max_batteries
        self.power_unit = power_unit
        self.homeassistant = homeassistant
        self.verbose = verbose
        self.dry_run = dry_run

    def _api_request(self) -> Dict[str, Any]:
        """Fetch state from EVCC API.

        GET {url}/api/state — single endpoint, no auth needed.
        EVCC v0.207+ removed the result wrapper; handle both formats.
        """
        api_url = f"{self.url}/api/state"
        try:
            response = requests.get(api_url, timeout=30)
            response.raise_for_status()
            data = response.json()
            # Handle both old (wrapped in 'result') and new (direct) formats
            return data.get('result', data)
        except requests.exceptions.ConnectionError as e:
            error_msg = f"Cannot connect to {self.url}: "
            if "Name or service not known" in str(e) or "nodename nor servname provided" in str(e):
                error_msg += "Host not found (check URL)"
            elif "Connection refused" in str(e):
                error_msg += "Connection refused (is EVCC running?)"
            else:
                error_msg += str(e)
            logger.error(error_msg)
            raise
        except requests.exceptions.Timeout:
            logger.error(f"Cannot connect to {self.url}: Request timed out")
            raise
        except requests.exceptions.HTTPError as e:
            logger.error(f"API request failed: {e}")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            raise

    @staticmethod
    def format_power(watts: float, unit: str = "auto") -> str:
        """Format power value for display.

        Args:
            watts: Power in watts (absolute value used).
            unit: "auto", "W", or "kW".

        Returns:
            Formatted string like "2.3 kW", "400 W", or "0 W".
        """
        w = abs(watts) if watts else 0

        if unit == "kW":
            return f"{w / 1000:.1f} kW"
        elif unit == "W":
            return f"{int(round(w))} W"
        else:
            # auto
            if w >= 1000:
                return f"{w / 1000:.1f} kW"
            else:
                return f"{int(round(w))} W"

    @staticmethod
    def format_duration(seconds: Optional[float]) -> str:
        """Format duration for display.

        Args:
            seconds: Duration in seconds (EVCC API convention).

        Returns:
            Formatted string: "" if 0/<60s, "45m", "2:44h", "1d 3h".
        """
        if not seconds:
            return ""

        if seconds < 60:
            return ""
        elif seconds < 3600:
            minutes = int(seconds // 60)
            return f"{minutes}m"
        elif seconds < 86400:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}:{minutes:02d}h"
        else:
            days = int(seconds // 86400)
            hours = int((seconds % 86400) // 3600)
            return f"{days}d {hours}h"

    @staticmethod
    def derive_status(lp: Dict[str, Any]) -> str:
        """Derive human-readable charging status from loadpoint state.

        Args:
            lp: Loadpoint dict from EVCC API.

        Returns:
            Status string: "Charging", "Waiting for solar", "Finished",
            "Connected", or "Disconnected".
        """
        connected = lp.get("connected", False)
        enabled = lp.get("enabled", False)
        charging = lp.get("charging", False)
        mode = lp.get("mode", "")

        if charging:
            return "Charging"
        if connected and enabled and not charging and mode in ("pv", "minpv"):
            return "Waiting for solar"
        if connected and enabled and not charging:
            return "Finished"
        if connected and not enabled:
            return "Connected"
        return "Disconnected"

    def transform_energy(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Transform energy/power data from EVCC state.

        Args:
            state: Full EVCC state dict.

        Returns:
            Dict with pv, grid, home power data and tariff info.
        """
        pv_power = state.get("pvPower", 0) or 0

        # Grid power: nested at state["grid"]["power"] (v0.207+),
        # fallback to state["gridPower"] for older EVCC
        grid_obj = state.get("grid", {})
        if isinstance(grid_obj, dict):
            grid_power_raw = grid_obj.get("power", state.get("gridPower", 0)) or 0
        else:
            grid_power_raw = state.get("gridPower", 0) or 0

        home_power = state.get("homePower", 0) or 0

        green_share_raw = state.get("greenShareHome", 0) or 0

        return {
            "pv_power": pv_power,
            "pv_power_formatted": self.format_power(pv_power, self.power_unit),
            "grid_power": abs(grid_power_raw),
            "grid_power_formatted": self.format_power(grid_power_raw, self.power_unit),
            "grid_import": grid_power_raw > 0,
            "home_power": home_power,
            "home_power_formatted": self.format_power(home_power, self.power_unit),
            "green_share_home": round(green_share_raw * 100),
            "tariff_grid": state.get("tariffGrid"),
            "tariff_feedin": state.get("tariffFeedIn"),
            "tariff_price_home": state.get("tariffPriceHome"),
        }

    def transform_battery(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Transform battery data from EVCC state.

        EVCC sign convention (house perspective): power > 0 means the battery
        is discharging into the home, power < 0 means it is charging.

        Handles all three shapes EVCC has used:
          - dict with aggregate + devices (current): state["battery"] =
            {power, soc, capacity, energy, returnEnergy, devices: [...]}
          - list of battery devices (older v0.2xx)
          - flat top-level keys (batterySoc / batteryPower)

        Args:
            state: Full EVCC state dict.

        Returns:
            Dict with battery configuration, SOC, power, flow direction,
            capacity, EVCC battery mode and per-device details.
        """
        raw = state.get("battery")

        total_power: float = 0
        soc: float = 0
        capacity: float = 0
        devices: List[Dict[str, Any]] = []
        configured = False

        if isinstance(raw, dict):
            # Aggregated form: use EVCC's own totals, they already account
            # for differently sized batteries.
            devices = [d for d in (raw.get("devices") or []) if isinstance(d, dict)]
            total_power = raw.get("power", 0) or 0
            soc = raw.get("soc", 0) or 0
            capacity = raw.get("capacity", 0) or 0
            configured = bool(devices) or bool(raw)
        elif isinstance(raw, list):
            devices = [d for d in raw if isinstance(d, dict)]
            configured = len(devices) > 0
            total_power = sum(d.get("power", 0) or 0 for d in devices)
            capacity = sum(d.get("capacity", 0) or 0 for d in devices)
            if capacity > 0:
                # Capacity-weighted SOC is the correct aggregate.
                soc = sum(
                    (d.get("soc", 0) or 0) * (d.get("capacity", 0) or 0) for d in devices
                ) / capacity
            elif devices:
                soc = sum(d.get("soc", 0) or 0 for d in devices) / len(devices)
        elif "batterySoc" in state or "batteryPower" in state:
            configured = True
            total_power = state.get("batteryPower", 0) or 0
            soc = state.get("batterySoc", 0) or 0
            capacity = state.get("batteryCapacity", 0) or 0

        if not configured:
            return {
                "configured": False,
                "soc": 0,
                "power": 0,
                "power_formatted": self.format_power(0, self.power_unit),
                "charging": False,
                "discharging": False,
                "idle": True,
                "state": "idle",
                "capacity_kwh": 0,
                "stored_kwh": 0,
                "mode": "",
                "mode_label": "",
                "grid_charge_active": False,
                "discharge_control": False,
                "device_count": 0,
                "devices": [],
            }

        charging = total_power < -self.BATTERY_IDLE_THRESHOLD_W
        discharging = total_power > self.BATTERY_IDLE_THRESHOLD_W

        if charging:
            flow_state = "charging"
        elif discharging:
            flow_state = "discharging"
        else:
            flow_state = "idle"

        battery_mode = state.get("batteryMode") or ""
        if battery_mode == "unknown":
            battery_mode = ""

        return {
            "configured": True,
            "soc": round(soc),
            "power": round(total_power),
            "power_formatted": self.format_power(total_power, self.power_unit),
            "charging": charging,
            "discharging": discharging,
            "idle": flow_state == "idle",
            "state": flow_state,
            "capacity_kwh": round(capacity, 1),
            "stored_kwh": round(capacity * soc / 100, 1),
            "mode": battery_mode,
            "mode_label": self.BATTERY_MODE_LABELS.get(battery_mode, battery_mode),
            "grid_charge_active": bool(state.get("batteryGridChargeActive", False)),
            "discharge_control": bool(state.get("batteryDischargeControl", False)),
            "device_count": len(devices),
            "devices": [
                self.transform_battery_device(d)
                for d in devices[:self.max_batteries]
            ],
        }

    def transform_battery_device(self, dev: Dict[str, Any]) -> Dict[str, Any]:
        """Transform a single battery device from EVCC state.

        Args:
            dev: Battery device dict from state["battery"]["devices"].

        Returns:
            Dict with title, SOC, power and flow direction for one battery.
        """
        power = dev.get("power", 0) or 0
        soc = dev.get("soc", 0) or 0
        capacity = dev.get("capacity", 0) or 0

        charging = power < -self.BATTERY_IDLE_THRESHOLD_W
        discharging = power > self.BATTERY_IDLE_THRESHOLD_W

        return {
            "title": dev.get("title") or dev.get("name", ""),
            "soc": round(soc),
            "power": round(power),
            "power_formatted": self.format_power(power, self.power_unit),
            "charging": charging,
            "discharging": discharging,
            "state": "charging" if charging else ("discharging" if discharging else "idle"),
            "capacity_kwh": round(capacity, 1),
            "controllable": bool(dev.get("controllable", False)),
        }

    def transform_loadpoint(self, lp: Dict[str, Any]) -> Dict[str, Any]:
        """Transform a single loadpoint from EVCC state.

        Args:
            lp: Loadpoint dict from EVCC API.

        Returns:
            Dict with all loadpoint fields for the template.
        """
        mode = lp.get("mode", "off")
        charge_power = lp.get("chargePower", 0) or 0
        charged_energy = lp.get("chargedEnergy", 0) or 0

        # Solar percentage: prefer sessionSolarPercentage (already 0-100 range)
        session_solar_pct = lp.get("sessionSolarPercentage")
        if session_solar_pct is None:
            session_solar_pct = 0
        else:
            session_solar_pct = round(session_solar_pct)

        # Session price is a float (total cost for the session)
        session_price_raw = lp.get("sessionPrice")
        session_price = round(session_price_raw, 2) if session_price_raw is not None else None

        return {
            "title": lp.get("title", ""),
            "mode": mode,
            "mode_label": self.MODE_LABELS.get(mode, mode),
            "charging": lp.get("charging", False),
            "connected": lp.get("connected", False),
            "enabled": lp.get("enabled", False),
            "status": self.derive_status(lp),
            "charge_power": charge_power,
            "charge_power_formatted": self.format_power(charge_power, self.power_unit),
            "charged_energy_kwh": round(charged_energy / 1000, 1),
            "charge_duration_formatted": self.format_duration(lp.get("chargeDuration")),
            "charge_remaining_formatted": self.format_duration(lp.get("chargeRemainingDuration")),
            "session_solar_pct": session_solar_pct,
            "session_price": session_price,
            "vehicle_title": lp.get("vehicleTitle") or lp.get("vehicleName"),
            "vehicle_soc": round(lp.get("vehicleSoc", 0) or 0),
            "vehicle_range": lp.get("vehicleRange"),
            "vehicle_connected": lp.get("connected", False),
            "limit_soc": lp.get("effectiveLimitSoc"),
            "plan_active": lp.get("planActive", False),
            "plan_time": lp.get("planTime"),
            "phases_active": lp.get("phasesActive"),
        }

    @staticmethod
    def transform_statistics(state: Dict[str, Any]) -> Dict[str, Any]:
        """Transform statistics from EVCC state.

        Args:
            state: Full EVCC state dict.

        Returns:
            Dict with "30d" and "total" keys, each containing
            charged_kwh, solar_pct, and avg_price.
        """
        stats = state.get("statistics", {})
        result = {}

        for period in ("30d", "total"):
            period_data = stats.get(period, {})
            charged = period_data.get("chargedKWh")
            solar = period_data.get("solarPercentage")
            price = period_data.get("avgPrice")
            result[period] = {
                "charged_kwh": round(charged, 1) if charged is not None else None,
                "solar_pct": round(solar) if solar is not None else None,
                "avg_price": round(price, 2) if price is not None else None,
            }

        return result

    def _get_timezone_abbrev(self) -> str:
        """Get timezone abbreviation from configured timezone."""
        if not self.timezone:
            return "UTC"

        try:
            tz = ZoneInfo(self.timezone)
            now = datetime.now(tz)
            abbrev = now.strftime('%Z')
            return abbrev if abbrev else self.timezone
        except Exception:
            return self.timezone if self.timezone else "UTC"

    def collect(self) -> Dict[str, Any]:
        """Collect all data from EVCC and build the payload.

        Returns:
            Dict with merge_variables for TRMNL.
        """
        logger.info(f"Collecting data from {self.url}")

        state = self._api_request()

        # Timestamps
        now_utc = datetime.now(timezone.utc)
        utc_iso = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')

        if self.timezone:
            tz = ZoneInfo(self.timezone)
            local_now = datetime.now(tz)
            local_formatted = local_now.strftime('%Y-%m-%d %H:%M')
        else:
            local_formatted = datetime.now().strftime('%Y-%m-%d %H:%M')

        tz_abbrev = self._get_timezone_abbrev()

        loadpoints = state.get("loadpoints", [])

        payload = {
            "merge_variables": {
                "site_title": state.get("siteTitle", "EVCC"),
                "last_updated": utc_iso,
                "last_updated_local": local_formatted,
                "timezone": tz_abbrev,
                "currency": state.get("currency", "EUR"),
                "energy": self.transform_energy(state),
                "battery": self.transform_battery(state),
                "loadpoints": [
                    self.transform_loadpoint(lp)
                    for lp in loadpoints[:self.max_loadpoints]
                ],
                "loadpoint_count": len(loadpoints),
                "statistics": self.transform_statistics(state),
            }
        }

        # Optional: daily energy totals from Home Assistant. A failure here
        # must not cost us the EVCC data, so it never raises.
        if self.homeassistant:
            try:
                payload["merge_variables"]["energy_today"] = self.homeassistant.collect()
            except Exception as e:
                logger.warning(f"Home Assistant collection failed: {e}")
                payload["merge_variables"]["energy_today"] = {"available": False}

        return payload

    def send(self, payload: Dict[str, Any]) -> bool:
        """Send payload to TRMNL webhook.

        Args:
            payload: Full payload dict with merge_variables.

        Returns:
            True if sent successfully, False otherwise.
        """
        payload_json = json.dumps(payload)

        if self.verbose:
            logger.info(f"Payload size: {len(payload_json)} bytes")

        # Dry run or no webhook - print to stdout
        if self.dry_run or not self.webhook:
            print(json.dumps(payload, indent=2))
            return True

        # Send to webhook
        logger.info("Sending data to TRMNL webhook...")
        try:
            response = requests.post(
                self.webhook,
                headers={'Content-Type': 'application/json'},
                data=payload_json,
                timeout=30,
            )
            response.raise_for_status()
            logger.info(f"Successfully sent data (HTTP {response.status_code})")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send data: {e}")
            if self.verbose and hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False


# --- HTTP serve mode for Terminus/BYOS ---

_serve_data: Dict[str, Dict[str, Any]] = {}
_serve_lock = threading.Lock()


def store_payload(payload: Dict[str, Any]):
    """Cache latest payload for HTTP serving."""
    data = payload.get('merge_variables', payload)
    with _serve_lock:
        _serve_data["evcc"] = data


class DataHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves cached EVCC data as JSON."""

    def do_GET(self):
        path = self.path.rstrip('/')

        if path == '' or path == '/':
            with _serve_lock:
                endpoints = {
                    name: f'/data/{name}' for name in _serve_data
                }
            self._json_response(200, {"endpoints": {"evcc": "/data/evcc"}})
            return

        if path == '/data/evcc':
            with _serve_lock:
                data = _serve_data.get("evcc")
            if data is None:
                self._json_response(404, {"error": "No data collected yet"})
                return
            self._json_response(200, data)
            return

        self.send_response(404)
        self.end_headers()

    def _json_response(self, status: int, body: Any):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, format, *args):
        logger.debug(f"HTTP: {args[0]}")


def start_server(host: str, port: int):
    """Start HTTP server in a daemon thread."""
    server = HTTPServer((host, port), DataHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"HTTP server listening on {host}:{port}")
    return server


def load_config(config_path: str) -> Dict[str, Any]:
    """Load configuration from YAML file."""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def run_collection(collector: EVCCCollector) -> bool:
    """Run a single collection cycle.

    Args:
        collector: The EVCC collector instance.

    Returns:
        True if collection and send succeeded, False otherwise.
    """
    logger.info(f"Starting collection cycle at {datetime.now()}")
    try:
        payload = collector.collect()
        store_payload(payload)
        success = collector.send(payload)
        if success:
            logger.info("Collection complete: success")
        else:
            logger.warning("Collection complete: webhook send failed")
        return success
    except Exception as e:
        logger.error(f"Collection failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description='TRMNL EVCC Collector - Collect data from EVCC and send to TRMNL webhook',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # With config file
  %(prog)s --config config.yaml

  # CLI only
  %(prog)s -u http://evcc:7070 -w https://webhook_url

  # Dry run (print JSON, don't send)
  %(prog)s -u http://evcc:7070 --dry-run

  # HTTP serve mode
  %(prog)s --config config.yaml --serve --port 8080
'''
    )

    # Config file
    parser.add_argument('--config', '-C', help='Path to YAML config file')

    # Instance options
    parser.add_argument('-u', '--url', help='EVCC URL (e.g. http://evcc:7070)')
    parser.add_argument('-w', '--webhook', help='TRMNL webhook URL')
    parser.add_argument('-z', '--timezone', default='', help='Timezone (default: from TZ env)')
    parser.add_argument('-i', '--interval', type=int, default=0,
                        help='Collection interval in seconds (0 = run once)')
    parser.add_argument('--max-loadpoints', type=int, default=4,
                        help='Max loadpoints to include (default: 4)')
    parser.add_argument('--max-batteries', type=int, default=4,
                        help='Max battery devices to include (default: 4)')
    parser.add_argument('--power-unit', choices=['W', 'kW', 'auto'], default='auto',
                        help='Power display unit (default: auto)')
    parser.add_argument('--serve', action='store_true', help='Enable HTTP server')
    parser.add_argument('--port', type=int, default=8080, help='HTTP server port (default: 8080)')
    parser.add_argument('--host', default='0.0.0.0', help='HTTP bind address (default: 0.0.0.0)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Debug logging')
    parser.add_argument('--dry-run', action='store_true', help='Print JSON, don\'t send')
    parser.add_argument('--version', action='version', version=f'%(prog)s {VERSION}')

    args = parser.parse_args()

    # Build collector from config or CLI args
    if args.config:
        try:
            config = load_config(args.config)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            sys.exit(1)

        interval = config.get('interval', args.interval)
        evcc_url = config.get('evcc_url')
        if not evcc_url:
            logger.error("Config file must contain 'evcc_url'")
            sys.exit(1)

        # Optional Home Assistant source for daily energy totals
        ha_config = config.get('homeassistant') or {}
        ha_client = None
        if ha_config.get('url') and ha_config.get('token'):
            ha_client = HomeAssistantClient(
                url=ha_config['url'],
                token=ha_config['token'],
                entities=ha_config.get('energy_today', {}),
                verbose=args.verbose,
            )
            logger.info(f"Home Assistant: {ha_client.url}")
        elif ha_config:
            logger.warning("homeassistant config needs both 'url' and 'token' - skipping")

        collector = EVCCCollector(
            url=evcc_url,
            webhook=config.get('webhook', args.webhook),
            timezone=config.get('timezone', args.timezone),
            max_loadpoints=config.get('max_loadpoints', args.max_loadpoints),
            max_batteries=config.get('max_batteries', args.max_batteries),
            power_unit=config.get('power_unit', args.power_unit),
            homeassistant=ha_client,
            verbose=args.verbose,
            dry_run=args.dry_run,
        )

        serve = args.serve
        serve_config = config.get('serve', {})
        serve = serve or serve_config.get('enabled', False)
        serve_port = serve_config.get('port', args.port)
        serve_host = serve_config.get('host', args.host)
    else:
        if not args.url:
            logger.error("Either --config or --url is required")
            parser.print_help()
            sys.exit(1)

        interval = args.interval
        collector = EVCCCollector(
            url=args.url,
            webhook=args.webhook,
            timezone=args.timezone,
            max_loadpoints=args.max_loadpoints,
            max_batteries=args.max_batteries,
            power_unit=args.power_unit,
            verbose=args.verbose,
            dry_run=args.dry_run,
        )

        serve = args.serve
        serve_port = args.port
        serve_host = args.host

    logger.info(f"TRMNL EVCC Collector v{VERSION}")
    logger.info(f"EVCC URL: {collector.url}")

    # Setup signal handlers
    def signal_handler(signum, frame):
        logger.info("Shutting down...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start HTTP server if requested
    if serve:
        start_server(serve_host, serve_port)

    # Run collection
    if interval > 0:
        logger.info(f"Running continuously with {interval}s interval (Ctrl+C to stop)")
        while True:
            run_collection(collector)
            logger.info(f"Sleeping for {interval} seconds...")
            time.sleep(interval)
    else:
        # Single run
        success = run_collection(collector)
        logger.info("Done!")
        sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
