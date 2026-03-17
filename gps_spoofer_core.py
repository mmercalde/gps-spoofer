# -*- coding: utf-8 -*-
# gps_spoofer_core.py
# Shared core logic for GPS Spoofer.
# Imported by gps_spoofer_gui.py (tkinter) and gps_spoofer_web.py (Flask).
# Has NO dependency on tkinter or Flask - runs standalone.

import os
import json
import subprocess
import threading
import shlex
import traceback
import time
import csv
import base64
import socket
import collections
from datetime import datetime

import requests
from geopy.geocoders import Nominatim

# ---------------------------------------------------------------------------
# Path & operational constants
# ---------------------------------------------------------------------------

CONFIG_PATH               = os.path.expanduser("~/gps_spoofer/config.json")
EPHEMERIS_DIR             = os.path.expanduser("~/gps_spoofer/ephemeris")
LATEST_TIME_PATH          = os.path.join(EPHEMERIS_DIR, "latest_time.txt")
LATEST_FILE_PATH          = os.path.join(EPHEMERIS_DIR, "latest_file.txt")
SIM_OUTPUT                = os.path.expanduser("~/gps_spoofer/sim_output/gpssim.c8")
TEMP_DIR                  = os.path.expanduser("~/gps_spoofer/temp")
TEMP_ROUTE_MOTION_FILE    = os.path.join(TEMP_DIR, "temp_route_motion.csv")
GPS_SDR_SIM_EXECUTABLE    = os.path.expanduser("~/gps-sdr-sim/gps-sdr-sim")
HACKRF_TRANSFER_EXECUTABLE = "hackrf_transfer"
HACKRF_SD_GPS_PATH        = "/media/michael/3402-CA84/GPS/"

DEFAULT_FREQ_HZ_STR       = "1575420000"
DEFAULT_FREQ_MHZ          = 1575.420
DEFAULT_SAMPLE_RATE_HZ    = 2600000
DEFAULT_ALTITUDE_METERS   = 100.0
DEFAULT_REMOTE_SERVER_URL = "http://45.32.131.224:5000"

REMOTE_GEN_POLLING_INTERVAL_SEC = 10
REMOTE_GEN_TOTAL_TIMEOUT_SEC    = 600

DF_CMD_TIMEOUT    = 10
MKDIR_CMD_TIMEOUT = 10
CP_CMD_TIMEOUT    = 600
STAT_CMD_TIMEOUT  = 10

BLAST_GAIN_DB          = 47
AUTO_BLAST_DURATION_SEC = 5
MAX_LOG_LINES          = 200   # ring-buffer size for LogBuffer


# ---------------------------------------------------------------------------
# LogBuffer  – thread-safe ring buffer consumed by both UIs
# ---------------------------------------------------------------------------

class LogBuffer:
    """
    Thread-safe log ring-buffer.
    Both the tkinter GUI and the Flask web app read from the same instance
    held inside SpooferCore.
    """

    def __init__(self, maxlen=MAX_LOG_LINES):
        self._lock  = threading.Lock()
        self._lines = collections.deque(maxlen=maxlen)
        self._callbacks = []          # UI-specific listeners (optional)

    def log(self, message: str):
        """Append a message. Also prints to stdout and fires any registered callbacks."""
        print(message)
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {message}"
        with self._lock:
            self._lines.append(entry)
        for cb in list(self._callbacks):
            try:
                cb(entry)
            except Exception:
                pass

    def register_callback(self, fn):
        """Register a function(message) called on every new log entry."""
        if fn not in self._callbacks:
            self._callbacks.append(fn)

    def unregister_callback(self, fn):
        if fn in self._callbacks:
            self._callbacks.remove(fn)

    def get_lines(self, last_n=None):
        """Return a list of recent log lines (newest last)."""
        with self._lock:
            lines = list(self._lines)
        if last_n is not None:
            return lines[-last_n:]
        return lines

    def clear(self):
        with self._lock:
            self._lines.clear()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    default = {
        "Maps_api_key": "YOUR_Maps_API_KEY_HERE",
        "address": "", "latitude": None, "longitude": None, "altitude": None,
        "start_address": "", "start_latlon": [None, None], "start_altitude": None,
        "end_address": "", "end_latlon": [None, None], "end_altitude": None,
        "location_mode": "Static (Address Lookup)", "motion_file_path": "",
        "gain": 15, "duration": 60, "map_zoom": 14,
        "frequency_hz": int(DEFAULT_FREQ_HZ_STR), "blast_duration_sec": 3,
        "ephemeris_file": None, "map_type": "roadmap",
        "auto_blast_enabled": False, "auto_blast_interval_min": 5,
        "active_map_enabled": False,
        "remote_server_url": DEFAULT_REMOTE_SERVER_URL,
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            for k, v in default.items():
                cfg.setdefault(k, v)
            return cfg
        except json.JSONDecodeError:
            print(f"[core] Bad JSON in {CONFIG_PATH}. Using defaults.")
    else:
        print(f"[core] Config not found at {CONFIG_PATH}. Creating defaults.")
        save_config(default)
    return default


def save_config(cfg: dict):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Geocoding / elevation / map helpers  (no UI deps)
# ---------------------------------------------------------------------------

def geocode_address(address: str):
    """Return (lat, lon) or (None, None)."""
    try:
        geo = Nominatim(user_agent="gps_spoofer_core_nominatim", timeout=10)
        loc = geo.geocode(address)
        if loc:
            return loc.latitude, loc.longitude
    except Exception as e:
        print(f"[core] Nominatim error: {e}")
    return None, None


def get_elevation(lat, lon, api_key) -> float | None:
    if not api_key or api_key == "YOUR_Maps_API_KEY_HERE":
        return None
    if lat is None or lon is None:
        return None
    try:
        url = (f"https://maps.googleapis.com/maps/api/elevation/json"
               f"?locations={float(lat):.7f},{float(lon):.7f}&key={api_key}")
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data["status"] == "OK" and data["results"]:
            return float(data["results"][0]["elevation"])
    except Exception as e:
        print(f"[core] Elevation API error: {e}")
    return None


def download_static_map(lat, lon, zoom=14, width=600, height=300,
                        maptype="roadmap", api_key=None) -> bytes | None:
    if not api_key or api_key == "YOUR_Maps_API_KEY_HERE":
        return None
    if lat is None or lon is None:
        return None
    try:
        params = {
            "center":  f"{float(lat):.7f},{float(lon):.7f}",
            "zoom":    str(int(zoom)),
            "size":    f"{int(width)}x{int(height)}",
            "maptype": maptype,
            "markers": f"color:red|label:L|{float(lat):.7f},{float(lon):.7f}",
            "key":     api_key,
        }
        r = requests.get("https://maps.googleapis.com/maps/api/staticmap",
                         params=params, timeout=15)
        if r.status_code == 200:
            return r.content
    except Exception as e:
        print(f"[core] Static map error: {e}")
    return None


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 1))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Route motion file generator
# ---------------------------------------------------------------------------

def get_road_route(start_coords, end_coords, api_key, log):
    """Call Google Directions API. Returns (waypoints, duration_sec) or (None, None)."""
    if not api_key or api_key == "YOUR_Maps_API_KEY_HERE":
        log.log("No Google API key — falling back to straight-line route.")
        return None, None
    try:
        url = "https://maps.googleapis.com/maps/api/directions/json"
        params = {
            "origin":      f"{start_coords[0]},{start_coords[1]}",
            "destination": f"{end_coords[0]},{end_coords[1]}",
            "mode":        "driving",
            "key":         api_key,
        }
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "OK":
            log.log(f"Directions API error: {data.get('status')} — falling back to straight line.")
            return None, None
        route    = data["routes"][0]
        leg      = route["legs"][0]
        duration = leg["duration"]["value"]
        polyline = route["overview_polyline"]["points"]
        # Decode polyline
        coords = []
        index = 0; lat = 0; lng = 0
        encoded = polyline
        while index < len(encoded):
            result = 0; shift = 0
            while True:
                b = ord(encoded[index]) - 63; index += 1
                result |= (b & 0x1F) << shift; shift += 5
                if b < 0x20: break
            dlat = ~(result >> 1) if result & 1 else result >> 1; lat += dlat
            result = 0; shift = 0
            while True:
                b = ord(encoded[index]) - 63; index += 1
                result |= (b & 0x1F) << shift; shift += 5
                if b < 0x20: break
            dlng = ~(result >> 1) if result & 1 else result >> 1; lng += dlng
            coords.append((lat / 1e5, lng / 1e5))
        log.log(f"Road route: {len(coords)} waypoints, drive time: {duration}s ({duration//60}m {duration%60}s)")
        return coords, duration
    except Exception as e:
        log.log(f"Directions API failed: {e} — falling back to straight line.")
        return None, None


def generate_route_motion_file(start_coords, end_coords, start_alt,
                                end_alt, duration_seconds, log: LogBuffer,
                                api_key: str = None,
                                use_roads: bool = True,
                                route_cache: list = None) -> str | None:
    """Generate a 10Hz LLH motion CSV for gps-sdr-sim.
    If api_key is provided and use_roads=True, uses Google Directions for road-following.
    route_cache: a mutable list [cache_dict] shared with caller for caching.
    Falls back to straight-line interpolation on failure.
    """
    import math as _math
    MAX_ROUTE_SPEED_MS = 44.7  # 100 mph in m/s

    if duration_seconds <= 0:
        log.log("Error: Duration for route must be positive.")
        return None

    waypoints = None

    if use_roads and api_key and api_key != "YOUR_Maps_API_KEY_HERE":
        start_key = (round(start_coords[0], 5), round(start_coords[1], 5))
        end_key   = (round(end_coords[0],   5), round(end_coords[1],   5))
        cached = route_cache[0] if route_cache and route_cache[0] else None
        if cached and cached.get('start') == start_key and cached.get('end') == end_key:
            waypoints = cached['waypoints']
            log.log(f"Road route (cached): {len(waypoints)} waypoints")
        else:
            waypoints, real_duration = get_road_route(start_coords, end_coords, api_key, log)
            if waypoints:
                if route_cache is not None:
                    route_cache[0] = {'start': start_key, 'end': end_key,
                                      'waypoints': waypoints, 'duration': real_duration}
                log.log(f"Road-following route: {len(waypoints)} waypoints")
            else:
                if route_cache is not None:
                    route_cache[0] = None
                log.log("Road routing unavailable — using straight line.")

    hgt1, hgt2 = float(start_alt), float(end_alt)
    num_points  = max(2, int(duration_seconds * 10))

    os.makedirs(os.path.dirname(TEMP_ROUTE_MOTION_FILE), exist_ok=True)
    try:
        with open(TEMP_ROUTE_MOTION_FILE, "w", newline="") as f:
            w = csv.writer(f)

            if waypoints and len(waypoints) >= 2:
                # ── Road-following mode ──────────────────────────────────────
                cum_dist = [0.0]
                for i in range(1, len(waypoints)):
                    lat1r = _math.radians(waypoints[i-1][0]); lon1r = _math.radians(waypoints[i-1][1])
                    lat2r = _math.radians(waypoints[i][0]);   lon2r = _math.radians(waypoints[i][1])
                    dlat = lat2r - lat1r; dlon = lon2r - lon1r
                    a = _math.sin(dlat/2)**2 + _math.cos(lat1r)*_math.cos(lat2r)*_math.sin(dlon/2)**2
                    cum_dist.append(cum_dist[-1] + 6371000.0 * 2 * _math.asin(_math.sqrt(a)))
                total_dist = cum_dist[-1]

                max_dist = duration_seconds * MAX_ROUTE_SPEED_MS
                if total_dist > max_dist:
                    trunc_idx = len(cum_dist) - 1
                    for j in range(1, len(cum_dist)):
                        if cum_dist[j] >= max_dist:
                            trunc_idx = j
                            break
                    waypoints  = waypoints[:trunc_idx + 1]
                    cum_dist   = cum_dist[:trunc_idx + 1]
                    total_dist = cum_dist[-1]
                    covered_km = total_dist / 1000.0
                    log.log(f"WARNING: Route too long for {duration_seconds}s at 100 mph max. "
                            f"Truncated to {covered_km:.1f} km ({covered_km*0.621:.1f} mi). "
                            f"Use Real Drive Time or pick a shorter route.")

                for i in range(num_points):
                    t        = i * 0.1
                    progress = min(t / duration_seconds, 1.0) if duration_seconds > 0 else 0
                    target_d = progress * total_dist
                    seg = 0
                    for j in range(1, len(cum_dist)):
                        if cum_dist[j] >= target_d:
                            seg = j - 1
                            break
                    else:
                        seg = len(waypoints) - 2
                    seg_len = cum_dist[seg+1] - cum_dist[seg]
                    r   = (target_d - cum_dist[seg]) / seg_len if seg_len > 0 else 0.0
                    lat = waypoints[seg][0] + r * (waypoints[seg+1][0] - waypoints[seg][0])
                    lon = waypoints[seg][1] + r * (waypoints[seg+1][1] - waypoints[seg][1])
                    hgt = hgt1 + progress * (hgt2 - hgt1)
                    w.writerow([f"{t:.1f}", f"{lat:.7f}", f"{lon:.7f}", f"{hgt:.1f}"])
                log.log(f"Road-following motion file written ({num_points} points, {duration_seconds}s)")

            else:
                # ── Straight-line fallback ───────────────────────────────────
                lat1, lon1 = start_coords
                lat2, lon2 = end_coords

                def _hav(a, b):
                    la1,lo1 = _math.radians(a[0]), _math.radians(a[1])
                    la2,lo2 = _math.radians(b[0]), _math.radians(b[1])
                    dlat = la2-la1; dlon = lo2-lo1
                    h = _math.sin(dlat/2)**2 + _math.cos(la1)*_math.cos(la2)*_math.sin(dlon/2)**2
                    return 6371000.0 * 2 * _math.asin(_math.sqrt(h))

                sl_dist = _hav(start_coords, end_coords)
                max_dist_sl = MAX_ROUTE_SPEED_MS * duration_seconds
                if sl_dist > max_dist_sl:
                    ratio = max_dist_sl / sl_dist
                    lat2 = lat1 + ratio * (lat2 - lat1)
                    lon2 = lon1 + ratio * (lon2 - lon1)
                    hgt2 = hgt1 + ratio * (hgt2 - hgt1)
                    km = max_dist_sl / 1000.0
                    log.log(f"WARNING: Straight-line truncated to {km:.1f} km ({km*0.621:.1f} mi) at 100 mph max.")

                for i in range(num_points):
                    t_ratio = i / (num_points - 1) if num_points > 1 else 0
                    t = i * 0.1
                    lat = lat1 + t_ratio * (lat2 - lat1)
                    lon = lon1 + t_ratio * (lon2 - lon1)
                    hgt = hgt1 + t_ratio * (hgt2 - hgt1)
                    w.writerow([f"{t:.1f}", f"{lat:.7f}", f"{lon:.7f}", f"{hgt:.1f}"])
                log.log(f"Straight-line motion file written ({num_points} points)")

        log.log(f"Route motion file: {os.path.basename(TEMP_ROUTE_MOTION_FILE)}")
        return TEMP_ROUTE_MOTION_FILE
    except Exception as e:
        log.log(f"Error writing route motion file: {e}")
        return None



# ---------------------------------------------------------------------------
# OS command runner  (no UI deps – log via LogBuffer)
# ---------------------------------------------------------------------------

def run_os_command(cmd_list: list, timeout: int, description: str,
                   log: LogBuffer) -> tuple[bool, str, str]:
    log.log(f"Executing: {description} -> {' '.join(shlex.quote(a) for a in cmd_list)}")
    try:
        p = subprocess.run(cmd_list, check=True, capture_output=True,
                           text=True, timeout=timeout)
        out = p.stdout.strip() if p.stdout else ""
        err = p.stderr.strip() if p.stderr else ""
        log.log(f"{description} OK." + (f" stdout={out}" if out else "") +
                (f" stderr={err}" if err else ""))
        return True, out, err
    except FileNotFoundError:
        msg = f"ERROR: '{cmd_list[0]}' not found for '{description}'."
        log.log(msg); return False, "", msg
    except subprocess.TimeoutExpired:
        msg = f"ERROR: '{description}' timed out after {timeout}s."
        log.log(msg); return False, "", msg
    except subprocess.CalledProcessError as e:
        out = e.stdout.strip() if e.stdout else ""
        err = e.stderr.strip() if e.stderr else ""
        msg = (f"ERROR: '{description}' failed (code {e.returncode})."
               + (f" stdout={out}" if out else "")
               + (f" stderr={err}" if err else ""))
        log.log(msg); return False, out, msg
    except Exception as e:
        msg = f"ERROR: Unexpected error in '{description}': {e}"
        log.log(msg); return False, "", msg


# ---------------------------------------------------------------------------
# SpooferCore  – the single shared runtime object
# ---------------------------------------------------------------------------

class SpooferCore:
    """
    Owns all mutable runtime state and all operations.
    Both the tkinter GUI and Flask web app hold a reference to ONE instance.
    Thread-safe for all state flags.

    UI callbacks:
        on_state_change()  – called whenever a flag changes (button refresh etc.)
        on_download_progress(downloaded_bytes, total_bytes)
        on_transfer_done(success: bool, message: str)
    All callbacks are optional; pass None to skip.
    """

    def __init__(self):
        self.config  = load_config()
        self.log     = LogBuffer()
        self._lock   = threading.Lock()

        # --- Runtime flags ---
        self.running                      = False
        self.is_looping_active            = False
        self.is_manual_blast_initial_phase = False
        self.intended_loop_after_blast    = False
        self.auto_blast_active_phase      = False
        self.auto_blast_enabled           = self.config.get("auto_blast_enabled", False)
        self.auto_blast_original_gain     = None
        self.auto_blast_original_loop     = None

        self.gps_sim_proc                 = None   # subprocess.Popen for local gen
        self.proc                         = None   # subprocess.Popen for hackrf_transfer
        self.remote_generation_in_progress = False

        self.transfer_in_progress         = False
        self.custom_transfer_in_progress  = False
        self.ephemeris_update_running     = False

        self.current_sim_gain_db          = None

        # Location state
        self.latlon        = (self.config.get("latitude"),  self.config.get("longitude"))
        self.altitude      = self.config.get("altitude")
        self.start_latlon  = tuple(self.config.get("start_latlon", [None, None]))
        self.start_altitude = self.config.get("start_altitude")
        self.end_latlon    = tuple(self.config.get("end_latlon",   [None, None]))
        self.end_altitude  = self.config.get("end_altitude")

        # Route cache — avoids repeat Directions API calls for same route
        self._road_route_cache          = None

        # Playback
        self.motion_playback_timeline   = None
        self.active_signal_duration_sec = 0
        self.playback_start_time        = None
        self.map_playback_center_latlon = None

        # UI-provided callback hooks (set by each UI after instantiation)
        self.on_state_change        = None   # fn()
        self.on_download_progress   = None   # fn(downloaded, total)
        self.on_transfer_done       = None   # fn(success, message)

        # Auto-blast timer handle (UI-layer sets this via schedule_auto_blast)
        self._auto_blast_cancel_fn  = None   # fn() to cancel pending timer

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fire_state_change(self):
        if self.on_state_change:
            try:
                self.on_state_change()
            except Exception:
                pass

    def _maps_api_key(self):
        k = self.config.get("Maps_api_key", "")
        return k if k and k != "YOUR_Maps_API_KEY_HERE" else None

    # ------------------------------------------------------------------
    # State query
    # ------------------------------------------------------------------

    def is_any_operation_active(self, exclude_generate=False,
                                 exclude_remote_generate=False,
                                 exclude_spoof_loop=False,
                                 exclude_transfer_sim=False,
                                 exclude_transfer_custom=False,
                                 exclude_ephem_update=False,
                                 exclude_auto_blast=False) -> bool:
        gen    = (not exclude_generate)        and isinstance(self.gps_sim_proc, subprocess.Popen) and self.gps_sim_proc.poll() is None
        rgen   = (not exclude_remote_generate) and self.remote_generation_in_progress
        spoof  = (not exclude_spoof_loop)      and self.running and not self.auto_blast_active_phase
        tsim   = (not exclude_transfer_sim)    and self.transfer_in_progress
        tcust  = (not exclude_transfer_custom) and self.custom_transfer_in_progress
        ephem  = (not exclude_ephem_update)    and self.ephemeris_update_running
        blast  = (not exclude_auto_blast)      and self.auto_blast_active_phase
        return gen or rgen or spoof or tsim or tcust or ephem or blast

    def get_status_dict(self) -> dict:
        """Snapshot of all relevant state for polling UIs."""
        sim_file_size = 0
        if os.path.exists(SIM_OUTPUT):
            try:
                sim_file_size = os.path.getsize(SIM_OUTPUT)
            except Exception:
                pass

        return {
            "running":                       self.running,
            "is_looping":                    self.is_looping_active,
            "is_blast_phase":                self.is_manual_blast_initial_phase,
            "auto_blast_active":             self.auto_blast_active_phase,
            "auto_blast_enabled":            self.auto_blast_enabled,
            "generating":                    isinstance(self.gps_sim_proc, subprocess.Popen) and self.gps_sim_proc.poll() is None,
            "remote_generating":             self.remote_generation_in_progress,
            "transfer_in_progress":          self.transfer_in_progress,
            "custom_transfer_in_progress":   self.custom_transfer_in_progress,
            "ephemeris_update_running":      self.ephemeris_update_running,
            "current_gain_db":               self.current_sim_gain_db,
            "config_gain_db":                self.config.get("gain", 15),
            "frequency_hz":                  self.config.get("frequency_hz", int(DEFAULT_FREQ_HZ_STR)),
            "duration_sec":                  self.config.get("duration", 60),
            "blast_duration_sec":            self.config.get("blast_duration_sec", 3),
            "auto_blast_interval_min":       self.config.get("auto_blast_interval_min", 5),
            "location_mode":                 self.config.get("location_mode", "Static (Address Lookup)"),
            "latitude":                      self.latlon[0],
            "longitude":                     self.latlon[1],
            "altitude":                      self.altitude,
            "start_latlon":                  list(self.start_latlon),
            "end_latlon":                    list(self.end_latlon),
            "address":                       self.config.get("address", ""),
            "start_address":                 self.config.get("start_address", ""),
            "end_address":                   self.config.get("end_address", ""),
            "motion_file_path":              self.config.get("motion_file_path", ""),
            "sim_output_exists":             os.path.exists(SIM_OUTPUT),
            "sim_file_size_bytes":           sim_file_size,
            "map_type":                      self.config.get("map_type", "roadmap"),
            "map_zoom":                      self.config.get("map_zoom", 14),
            "map_playback_latlon":           self.map_playback_center_latlon,
            "remote_server_url":             self.config.get("remote_server_url", DEFAULT_REMOTE_SERVER_URL),
        }

    # ------------------------------------------------------------------
    # Config updates (called from either UI)
    # ------------------------------------------------------------------

    def update_gain(self, db: int):
        db = max(0, min(47, int(db)))
        self.config["gain"] = db
        save_config(self.config)
        self.log.log(f"Gain set to {db}dB.")
        if self.auto_blast_active_phase:
            self.log.log("Will apply after auto-blast cycle.")
            self.auto_blast_original_gain = db
            return
        if self.running and self.proc and not self.is_manual_blast_initial_phase:
            self.log.log("Gain changed while running – restarting HackRF.")
            was_looping = self.is_looping_active
            self._stop_hackrf_proc()
            self._launch_hackrf(loop=was_looping, gain_override=db)
        self._fire_state_change()

    def update_frequency(self, hz: int):
        hz = int(hz)
        self.config["frequency_hz"] = hz
        save_config(self.config)
        self.log.log(f"Frequency set to {hz/1e6:.3f} MHz.")
        if self.running and self.proc and not self.is_manual_blast_initial_phase and not self.auto_blast_active_phase:
            self.log.log("Frequency changed while running – restarting HackRF.")
            was_looping = self.is_looping_active
            self._stop_hackrf_proc()
            self._launch_hackrf(loop=was_looping)
        self._fire_state_change()

    def update_duration(self, sec: int):
        self.config["duration"] = int(sec)
        save_config(self.config)

    def update_blast_duration(self, sec: int):
        self.config["blast_duration_sec"] = int(sec)
        save_config(self.config)

    def update_auto_blast_interval(self, minutes: int):
        self.config["auto_blast_interval_min"] = int(minutes)
        save_config(self.config)

    def update_map_type(self, map_type: str):
        self.config["map_type"] = map_type
        save_config(self.config)

    def update_map_zoom(self, zoom: int):
        self.config["map_zoom"] = int(zoom)
        save_config(self.config)

    def set_auto_blast_enabled(self, enabled: bool):
        self.auto_blast_enabled = enabled
        self.config["auto_blast_enabled"] = enabled
        save_config(self.config)
        self.log.log(f"Auto Blast {'enabled' if enabled else 'disabled'}.")
        self._fire_state_change()

    def set_use_roads(self, enabled: bool):
        self.config["use_roads"] = enabled
        save_config(self.config)
        self.log.log(f"Follow Roads {'enabled' if enabled else 'disabled'}.")

    def update_remote_server_url(self, url: str):
        self.config["remote_server_url"] = url.replace("/generate", "").rstrip("/")
        save_config(self.config)

    # ------------------------------------------------------------------
    # Address lookup
    # ------------------------------------------------------------------

    def lookup_static_address(self, address: str) -> dict:
        """Geocode address and fetch elevation. Returns result dict."""
        self.log.log(f"Lookup (Static): {address}")
        lat, lon = geocode_address(address)
        if lat is None:
            self.log.log("Nominatim geocode failed.")
            return {"ok": False, "error": "Geocode failed."}
        self.latlon = (lat, lon)
        self.log.log(f"Found: {lat:.4f}, {lon:.4f}")
        alt = get_elevation(lat, lon, self._maps_api_key())
        self.altitude = round(alt, 1) if alt is not None else None
        self.log.log(f"Altitude: {self.altitude}m" if self.altitude else "Altitude lookup failed.")
        self.map_playback_center_latlon = None
        self.config.update({"address": address, "latitude": lat,
                             "longitude": lon, "altitude": self.altitude})
        save_config(self.config)
        self._fire_state_change()
        return {"ok": True, "lat": lat, "lon": lon, "altitude": self.altitude}

    def lookup_start_address(self, address: str) -> dict:
        self.log.log(f"Lookup Start: {address}")
        lat, lon = geocode_address(address)
        if lat is None:
            self.log.log("Start geocode failed.")
            return {"ok": False, "error": "Geocode failed."}
        self.start_latlon = (lat, lon)
        if self.latlon[0] is None:
            self.latlon = (lat, lon)
        alt = get_elevation(lat, lon, self._maps_api_key())
        self.start_altitude = round(alt, 1) if alt is not None else None
        self.log.log(f"Start: {lat:.4f},{lon:.4f}  Alt:{self.start_altitude}")
        self.config.update({"start_address": address, "start_latlon": list(self.start_latlon),
                             "start_altitude": self.start_altitude})
        save_config(self.config)
        self._fire_state_change()
        return {"ok": True, "lat": lat, "lon": lon, "altitude": self.start_altitude}

    def lookup_end_address(self, address: str) -> dict:
        self.log.log(f"Lookup End: {address}")
        lat, lon = geocode_address(address)
        if lat is None:
            self.log.log("End geocode failed.")
            return {"ok": False, "error": "Geocode failed."}
        self.end_latlon = (lat, lon)
        alt = get_elevation(lat, lon, self._maps_api_key())
        self.end_altitude = round(alt, 1) if alt is not None else None
        self.log.log(f"End: {lat:.4f},{lon:.4f}  Alt:{self.end_altitude}")
        self.config.update({"end_address": address, "end_latlon": list(self.end_latlon),
                             "end_altitude": self.end_altitude})
        save_config(self.config)
        self._fire_state_change()
        return {"ok": True, "lat": lat, "lon": lon, "altitude": self.end_altitude}

    # ------------------------------------------------------------------
    # Map image
    # ------------------------------------------------------------------

    def get_map_image_bytes(self, width=600, height=300) -> bytes | None:
        """Return raw PNG bytes for the current map position."""
        loc_mode = self.config.get("location_mode", "Static (Address Lookup)")
        lat, lon = None, None

        if self.map_playback_center_latlon:
            lat, lon = self.map_playback_center_latlon
        elif "Static" in loc_mode:
            lat, lon = self.latlon
        elif "Route" in loc_mode:
            lat, lon = self.start_latlon if self.start_latlon[0] else self.latlon
        elif "User Motion" in loc_mode:
            if self.motion_playback_timeline:
                lat, lon = self.motion_playback_timeline[0][1], self.motion_playback_timeline[0][2]
            else:
                lat, lon = self.latlon

        if lat is None or lon is None:
            return None

        return download_static_map(
            lat, lon,
            zoom    = self.config.get("map_zoom", 14),
            width   = width,
            height  = height,
            maptype = self.config.get("map_type", "roadmap"),
            api_key = self._maps_api_key(),
        )

    # ------------------------------------------------------------------
    # Local generation
    # ------------------------------------------------------------------

    def generate(self) -> bool:
        """Start local gps-sdr-sim in a background thread. Returns True if started."""
        if self.is_any_operation_active(exclude_generate=True):
            self.log.log("Busy: another operation is active.")
            return False

        self.motion_playback_timeline   = None
        self.active_signal_duration_sec = 0
        self.map_playback_center_latlon = None

        eph = self._resolve_ephemeris()
        if not eph:
            return False

        loc_mode  = self.config.get("location_mode", "Static (Address Lookup)")
        duration  = self.config.get("duration", 60)
        args      = [GPS_SDR_SIM_EXECUTABLE, "-e", eph]

        # Align simulation time
        if os.path.exists(LATEST_TIME_PATH):
            with open(LATEST_TIME_PATH) as f:
                ts = f.read().strip()
            if ts:
                args += ["-t", ts]
                self.log.log(f"Sim time aligned to: {ts}")

        motion_file = None

        if "Static" in loc_mode:
            lat, lon = self.latlon
            alt = self.altitude if self.altitude is not None else DEFAULT_ALTITUDE_METERS
            if lat is None:
                self.log.log("Error: Lat/Lon not set for Static mode.")
                return False
            args += ["-l", f"{float(lat):.7f},{float(lon):.7f},{float(alt):.1f}"]
            self.log.log(f"Static: {lat:.4f},{lon:.4f} alt={alt:.1f}m")
            duration_cmd = str(duration)
            self.active_signal_duration_sec = duration

        elif "Route" in loc_mode:
            if self.start_latlon[0] is None or self.end_latlon[0] is None:
                self.log.log("Error: Route start/end not geocoded.")
                return False
            s_alt = self.start_altitude or DEFAULT_ALTITUDE_METERS
            e_alt = self.end_altitude   or DEFAULT_ALTITUDE_METERS
            motion_file = generate_route_motion_file(
                self.start_latlon, self.end_latlon, s_alt, e_alt, duration, self.log,
                api_key=self.config.get("Maps_api_key"),
                use_roads=self.config.get("use_roads", True),
                route_cache=[self._road_route_cache])
            if not motion_file:
                return False
            args += ["-x", motion_file]
            duration_cmd = str(min(duration, 3600))
            self.active_signal_duration_sec = int(duration_cmd)
            self._prepare_playback_timeline(motion_file, self.active_signal_duration_sec)

        else:  # User motion modes
            motion_file = self.config.get("motion_file_path", "")
            if not motion_file or not os.path.exists(motion_file):
                self.log.log("Error: Motion file not found.")
                return False
            if "ECEF" in loc_mode:
                args += ["-u", motion_file]
            elif "LLH" in loc_mode:
                args += ["-x", motion_file]
                self._prepare_playback_timeline(motion_file, min(duration, 3600))
            elif "NMEA" in loc_mode:
                args += ["-g", motion_file]
            duration_cmd = str(min(duration, 3600))
            self.active_signal_duration_sec = int(duration_cmd)

        args += ["-d", duration_cmd, "-b", "8", "-o", SIM_OUTPUT]
        os.makedirs(os.path.dirname(SIM_OUTPUT), exist_ok=True)
        self.log.log(f"CMD: {' '.join(shlex.quote(a) for a in args)}")

        temp_to_delete = motion_file if "Route" in loc_mode else None

        try:
            self.gps_sim_proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, universal_newlines=True)
            self._fire_state_change()
            threading.Thread(target=self._stream_watcher,
                             args=("GEN_OUT", self.gps_sim_proc.stdout),
                             daemon=True).start()
            threading.Thread(target=self._stream_watcher,
                             args=("GEN_ERR", self.gps_sim_proc.stderr),
                             daemon=True).start()
            threading.Thread(target=self._generate_finisher,
                             args=(self.gps_sim_proc, temp_to_delete),
                             daemon=True).start()
            return True
        except FileNotFoundError:
            self.log.log(f"Error: gps-sdr-sim not found at {GPS_SDR_SIM_EXECUTABLE}")
            self.gps_sim_proc = None
            self._fire_state_change()
            return False
        except Exception as e:
            self.log.log(f"Error starting generation: {e}\n{traceback.format_exc()}")
            self.gps_sim_proc = None
            self._fire_state_change()
            return False

    def _stream_watcher(self, label, stream):
        try:
            for line in iter(stream.readline, ""):
                if line:
                    self.log.log(line.strip())
            stream.close()
        except Exception as e:
            self.log.log(f"[{label}] stream error: {e}")

    def _generate_finisher(self, proc, temp_file=None):
        try:
            rc = proc.wait()
        except Exception as e:
            self.log.log(f"Error waiting for gen proc: {e}")
            rc = -1
        self.gps_sim_proc = None
        self.log.log(f"gps-sdr-sim finished. Exit code: {rc}.")
        if rc == 0 and os.path.exists(SIM_OUTPUT) and os.path.getsize(SIM_OUTPUT) > 0:
            self.log.log(f"Generation SUCCESS: {os.path.basename(SIM_OUTPUT)}")
        elif rc == 0:
            self.log.log("Generation: exit 0 but output file missing/empty.")
        else:
            self.log.log(f"Generation FAILED (code {rc}).")
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
                self.log.log(f"Temp file deleted: {os.path.basename(temp_file)}")
            except Exception as e:
                self.log.log(f"Error deleting temp file: {e}")
        self._fire_state_change()

    # ------------------------------------------------------------------
    # Remote generation
    # ------------------------------------------------------------------

    def remote_generate(self) -> bool:
        if self.is_any_operation_active(exclude_remote_generate=True):
            self.log.log("Busy: another operation is active.")
            return False
        self.remote_generation_in_progress = True
        self._fire_state_change()
        self.log.log("Starting remote generation...")
        threading.Thread(target=self._remote_gen_thread, daemon=True).start()
        return True

    def _remote_gen_thread(self):
        base_url = self.config.get("remote_server_url", DEFAULT_REMOTE_SERVER_URL)
        base_url = base_url.replace("/generate", "").rstrip("/")
        start    = time.time()
        try:
            self.log.log("Step 1: Submitting job...")
            job_id = self._submit_remote_job(base_url)
            self.log.log(f"Job ID: {job_id}")

            self.log.log("Step 2: Polling for status...")
            status_url = f"{base_url}/status/{job_id}"
            while time.time() - start < REMOTE_GEN_TOTAL_TIMEOUT_SEC:
                if not self.remote_generation_in_progress:
                    self.log.log("Remote gen cancelled.")
                    return
                r    = requests.get(status_url, timeout=10)
                r.raise_for_status()
                data = r.json()
                status = data.get("status")
                if status == "complete":
                    self.log.log("Job complete. Downloading...")
                    self._download_remote_file(
                        f"{base_url}/download/{job_id}",
                        data.get("file_size"))
                    self.log.log("Remote generation complete.")
                    break
                elif status == "failed":
                    raise RuntimeError(f"Server error: {data.get('error', 'unknown')}")
                else:
                    self.log.log(f"Status: {status}. Waiting...")
                    time.sleep(REMOTE_GEN_POLLING_INTERVAL_SEC)
            else:
                raise TimeoutError(f"Timed out after {REMOTE_GEN_TOTAL_TIMEOUT_SEC}s.")
        except Exception as e:
            self.log.log(f"Remote gen ERROR: {e}")
            print(traceback.format_exc())
        finally:
            self.remote_generation_in_progress = False
            self.log.log("Remote generation process finished.")
            self._fire_state_change()

    def _submit_remote_job(self, server_url: str) -> str:
        eph = self._resolve_ephemeris()
        if not eph:
            raise ValueError("No valid ephemeris file.")
        loc_mode = self.config.get("location_mode", "Static (Address Lookup)")
        duration = self.config.get("duration", 60)
        params   = {"duration": duration}
        if "Static" in loc_mode:
            lat, lon = self.latlon
            alt = self.altitude if self.altitude is not None else DEFAULT_ALTITUDE_METERS
            if lat is None:
                raise ValueError("Lat/Lon not set.")
            params["location"] = f"{float(lat):.7f},{float(lon):.7f},{float(alt):.1f}"
        else:
            raise ValueError("Remote generation supports Static mode only.")
        with open(eph, "rb") as f:
            eph_b64 = base64.b64encode(f.read()).decode()
        payload = {"params": params, "ephemeris_data": eph_b64,
                   "ephemeris_filename": os.path.basename(eph)}
        r = requests.post(f"{server_url}/generate", json=payload, timeout=30)
        r.raise_for_status()
        job_id = r.json().get("job_id")
        if not job_id:
            raise ValueError("No job_id in server response.")
        return job_id

    def _download_remote_file(self, url: str, total_size):
        os.makedirs(os.path.dirname(SIM_OUTPUT), exist_ok=True)
        downloaded = 0
        with requests.get(url, stream=True,
                          timeout=REMOTE_GEN_TOTAL_TIMEOUT_SEC) as r:
            r.raise_for_status()
            if not total_size:
                total_size = int(r.headers.get("content-length", 0))
            with open(SIM_OUTPUT, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if not self.remote_generation_in_progress:
                        self.log.log("Download cancelled.")
                        return
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size and self.on_download_progress:
                        try:
                            self.on_download_progress(downloaded, total_size)
                        except Exception:
                            pass
        self.log.log(f"Downloaded {downloaded/1e6:.1f} MB to {SIM_OUTPUT}")

    # ------------------------------------------------------------------
    # HackRF transmission
    # ------------------------------------------------------------------

    def start_sim(self) -> bool:
        """Sim button: blast then normal."""
        return self._start_hackrf(loop=False, is_blast=True)

    def start_loop(self) -> bool:
        """Loop button: blast then loop."""
        return self._start_hackrf(loop=True, is_blast=True)

    def _start_hackrf(self, loop=False, is_blast=False,
                      blast_duration_override=None, gain_override=None,
                      is_auto_blast_cycle=False) -> bool:
        if not is_blast and not is_auto_blast_cycle:
            if self.is_any_operation_active(exclude_spoof_loop=True):
                self.log.log("Busy: another operation active.")
                return False
        if self.gps_sim_proc and self.gps_sim_proc.poll() is None:
            self.log.log("Generation in progress – cannot start HackRF yet.")
            return False
        if not os.path.exists(SIM_OUTPUT) or os.path.getsize(SIM_OUTPUT) == 0:
            self.log.log("Error: gpssim.c8 not found or empty.")
            return False
        return self._launch_hackrf(loop=loop, is_blast=is_blast,
                                   blast_duration_override=blast_duration_override,
                                   gain_override=gain_override,
                                   is_auto_blast_cycle=is_auto_blast_cycle)

    def _launch_hackrf(self, loop=False, is_blast=False,
                       blast_duration_override=None, gain_override=None,
                       is_auto_blast_cycle=False) -> bool:
        gain = BLAST_GAIN_DB if is_blast else \
               (gain_override if gain_override is not None else self.config.get("gain", 15))
        self.current_sim_gain_db = gain

        blast_dur = self.config.get("blast_duration_sec", 3)
        if blast_duration_override is not None:
            blast_dur = blast_duration_override

        freq_hz  = str(self.config.get("frequency_hz", DEFAULT_FREQ_HZ_STR))
        rate_hz  = str(DEFAULT_SAMPLE_RATE_HZ)
        effective_loop = loop and not is_blast
        cmd = [HACKRF_TRANSFER_EXECUTABLE,
               "-t", SIM_OUTPUT,
               "-f", freq_hz,
               "-s", rate_hz,
               "-a", "1",
               "-x", str(gain)]
        if effective_loop:
            cmd.append("-R")

        self.running           = True
        self.is_looping_active = effective_loop
        if not is_auto_blast_cycle:
            self.is_manual_blast_initial_phase = is_blast
            if is_blast:
                self.intended_loop_after_blast = loop

        try:
            self.log.log(f"HackRF CMD: {' '.join(shlex.quote(a) for a in cmd)}")
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, universal_newlines=True)
            self.proc = proc
            self.log.log(f"HackRF PID: {proc.pid}")
            self._fire_state_change()

            if is_blast:
                # Schedule transition out of blast phase
                self._schedule_blast_transition(proc.pid, blast_dur, is_auto_blast_cycle)
            else:
                threading.Thread(target=self._hackrf_reader,
                                 args=(proc,), daemon=True).start()
                if self.auto_blast_enabled and not self.auto_blast_active_phase:
                    self._schedule_auto_blast()

            loc_mode = self.config.get("location_mode", "")
            if "Static" in loc_mode and not is_blast:
                lat, lon = self.latlon
                alt = self.altitude or DEFAULT_ALTITUDE_METERS
                if lat:
                    self.log.log(f"Static Sim: Gain:{gain}dB Lat:{lat:.4f} Lon:{lon:.4f} Alt:{alt:.1f}m")

            if not is_blast and ("Route" in loc_mode or "User Motion (LLH" in loc_mode):
                if self.motion_playback_timeline and self.active_signal_duration_sec > 0:
                    self.playback_start_time = time.time()

            return True

        except FileNotFoundError:
            self.log.log(f"Error: {HACKRF_TRANSFER_EXECUTABLE} not found.")
            self._reset_hackrf_state(is_auto_blast_cycle)
            return False
        except Exception as e:
            self.log.log(f"Error starting HackRF: {e}\n{traceback.format_exc()}")
            self._reset_hackrf_state(is_auto_blast_cycle)
            return False

    def _reset_hackrf_state(self, is_auto_blast_cycle=False):
        self.running = False
        self.is_looping_active = False
        self.is_manual_blast_initial_phase = False
        self.proc = None
        self.current_sim_gain_db = None
        if is_auto_blast_cycle:
            self.auto_blast_active_phase = False
        self._fire_state_change()

    def _hackrf_reader(self, proc):
        pid = proc.pid
        self.log.log(f"Reader thread started for PID {pid}.")
        try:
            if proc.stdout:
                for line in iter(proc.stdout.readline, ""):
                    if line:
                        self.log.log(line.strip())
                proc.stdout.close()
            rc = proc.wait()
            self.log.log(f"HackRF PID {pid} finished. Code: {rc}.")
        except Exception as e:
            self.log.log(f"HackRF reader error (PID {pid}): {e}")
        finally:
            if self.proc is proc and not self.is_manual_blast_initial_phase and not self.auto_blast_active_phase:
                self.running = False
                self.proc    = None
                self.is_looping_active = False
                self.current_sim_gain_db = None
                self._cancel_auto_blast()
                self._fire_state_change()
                self.log.log("HackRF session ended. State reset.")

    def _stop_hackrf_proc(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()
            except Exception as e:
                self.log.log(f"Error stopping HackRF: {e}")
        self.proc = None
        self.running = False
        self.is_looping_active = False

    # ------------------------------------------------------------------
    # Blast phase transitions
    # ------------------------------------------------------------------

    def _schedule_blast_transition(self, expected_pid, delay_sec, is_auto):
        """
        The UI layer must implement timer scheduling.
        This method is called by _launch_hackrf.
        The UI calls back into blast_transition_callback() after delay_sec.
        To allow both UIs to schedule timers differently, the core exposes
        `schedule_callback_after(seconds, fn)` which the UI must wire up.
        """
        def do_transition():
            self.blast_transition_callback(expected_pid, is_auto)

        if self._schedule_after_fn:
            self._schedule_after_fn(delay_sec, do_transition)
        else:
            # Fallback: blocking thread (works headless)
            threading.Timer(delay_sec, do_transition).start()

    # Injected by UI:  schedule_after_fn = lambda sec, fn: master.after(sec*1000, fn)
    _schedule_after_fn = None

    def blast_transition_callback(self, expected_pid, is_auto_blast_cycle):
        """Called by UI timer after blast phase ends."""
        current_pid = self.proc.pid if self.proc else None
        if expected_pid and current_pid != expected_pid:
            self.log.log(f"Blast transition aborted: PID mismatch "
                         f"(expected {expected_pid}, got {current_pid}).")
            return
        self.log.log(f"Blast phase ending (PID {expected_pid}).")
        self._stop_hackrf_proc()
        self.is_manual_blast_initial_phase = False

        if is_auto_blast_cycle:
            self.auto_blast_active_phase = False
            self._fire_state_change()
            self._finish_auto_blast_cycle()
        else:
            loop_after  = self.intended_loop_after_blast
            gain_after  = self.config.get("gain", 15)
            self.log.log(f"Transitioning from blast → loop={loop_after}, gain={gain_after}dB")
            self._launch_hackrf(loop=loop_after, is_blast=False, gain_override=gain_after)

    # ------------------------------------------------------------------
    # Auto blast cycle
    # ------------------------------------------------------------------

    def _schedule_auto_blast(self):
        self._cancel_auto_blast()
        if not self.auto_blast_enabled or not self.running:
            return
        if self.is_manual_blast_initial_phase or self.auto_blast_active_phase:
            return
        interval_sec = self.config.get("auto_blast_interval_min", 5) * 60
        self.log.log(f"Next auto blast in {interval_sec//60}m.")
        if self._schedule_after_fn:
            self._schedule_after_fn(interval_sec, self._initiate_auto_blast)
        else:
            t = threading.Timer(interval_sec, self._initiate_auto_blast)
            t.daemon = True
            t.start()
            self._auto_blast_cancel_fn = t.cancel

    def _cancel_auto_blast(self):
        if self._auto_blast_cancel_fn:
            try:
                self._auto_blast_cancel_fn()
            except Exception:
                pass
            self._auto_blast_cancel_fn = None

    def _initiate_auto_blast(self):
        if not self.auto_blast_enabled or not self.running:
            if self.running:
                self._schedule_auto_blast()
            return
        self.log.log("Initiating auto blast cycle...")
        self.auto_blast_active_phase       = True
        self.auto_blast_original_gain      = self.config.get("gain", 15)
        self.auto_blast_original_loop      = self.is_looping_active
        self._stop_hackrf_proc()
        self.running = False
        self._fire_state_change()
        self._launch_hackrf(loop=False, is_blast=True,
                            blast_duration_override=AUTO_BLAST_DURATION_SEC,
                            gain_override=BLAST_GAIN_DB,
                            is_auto_blast_cycle=True)

    def _finish_auto_blast_cycle(self):
        self.log.log("Auto blast cycle finished. Restoring normal operation.")
        gain = self.auto_blast_original_gain or self.config.get("gain", 15)
        loop = self.auto_blast_original_loop or False
        self.auto_blast_original_gain = None
        self.auto_blast_original_loop = None
        self._launch_hackrf(loop=loop, is_blast=False, gain_override=gain)

    # ------------------------------------------------------------------
    # Stop all
    # ------------------------------------------------------------------

    def stop_all(self):
        self.log.log("Stop all operations...")
        self._cancel_auto_blast()
        self.auto_blast_active_phase       = False
        self.is_manual_blast_initial_phase = False
        self.playback_start_time           = None

        # Stop local generation
        if isinstance(self.gps_sim_proc, subprocess.Popen) and self.gps_sim_proc.poll() is None:
            self.log.log(f"Stopping gps-sdr-sim (PID {self.gps_sim_proc.pid})...")
            try:
                self.gps_sim_proc.terminate()
                self.gps_sim_proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.gps_sim_proc.kill(); self.gps_sim_proc.wait()
            except Exception as e:
                self.log.log(f"Error stopping gen: {e}")
        self.gps_sim_proc = None

        # Cancel remote generation
        if self.remote_generation_in_progress:
            self.log.log("Cancelling remote generation...")
            self.remote_generation_in_progress = False

        # Stop HackRF
        self._stop_hackrf_proc()

        # Reset all flags
        self.running                     = False
        self.is_looping_active           = False
        self.transfer_in_progress        = False
        self.custom_transfer_in_progress = False
        self.ephemeris_update_running    = False
        self.current_sim_gain_db         = None

        self.log.log("All operations stopped.")
        self._fire_state_change()

    # ------------------------------------------------------------------
    # Ephemeris update
    # ------------------------------------------------------------------

    def update_ephemeris(self) -> bool:
        if self.ephemeris_update_running:
            self.log.log("Ephemeris update already running.")
            return False
        if self.is_any_operation_active(exclude_ephem_update=True):
            self.log.log("Busy: stop other operations first.")
            return False
        self.ephemeris_update_running = True
        self._fire_state_change()
        self.log.log("Starting ephemeris update...")
        threading.Thread(target=self._ephemeris_thread, daemon=True).start()
        return True

    def _ephemeris_thread(self):
        error = None
        try:
            from gpsdata import download_ephemeris
            path, ts = download_ephemeris()
            if path and os.path.exists(path) and ts:
                self.config["ephemeris_file"] = path
                save_config(self.config)
                self.log.log(f"Ephemeris updated: {os.path.basename(path)}  ({ts})")
            else:
                error = "Ephemeris download returned invalid data."
        except ImportError:
            error = "gpsdata module not found."
        except Exception as e:
            error = f"Ephemeris update failed: {e}\n{traceback.format_exc()}"
        finally:
            self.ephemeris_update_running = False
            if error:
                self.log.log(f"ERROR: {error}")
            self._fire_state_change()

    # ------------------------------------------------------------------
    # SD card transfer
    # ------------------------------------------------------------------

    def transfer_sim_to_sd(self) -> bool:
        """Transfer gpssim.c8 to HACKRF_SD_GPS_PATH."""
        if self.transfer_in_progress or self.custom_transfer_in_progress:
            self.log.log("Transfer already in progress.")
            return False
        if not os.path.exists(SIM_OUTPUT) or os.path.getsize(SIM_OUTPUT) == 0:
            self.log.log("Error: gpssim.c8 not found or empty.")
            return False
        self.transfer_in_progress = True
        self._fire_state_change()
        threading.Thread(target=self._transfer_thread,
                         args=(SIM_OUTPUT, False), daemon=True).start()
        return True

    def transfer_custom_to_sd(self, source_path: str) -> bool:
        """Transfer any file to HACKRF_SD_GPS_PATH."""
        if self.transfer_in_progress or self.custom_transfer_in_progress:
            self.log.log("Transfer already in progress.")
            return False
        if not os.path.exists(source_path) or os.path.getsize(source_path) == 0:
            self.log.log(f"Error: Source file not found or empty: {source_path}")
            return False
        self.custom_transfer_in_progress = True
        self._fire_state_change()
        threading.Thread(target=self._transfer_thread,
                         args=(source_path, True), daemon=True).start()
        return True

    def _transfer_thread(self, source_path: str, is_custom: bool):
        success_flags = {"df_check": False, "mkdir": False, "cp": False, "size_check": False}
        errors        = []
        dest_folder   = HACKRF_SD_GPS_PATH
        src_name      = os.path.basename(source_path)
        dest_path     = os.path.join(dest_folder, src_name)
        mount_point   = os.path.dirname(dest_folder.rstrip(os.sep))

        try:
            if not os.path.ismount(mount_point):
                raise Exception(f"SD mount point '{mount_point}' not found.")
            if not os.path.exists(source_path):
                raise Exception(f"Source file '{src_name}' does not exist.")

            ok, stdout, _ = run_os_command(
                ["df", "--block-size=1", "--output=avail", mount_point],
                DF_CMD_TIMEOUT, "Disk space check", self.log)
            if ok:
                avail = int(stdout.strip().splitlines()[-1].strip())
                if avail < os.path.getsize(source_path):
                    raise Exception("Not enough space on SD card.")
                success_flags["df_check"] = True
            else:
                raise Exception("Disk space check failed.")

            ok, _, _ = run_os_command(
                ["mkdir", "-p", dest_folder],
                MKDIR_CMD_TIMEOUT, "Create directory", self.log)
            if not ok:
                raise Exception("mkdir failed.")
            success_flags["mkdir"] = True

            ok, _, _ = run_os_command(
                ["cp", "-a", "-v", source_path, dest_folder],
                CP_CMD_TIMEOUT, "Copy file", self.log)
            if not ok:
                raise Exception("cp failed.")
            success_flags["cp"] = True

            if os.path.getsize(source_path) == os.path.getsize(dest_path):
                success_flags["size_check"] = True
                self.log.log("IMPORTANT: Manually unmount SD card from OS.")
            else:
                raise Exception("File size mismatch after copy.")

        except Exception as e:
            errors.append(str(e))

        success = all(success_flags.values())
        msg = f"Transfer of '{src_name}' {'succeeded' if success else 'failed'}."
        if errors:
            msg += "\nErrors:\n" + "\n".join(errors)
        self.log.log(msg)

        # Reset flags
        if is_custom:
            self.custom_transfer_in_progress = False
        else:
            self.transfer_in_progress = False
        self._fire_state_change()

        if self.on_transfer_done:
            try:
                self.on_transfer_done(success, msg)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Playback timeline (route / LLH motion map tracking)
    # ------------------------------------------------------------------

    def _prepare_playback_timeline(self, filepath: str, duration_sec: float):
        self.motion_playback_timeline   = []
        self.active_signal_duration_sec = float(duration_sec)
        if not filepath or not os.path.exists(filepath):
            self.motion_playback_timeline = None
            return
        try:
            with open(filepath, "r", newline="") as f:
                for row_num, row in enumerate(csv.reader(f)):
                    if len(row) >= 4:
                        try:
                            self.motion_playback_timeline.append(
                                (float(row[0]), float(row[1]), float(row[2]), float(row[3])))
                        except ValueError:
                            pass
                    elif len(row) >= 3:
                        try:
                            self.motion_playback_timeline.append(
                                (float(row[0]), float(row[1]), float(row[2]), DEFAULT_ALTITUDE_METERS))
                        except ValueError:
                            pass
            if not self.motion_playback_timeline:
                self.motion_playback_timeline = None
        except Exception as e:
            self.log.log(f"Error reading motion file: {e}")
            self.motion_playback_timeline = None

    def get_playback_position(self) -> tuple | None:
        """Return (lat, lon, alt, elapsed_sec) for current playback position, or None."""
        if not self.running or not self.motion_playback_timeline:
            return None
        if self.playback_start_time is None:
            return None
        elapsed = time.time() - self.playback_start_time
        t = elapsed
        if self.is_looping_active and self.active_signal_duration_sec > 0:
            t = elapsed % self.active_signal_duration_sec

        tl = self.motion_playback_timeline
        if t < tl[0][0]:
            return (tl[0][1], tl[0][2], tl[0][3], elapsed)
        if t >= tl[-1][0]:
            return (tl[-1][1], tl[-1][2], tl[-1][3], elapsed)
        for i in range(len(tl) - 1):
            if tl[i][0] <= t < tl[i+1][0]:
                return (tl[i][1], tl[i][2], tl[i][3], elapsed)
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_ephemeris(self) -> str | None:
        eph = self.config.get("ephemeris_file")
        if eph and os.path.exists(eph):
            return eph
        if os.path.exists(LATEST_FILE_PATH):
            with open(LATEST_FILE_PATH) as f:
                name = f.read().strip()
            candidate = os.path.join(EPHEMERIS_DIR, name)
            if os.path.exists(candidate):
                return candidate
        self.log.log("Error: No valid ephemeris file. Run Update Eph first.")
        return None


# ---------------------------------------------------------------------------
# Module-level singleton – both UIs import this
# ---------------------------------------------------------------------------
# Usage:
#   from gps_spoofer_core import core
#   core.generate()
#
core = SpooferCore()
