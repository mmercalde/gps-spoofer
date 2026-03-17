# -*- coding: utf-8 -*-
# GPS Spoofer GUI
# Version: 2025-06-07_09 (Modified by Gemini)
# Description: GUI for GPS signal generation with local and remote simulation capabilities.
# Changes:
# - (2025-06-07_09)
#   - Added a real-time progress bar and text label that appear during remote file download.
#   - The download progress is displayed in MB (e.g., "Downloading: 125.5 / 1500.0 MB").
#   - The progress bar is hidden after the download completes.
# - (2025-06-07_08)
#   - Added a status bar to the bottom of the GUI.
# - (Gemini Repair): Restored dynamic button color styling in _update_all_button_states.

import os
import json
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
from tkinter.font import nametofont
import subprocess
import threading
import shutil # shutil.which is used for command existence check
import shlex
import traceback
import time
import csv
import math # For pan calculations
import base64
import socket

# --- Check for dependencies before they are used ---
try:
    from PIL import Image, ImageTk
    import requests
    from geopy.geocoders import Nominatim
except ImportError as e:
    missing_lib = e.name
    error_msg = (f"Missing required Python library: '{missing_lib}'.\n\n"
                 f"Please install it by opening a terminal and running:\n"
                 f"pip install {missing_lib if missing_lib != 'PIL' else 'Pillow'}")
    print(error_msg)
    # Attempt to show a GUI error before exiting
    try:
        root_err = tk.Tk()
        root_err.withdraw()
        messagebox.showerror("Missing Library", error_msg)
        root_err.destroy()
    except tk.TclError:
        pass # If tkinter itself is missing or fails, the print statement is the only output.
    exit(1)


# --- Configuration and Path Constants ---
CONFIG_PATH = os.path.expanduser("~/gps_spoofer/config.json")
EPHEMERIS_DIR = os.path.expanduser("~/gps_spoofer/ephemeris")
LATEST_TIME_PATH = os.path.join(EPHEMERIS_DIR, "latest_time.txt")
LATEST_FILE_PATH = os.path.join(EPHEMERIS_DIR, "latest_file.txt")
SIM_OUTPUT = os.path.expanduser("~/gps_spoofer/sim_output/gpssim.c8")
TEMP_DIR = os.path.expanduser("~/gps_spoofer/temp")
TEMP_ROUTE_MOTION_FILE = os.path.join(TEMP_DIR, "temp_route_motion.csv")
GPS_SDR_SIM_EXECUTABLE = os.path.expanduser("~/gps-sdr-sim/gps-sdr-sim")
GPS_SDR_SIM_4CORE_EXECUTABLE = os.path.expanduser("~/gps-sdr-sim/gps-sdr-sim-4core")
HACKRF_TRANSFER_EXECUTABLE = "hackrf_transfer"
HACKRF_SD_GPS_PATH = "/media/michael/3402-CA84/GPS/" # User specific

# --- SUDO Credentials (No longer used by script for umount) ---
SUDO_USERNAME = "michael"
SUDO_PASSWORD = "password"

# --- Default and Operational Constants ---
DEFAULT_FREQ_HZ_STR = "1575420000"
DEFAULT_FREQ_MHZ = 1575.420
DEFAULT_SAMPLE_RATE_HZ = 2600000
DEFAULT_ALTITUDE_METERS = 100.0
DEFAULT_REMOTE_SERVER_URL = "http://45.32.131.224:5000" # Base URL now
REMOTE_GEN_POLLING_INTERVAL_SEC = 10
REMOTE_GEN_TOTAL_TIMEOUT_SEC = 600 # 10 minutes total wait time


# Command Timeouts (seconds)
DF_CMD_TIMEOUT = 10
MKDIR_CMD_TIMEOUT = 10
CP_CMD_TIMEOUT = 600
STAT_CMD_TIMEOUT = 10
MD5_CMD_TIMEOUT = 5
SYNC_CMD_TIMEOUT = 5
UNMOUNT_CMD_TIMEOUT = 180

TRANSFER_WATCHDOG_TIMEOUT_MS = (MKDIR_CMD_TIMEOUT + CP_CMD_TIMEOUT +
                                (STAT_CMD_TIMEOUT * 2) +
                                60) * 1000

BLAST_GAIN_DB = 47
GLO_JAM_FREQ_HZ     = 1602000000  # GLONASS L1 center
GLO_JAM_SAMPLE_RATE = 20000000    # 20 MHz covers all GLONASS channels
GLO_JAM_GAIN_DB     = 47          # Max gain for jam phase
DEFAULT_GLO_JAM_SEC = 20          # Default jam duration
AUTO_BLAST_DURATION_SEC = 5
MAP_UPDATE_INTERVAL_MS = 10000  # FIX: was 1000 — reduced to cut Static Maps API cost by 90%
MAX_TERMINAL_LINES = 50

Maps_API_KEY = None

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 1))
        IP = s.getsockname()[0]
    except Exception:
        try:
            IP = socket.gethostbyname(socket.gethostname())
        except Exception:
            IP = '127.0.0.1'
    finally:
        s.close()
    return IP

def load_config():
    global Maps_API_KEY
    default_config = {
        "Maps_api_key": "YOUR_Maps_API_KEY_HERE",
        "address": "", "latitude": None, "longitude": None, "altitude": None,
        "start_address": "", "start_latlon": [None, None], "start_altitude": None,
        "end_address": "", "end_latlon": [None, None], "end_altitude": None,
        "location_mode": "Static (Address Lookup)", "motion_file_path": "",
        "gain": 15, "duration": 60, "map_zoom": 14,
        "frequency_hz": int(DEFAULT_FREQ_HZ_STR), "blast_duration_sec": 3, "gen_cores": 1,
        "ephemeris_file": None, "map_type": "roadmap",
        "auto_blast_enabled": False, "auto_blast_interval_min": 5,
        "active_map_enabled": False,
        "remote_server_url": DEFAULT_REMOTE_SERVER_URL
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                config = json.load(f)
                for key, value in default_config.items():
                    config.setdefault(key, value)
                Maps_API_KEY = config.get("Maps_api_key")
                if Maps_API_KEY == "YOUR_Maps_API_KEY_HERE":
                    print("WARNING: Placeholder Google Maps API Key found.")
                return config
        except json.JSONDecodeError:
            print(f"Error decoding JSON from {CONFIG_PATH}. Using default config.")
            return default_config
    else:
        print(f"Config file {CONFIG_PATH} not found. Creating with default values.")
        save_config(default_config)
        return default_config

def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

def _decode_polyline(encoded: str) -> list:
    """Decode Google encoded polyline into list of (lat, lon) tuples."""
    coords = []; index = 0; lat = 0; lng = 0
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
    return coords


def get_road_route(start_coords, end_coords, api_key, log_fn=None):
    """Call Google Directions API. Returns (waypoints, duration_sec) or (None, None).
    log_fn: optional callable(str) to surface errors to the GUI terminal.
    """
    def _log(msg):
        print(msg)
        if log_fn:
            try: log_fn(msg)
            except Exception: pass
    if not api_key or api_key == "YOUR_Maps_API_KEY_HERE":
        _log("Road routing: no API key — falling back to straight line.")
        return None, None
    try:
        params = {
            "origin":      f"{start_coords[0]},{start_coords[1]}",
            "destination": f"{end_coords[0]},{end_coords[1]}",
            "mode":        "driving",
            "key":         api_key,
        }
        r = requests.get("https://maps.googleapis.com/maps/api/directions/json",
                         params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "OK":
            _log(f"Directions API error: {data.get('status')} — falling back to straight line.")
            return None, None
        leg      = data["routes"][0]["legs"][0]
        duration = leg["duration"]["value"]
        # Use per-step detailed polylines instead of overview_polyline —
        # overview is simplified and cuts corners/rivers. Step polylines
        # follow actual road geometry with full detail.
        waypoints = []
        for step in leg["steps"]:
            step_pts = _decode_polyline(step["polyline"]["points"])
            # Avoid duplicate points at step boundaries
            if waypoints and step_pts and step_pts[0] == waypoints[-1]:
                step_pts = step_pts[1:]
            waypoints.extend(step_pts)
        _log(f"Road route OK: {len(waypoints)} waypoints (detailed), real drive time {duration//60}m {duration%60}s")
        return waypoints, duration
    except Exception as e:
        _log(f"Directions API failed: {e} — falling back to straight line.")
        return None, None


def geocode_address(address):
    try:
        geolocator = Nominatim(user_agent="gps_spoofer_gui_nominatim", timeout=10)
        location = geolocator.geocode(address)
        if location:
            return location.latitude, location.longitude
    except Exception as e:
        print(f"Nominatim geocoding failed: {e}")
    return None, None

def get_elevation(lat, lon, api_key):
    if not api_key or api_key == "YOUR_Maps_API_KEY_HERE":
        return None
    if lat is None or lon is None:
        return None
    try:
        s_lat, s_lon = f"{float(lat):.7f}", f"{float(lon):.7f}"
        url = f"https://maps.googleapis.com/maps/api/elevation/json?locations={s_lat},{s_lon}&key={api_key}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data["status"] == "OK" and data["results"]:
            return float(data["results"][0]["elevation"])
        else:
            print(f"Google Elevation API Error: {data.get('status')}")
            return None
    except Exception as e:
        print(f"Google Elevation API request failed: {e}")
        return None

def download_static_map(lat, lon, zoom=14, width=600, height=300, maptype="roadmap"):
    if not Maps_API_KEY or Maps_API_KEY == "YOUR_Maps_API_KEY_HERE":
        return None
    if lat is None or lon is None: return None
    try:
        s_lat, s_lon = f"{float(lat):.7f}", f"{float(lon):.7f}"
        s_zoom, s_width, s_height = str(int(zoom)), str(int(width)), str(int(height))
        params = {
            "center": f"{s_lat},{s_lon}", "zoom": s_zoom, "size": f"{s_width}x{s_height}",
            "maptype": maptype, "markers": f"color:red|label:L|{s_lat},{s_lon}",
            "key": Maps_API_KEY
        }
        response = requests.get("https://maps.googleapis.com/maps/api/staticmap?", params=params, timeout=15)
        return response.content if response.status_code == 200 else None
    except Exception as e:
        print(f"Map download failed: {e}")
        return None

class GPSSpooferGUI:
    def __init__(self, master):
        self.master = master
        master.title("GPS Signal Simulator & Controller")
        self.config = load_config()
        self.running = False
        self.proc = None
        self.gps_sim_proc = None
        self.remote_generation_in_progress = False
        self.remote_gen_worker_thread = None
        self.is_looping_active = False
        self.ephemeris_update_running = False
        self.transfer_in_progress = False
        self.custom_transfer_in_progress = False
        self.transfer_watchdog_id = None
        self.custom_transfer_watchdog_id = None
        self.transfer_progress_timer_id = None
        self.transfer_start_time = 0
        self.current_transfer_filename = ""
        self.current_sim_gain_db = self.config.get("gain", 15)
        self.latlon = (self.config.get("latitude"), self.config.get("longitude"))
        self.altitude = self.config.get("altitude")
        self.start_latlon = self.config.get("start_latlon", [None, None])
        self.start_altitude = self.config.get("start_altitude")
        self.end_latlon = self.config.get("end_latlon", [None, None])
        self.end_altitude = self.config.get("end_altitude")
        self.map_image = None
        self.current_map_width, self.current_map_height = 250, 200
        self.is_manual_blast_initial_phase = False
        self.after_manual_blast_id = None
        self.intended_loop_state_after_manual_blast = False
        self.auto_blast_enabled = tk.BooleanVar(value=self.config.get("auto_blast_enabled", False))
        self.auto_blast_timer_id = None
        self.auto_blast_active_phase = False
        self.auto_blast_original_gain_temp = None
        self.auto_blast_original_loop_state_temp = None
        self.motion_playback_timeline = None
        self.active_signal_duration_sec = 0
        self.playback_start_time = None
        self.map_update_timer_id = None
        self.map_playback_center_latlon = None
        self.active_map_enabled = tk.BooleanVar(value=self.config.get("active_map_enabled", False))
        self.glo_jam_enabled = tk.BooleanVar(value=self.config.get("glo_jam_enabled", False))
        self.stream_mode = tk.BooleanVar(value=False)
        self.glo_jam_active = False
        self.glo_jam_proc = None
        self.glo_jam_duration_sec = self.config.get("glo_jam_duration_sec", DEFAULT_GLO_JAM_SEC)
        self._last_map_params = None        # FIX: cache (lat,lon,zoom,w,h,type) — skips Static Maps call if unchanged
        self._last_road_route_cache = None  # FIX: cache (start_key,end_key,waypoints,dur) — skips Directions call if same route

        self._setup_styles()
        self._setup_layout()
        self._create_action_buttons()
        self._create_control_panel()
        self._create_map_display()
        self._create_terminal()
        self._create_status_bar()

        self._update_all_button_states()
        self._update_status_bar()
        self.master.after(200, self.update_map)

        self.master.after(150, lambda: self.control_canvas.config(scrollregion=self.control_canvas.bbox("all")))
        if hasattr(self, 'gain_slider'): self.update_gain(str(self.gain_slider.get()))
        if hasattr(self, 'duration_slider'): self.update_duration_label(str(self.duration_slider.get()))
        if hasattr(self, 'zoom_slider'): self.update_map_on_zoom(str(self.zoom_slider.get()))
        if hasattr(self, 'freq_slider'): self.update_frequency(str(self.freq_slider.get()))
        if hasattr(self, 'blast_duration_slider'): self.update_blast_duration(str(self.blast_duration_slider.get()))
        if hasattr(self, 'auto_blast_interval_slider'): self.update_auto_blast_interval(str(self.auto_blast_interval_slider.get()))


    def _setup_styles(self):
        self.style = ttk.Style()
        styles = {
            "NonActive.TButton": {"foreground": "gray50", "background": "#EAEAEA"},
            "ActiveLoop.TButton": {"foreground": "white", "background": "#28A745"},
            "ActiveSim.TButton": {"foreground": "white", "background": "#007BFF"},
            "ActiveGenerate.TButton": {"foreground": "black", "background": "#FFC107"},
            "ActiveRemoteGenerate.TButton": {"foreground": "white", "background": "#ff6347"},
            "ActiveUpdate.TButton": {"foreground": "white", "background": "#663399"},
            "ActiveTransfer.TButton": {"foreground": "white", "background": "#dc3545"},
            "ActiveCustomTransfer.TButton": {"foreground": "white", "background": "#17A2B8"},
            "ActiveAutoBlast.TButton": {"foreground": "white", "background": "#FD7E14"},
            "ActiveMap.TButton": {"foreground": "white", "background": "#6f42c1"},
            "ActiveGloJam.TButton": {"foreground": "white", "background": "#e83e8c"}
        }
        for name, opts in styles.items():
            self.style.configure(name, **opts)
            self.style.map(name, background=[('disabled', opts["background"])], foreground=[('disabled', opts["foreground"])])

    def _setup_layout(self):
        self.main_app_frame = ttk.Frame(self.master, padding="2")
        self.main_app_frame.pack(fill=tk.BOTH, expand=True)
        self.main_app_frame.rowconfigure(1, weight=1)
        self.main_app_frame.rowconfigure(4, weight=1)
        self.main_app_frame.rowconfigure(6, weight=0) # Row for status bar
        self.main_app_frame.columnconfigure(0, weight=0, minsize=430)
        self.main_app_frame.columnconfigure(1, weight=1)

    def _create_action_buttons(self):
        action_buttons_frame = ttk.Frame(self.main_app_frame)
        action_buttons_frame.grid(row=0, column=0, columnspan=2, pady=(2, 5), sticky="ew")
        buttons = [
            ("Gen", self.generate), ("Remote Gen", self.remote_generate), ("Sim", self.start_spoofing),
            ("Loop", self.loop_signal), ("Stream", self.stream_signal), ("Auto Blast", self.toggle_auto_blast), ("GLO Jam", self.toggle_glo_jam), ("Update Eph", self.update_ephemeris), ("Sel Eph", self.select_ephemeris_file),
            ("To SD", self.prompt_and_transfer_to_hackrf), ("File->SD", self.prompt_and_transfer_custom_file),
            ("Stop", self.stop_spoofing), ("Quit", self.quit_gui)
        ]
        self.action_buttons = {}
        for i, (text, cmd) in enumerate(buttons):
            action_buttons_frame.columnconfigure(i, weight=1)
            btn = ttk.Button(action_buttons_frame, text=text, command=cmd)
            btn.grid(row=0, column=i, padx=2, sticky="ew")
            self.action_buttons[text.replace(" ", "_").lower()] = btn
        self._update_auto_blast_button_style()

    def _create_control_panel(self):
        pad_x = 2; pad_y = 1; slider_group_pad_x = 1; vertical_slider_height = 70

        control_scroll_area = ttk.Frame(self.main_app_frame)
        control_scroll_area.grid(row=1, column=0, sticky="nswe")
        control_scroll_area.rowconfigure(0, weight=1)
        control_scroll_area.columnconfigure(0, weight=1)

        self.control_canvas = tk.Canvas(control_scroll_area, borderwidth=0, highlightthickness=0)
        self.control_scrollbar = ttk.Scrollbar(control_scroll_area, orient="vertical", command=self.control_canvas.yview)
        self.control_canvas.configure(yscrollcommand=self.control_scrollbar.set)
        self.control_canvas.grid(row=0, column=0, sticky="nswe")
        self.control_scrollbar.grid(row=0, column=1, sticky="ns")

        self.scrollable_control_frame = ttk.Frame(self.control_canvas, padding="3")
        self.scrollable_control_frame.columnconfigure(0, weight=1)
        self.control_canvas_window = self.control_canvas.create_window((0, 0), window=self.scrollable_control_frame, anchor="nw")

        def configure_scrollable_frame_and_canvas(event):
            self.control_canvas.itemconfig(self.control_canvas_window, width=self.control_canvas.winfo_width())
            self.control_canvas.config(scrollregion=self.control_canvas.bbox("all"))
        self.scrollable_control_frame.bind("<Configure>", configure_scrollable_frame_and_canvas)
        self.control_canvas.bind("<Configure>", lambda e: self.control_canvas.itemconfig(self.control_canvas_window, width=e.width))

        self.loc_mode_frame = ttk.LabelFrame(self.scrollable_control_frame, text="Location Source")
        self.loc_mode_frame.grid(row=0, column=0, sticky="ew", pady=(0, pad_y))
        self.loc_mode_frame.columnconfigure(1, weight=1)
        ttk.Label(self.loc_mode_frame, text="Mode:").grid(row=0, column=0, sticky="w", padx=pad_x, pady=pad_y)
        self.location_mode_var = tk.StringVar(value=self.config.get("location_mode", "Static (Address Lookup)"))
        self.location_mode_combo = ttk.Combobox(self.loc_mode_frame, textvariable=self.location_mode_var, state="readonly", width=23)
        self.location_mode_combo['values'] = ("Static (Address Lookup)", "Route (Start/End Address)", "User Motion (ECEF .csv)", "User Motion (LLH .csv)", "User Motion (NMEA GGA)")
        self.location_mode_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=pad_x, pady=pad_y)
        self.location_mode_combo.bind("<<ComboboxSelected>>", self._on_location_mode_change)

        self.static_address_label = ttk.Label(self.loc_mode_frame, text="Address:")
        self.static_address_entry = ttk.Entry(self.loc_mode_frame, width=28)
        self.static_address_entry.insert(0, self.config.get("address", ""))
        self.static_lookup_button = ttk.Button(self.loc_mode_frame, text="Lookup", command=self.lookup_static_address)
        self.static_loc_label_title = ttk.Label(self.loc_mode_frame, text="Lat, Lon:")
        self.static_loc_label_value = ttk.Label(self.loc_mode_frame, text="-")
        self.static_alt_label_title = ttk.Label(self.loc_mode_frame, text="Altitude:")
        self.static_alt_label_value = ttk.Label(self.loc_mode_frame, text="-")
        self.update_static_labels()

        self.start_address_label = ttk.Label(self.loc_mode_frame, text="Start Addr:")
        self.start_address_var = tk.StringVar(value=self.config.get("start_address", ""))
        self.start_address_entry = ttk.Entry(self.loc_mode_frame, width=25, textvariable=self.start_address_var)
        self.start_lookup_button = ttk.Button(self.loc_mode_frame, text="LkUp Start", command=self._lookup_start_address)
        self.start_loc_label = ttk.Label(self.loc_mode_frame, text="Start Lat,Lon: -")
        self.start_alt_label = ttk.Label(self.loc_mode_frame, text="Start Alt: -")
        self.end_address_label = ttk.Label(self.loc_mode_frame, text="End Addr:")
        self.end_address_var = tk.StringVar(value=self.config.get("end_address", ""))
        self.end_address_entry = ttk.Entry(self.loc_mode_frame, width=25, textvariable=self.end_address_var)
        self.end_lookup_button = ttk.Button(self.loc_mode_frame, text="LkUp End", command=self._lookup_end_address)
        self.end_loc_label = ttk.Label(self.loc_mode_frame, text="End Lat,Lon: -")
        self.end_alt_label = ttk.Label(self.loc_mode_frame, text="End Alt: -")
        self.real_drive_time_button = ttk.Button(self.loc_mode_frame, text="⏱ Use Real Drive Time",
                                                  command=self._fetch_real_drive_time)
        self.route_time_label = ttk.Label(self.loc_mode_frame, text="", font=('TkDefaultFont', 7))
        self.use_roads_var = tk.BooleanVar(value=True)
        self.use_roads_check = ttk.Checkbutton(self.loc_mode_frame, text="Follow Roads",
                                                variable=self.use_roads_var)
        self.stream_mode_check = ttk.Checkbutton(self.loc_mode_frame, text="Stream Mode",
                                                  variable=self.stream_mode)
        self.update_route_labels()

        self.motion_file_label = ttk.Label(self.loc_mode_frame, text="Motion File:")
        self.motion_file_path_var = tk.StringVar(value=self.config.get("motion_file_path", ""))
        self.motion_file_entry = ttk.Entry(self.loc_mode_frame, textvariable=self.motion_file_path_var, state="readonly", width=25)
        self.browse_motion_button = ttk.Button(self.loc_mode_frame, text="Browse...", command=self._browse_motion_file)
        self.motion_file_info_label = ttk.Label(self.loc_mode_frame, text="Note: Motion files should be 10Hz.", font=('TkDefaultFont', 7))

        self._on_location_mode_change()

        sliders_area_frame = ttk.LabelFrame(self.scrollable_control_frame, text="Adjustments")
        sliders_area_frame.grid(row=1, column=0, sticky="ew", pady=(pad_y,0))
        for i in range(7): sliders_area_frame.columnconfigure(i, weight=1, minsize=42)
        self._create_sliders(sliders_area_frame, vertical_slider_height, pad_x, 0, slider_group_pad_x)

    def _create_sliders(self, parent, height, px, lpy, spx):
        def create_simple_slider(col_idx, text, from_, to, res, init_val, conf_key, cmd, fmt, s_name, l_name, p_btn, m_btn, step):
            group = ttk.Frame(parent); group.grid(row=0, column=col_idx, sticky="nswe", padx=spx, pady=(0, px))
            group.columnconfigure(1, weight=1)
            ttk.Label(group, text=text, font=('TkDefaultFont', 7)).grid(row=0, column=0, columnspan=3, sticky="w", padx=px, pady=(0, lpy))
            btn_m = ttk.Button(group, text="-", width=1, command=lambda s=s_name, v=-step: self._adjust_slider_value(getattr(self, s), v))
            btn_m.grid(row=1, column=1, sticky="s"); setattr(self, m_btn, btn_m)
            slider = tk.Scale(group, from_=from_, to=to, resolution=res, orient=tk.VERTICAL, command=cmd, length=height, showvalue=0, width=8)
            slider.set(self.config.get(conf_key, init_val)); slider.grid(row=2, column=1, sticky="ns"); setattr(self, s_name, slider)
            btn_p = ttk.Button(group, text="+", width=1, command=lambda s=s_name, v=step: self._adjust_slider_value(getattr(self, s), v))
            btn_p.grid(row=3, column=1, sticky="n"); setattr(self, p_btn, btn_p)
            val_label = ttk.Label(group, text=fmt.format(slider.get()), width=5, anchor="center", font=('TkDefaultFont', 7))
            val_label.grid(row=4, column=1, sticky="ew", padx=(1,px), pady=(lpy, 0)); setattr(self, l_name, val_label)

        create_simple_slider(0, "Gain:", 0, 47, 1, 15, "gain", self.update_gain, "{}dB", "gain_slider", "gain_label", "gain_plus_btn", "gain_minus_btn", 1)
        create_simple_slider(1, "Dur(s):", 10, 3600, 10, 60, "duration", self.update_duration_label, "{}s", "duration_slider", "duration_label", "duration_plus_btn", "duration_minus_btn", 10)
        create_simple_slider(2, "Zoom:", 1, 18, 1, 14, "map_zoom", self.update_map_on_zoom, "Z{}", "zoom_slider", "zoom_label", "zoom_plus_btn", "zoom_minus_btn", 1)

        freq_group = ttk.Frame(parent); freq_group.grid(row=0, column=3, sticky="nswe", padx=spx, pady=(0, px)); freq_group.columnconfigure(0, weight=1)
        ttk.Label(freq_group, text="Freq(MHz):", font=('TkDefaultFont', 7)).grid(row=0, column=0, sticky="w", padx=px, pady=(0, lpy))
        self.freq_minus_button_top = ttk.Button(freq_group, text="-", width=1, command=lambda: self._adjust_frequency_step(False)); self.freq_minus_button_top.grid(row=1, column=0, sticky="s")
        freq_val = float(self.config.get("frequency_hz", DEFAULT_FREQ_HZ_STR))/1e6
        self.freq_slider = tk.Scale(freq_group, from_=1560.0, to=1590.0, res=0.001, orient=tk.VERTICAL, command=self.update_frequency, length=height, showvalue=0, width=8)
        self.freq_slider.set(freq_val); self.freq_slider.grid(row=2, column=0, sticky="ns")
        self.freq_plus_button_bottom = ttk.Button(freq_group, text="+", width=1, command=lambda: self._adjust_frequency_step(True)); self.freq_plus_button_bottom.grid(row=3, column=0, sticky="n")
        self.freq_label = ttk.Label(freq_group, text=f"{freq_val:.3f}MHz", width=8, anchor="center", font=('TkDefaultFont', 7)); self.freq_label.grid(row=4, column=0, sticky="ew", padx=(1,px), pady=(lpy,0))

        create_simple_slider(4, "Blast(s):", 1, 10, 1, 3, "blast_duration_sec", self.update_blast_duration, "{}s", "blast_duration_slider", "blast_duration_label", "blast_plus_btn", "blast_minus_btn", 1)
        create_simple_slider(5, "Blast Int(m):", 1, 10, 1, 5, "auto_blast_interval_min", self.update_auto_blast_interval, "{}m", "auto_blast_interval_slider", "auto_blast_interval_label", "auto_blast_int_plus_btn", "auto_blast_int_minus_btn", 1)
        create_simple_slider(6, "Cores:", 1, 4, 1, 1, "gen_cores", self.update_gen_cores, "{}c", "gen_cores_slider", "gen_cores_label", "gen_cores_plus_btn", "gen_cores_minus_btn", 1)

    def _create_map_display(self):
        map_display_frame = ttk.Frame(self.main_app_frame, padding="1")
        map_display_frame.grid(row=1, column=1, rowspan=3, sticky="nsew", padx=(2, 0))
        map_display_frame.rowconfigure(0, weight=1)
        map_display_frame.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(map_display_frame, bg="lightgray", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", self.on_canvas_resize)

        map_controls_frame = ttk.Frame(map_display_frame)
        map_controls_frame.grid(row=1, column=0, sticky="ew")
        map_controls_frame.columnconfigure(0, weight=1); map_controls_frame.columnconfigure(3, weight=1)
        self.active_map_button = ttk.Button(map_controls_frame, text="Active Map", command=self.toggle_active_map)
        self.active_map_button.grid(row=0, column=0, padx=(1,2), sticky="ew")
        self._update_active_map_button_style()
        self.default_freq_button = ttk.Button(map_controls_frame, text="Default Freq", command=self.set_default_frequency, width=10)
        self.default_freq_button.grid(row=0, column=1, padx=(0,2), pady=(0,0), sticky="ew")
        ttk.Label(map_controls_frame, text="Map Type:").grid(row=0, column=2, padx=(3,0), sticky="e")
        self.map_type_var = tk.StringVar(value=self.config.get("map_type", "roadmap"))
        self.map_type_combo = ttk.Combobox(map_controls_frame, textvariable=self.map_type_var,
                                           values=["roadmap", "satellite", "hybrid", "terrain"],
                                           state="readonly", width=9)
        self.map_type_combo.grid(row=0, column=3, padx=(1,1), sticky="ew")
        self.map_type_combo.bind("<<ComboboxSelected>>", self._on_map_type_change)

    def _create_terminal(self):
        terminal_header_frame = ttk.Frame(self.main_app_frame)
        terminal_header_frame.grid(row=4, column=0, columnspan=2, sticky="ew", padx=2, pady=(2,0))
        terminal_header_frame.columnconfigure(0, weight=1)
        ttk.Label(terminal_header_frame, text="Output:").grid(row=0, column=0, sticky="w")
        self.clear_terminal_button = ttk.Button(terminal_header_frame, text="Clear Output", command=self.clear_terminal_output, width=12)
        self.clear_terminal_button.grid(row=0, column=1, sticky="e")

        self.terminal_output = scrolledtext.ScrolledText(self.main_app_frame, height=4, width=50, wrap=tk.WORD, relief=tk.SUNKEN, borderwidth=1)
        self.terminal_output.grid(row=5, column=0, columnspan=2, sticky="nsew", padx=2, pady=(0,2))
        self.terminal_output.configure(state='disabled')

    def _create_status_bar(self):
        self.status_bar_frame = ttk.Frame(self.main_app_frame, relief=tk.SUNKEN, borderwidth=1)
        self.status_bar_frame.grid(row=6, column=0, columnspan=2, sticky="ew")

        self.status_label = ttk.Label(self.status_bar_frame, text="No file loaded.", anchor=tk.W)
        self.status_label.pack(side=tk.LEFT, padx=5)

        self.download_label = ttk.Label(self.status_bar_frame, text="", anchor=tk.W)
        self.download_progress = ttk.Progressbar(self.status_bar_frame, orient='horizontal', length=200, mode='determinate')
        self.transfer_progressbar = ttk.Progressbar(self.status_bar_frame, mode='indeterminate', length=200)

    def _update_status_bar(self):
        if os.path.exists(SIM_OUTPUT):
            try:
                size_bytes = os.path.getsize(SIM_OUTPUT)
                if size_bytes > 1e9:
                    size_str = f"{size_bytes / 1e9:.2f} GB"
                elif size_bytes > 1e6:
                    size_str = f"{size_bytes / 1e6:.2f} MB"
                elif size_bytes > 1e3:
                    size_str = f"{size_bytes / 1e3:.2f} KB"
                else:
                    size_str = f"{size_bytes} B"
                self.status_label.config(text=f"File: {os.path.basename(SIM_OUTPUT)} | Size: {size_str}")
            except Exception as e:
                self.status_label.config(text=f"File: {os.path.basename(SIM_OUTPUT)} | Error reading size")
        else:
            self.status_label.config(text="File: gpssim.c8 not found.")
        self._update_all_button_states()

    def _show_download_progress(self):
        self.status_label.pack_forget()
        self.download_label.pack(side=tk.LEFT, padx=5)
        self.download_progress.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

    def _hide_download_progress(self):
        self.download_label.pack_forget()
        self.download_progress.pack_forget()
        self.status_label.pack(side=tk.LEFT, padx=5)

    def _update_download_progress(self, downloaded_bytes, total_bytes):
        self.download_progress['value'] = downloaded_bytes
        self.download_progress['maximum'] = total_bytes

        downloaded_mb = downloaded_bytes / 1e6
        total_mb = total_bytes / 1e6
        self.download_label.config(text=f"Downloading: {downloaded_mb:.1f} MB / {total_mb:.1f} MB")

    def update_static_labels(self):
        if self.latlon[0] is not None:
            self.static_loc_label_value.config(text=f"{self.latlon[0]:.4f},{self.latlon[1]:.4f}")
        if self.altitude is not None:
            self.static_alt_label_value.config(text=f"{self.altitude:.1f}m")

    def update_route_labels(self):
        if self.start_latlon[0] is not None:
            self.start_loc_label.config(text=f"Start: {self.start_latlon[0]:.4f},{self.start_latlon[1]:.4f}")
        if self.start_altitude is not None:
            self.start_alt_label.config(text=f"Start Alt: {self.start_altitude:.1f}m")
        if self.end_latlon[0] is not None:
            self.end_loc_label.config(text=f"End: {self.end_latlon[0]:.4f},{self.end_latlon[1]:.4f}")
        if self.end_altitude is not None:
            self.end_alt_label.config(text=f"End Alt: {self.end_altitude:.1f}m")

    # The rest of the class methods continue here...
    def clear_terminal_output(self):
        if hasattr(self, 'terminal_output') and self.terminal_output and self.terminal_output.winfo_exists():
            self.terminal_output.configure(state='normal')
            self.terminal_output.delete("1.0", tk.END)
            self.terminal_output.configure(state='disabled')
            print("Terminal output cleared by user.")

    def _update_active_map_button_style(self):
        if hasattr(self, 'active_map_button') and self.active_map_button.winfo_exists():
            if self.active_map_enabled.get(): self.active_map_button.config(style="ActiveMap.TButton")
            else: self.active_map_button.config(style="TButton")

    def toggle_active_map(self):
        self.active_map_enabled.set(not self.active_map_enabled.get())
        self.config["active_map_enabled"] = self.active_map_enabled.get(); save_config(self.config)
        self._update_active_map_button_style()
        self.add_to_terminal(f"Active Map {'Enabled' if self.active_map_enabled.get() else 'Disabled'}.")
        self.update_map()

    def _update_auto_blast_button_style(self):
        btn_key = "auto_blast"
        if hasattr(self, 'action_buttons') and btn_key in self.action_buttons and self.action_buttons[btn_key].winfo_exists():
            if self.auto_blast_enabled.get():
                self.action_buttons[btn_key].config(style="ActiveAutoBlast.TButton")
            else:
                self.action_buttons[btn_key].config(style="TButton")


    def stream_signal(self):
        """Generate IQ data and pipe directly to hackrf_transfer — no file write."""
        if self.is_any_operation_active(): 
            messagebox.showwarning("Busy", "Another operation is active. Please stop it first.")
            return
        self.add_to_terminal("Preparing stream...")

        # Build gps-sdr-sim args (same as generate but output to stdout)
        _cores = int(self.config.get("gen_cores", 1))
        _use_exec = GPS_SDR_SIM_4CORE_EXECUTABLE if _cores > 1 else GPS_SDR_SIM_EXECUTABLE
        import os as _os; _sim_env = _os.environ.copy(); _sim_env['GPSSIM_NTHREADS'] = str(_cores)
        args = [_use_exec]

        eph_to_use = self.config.get("ephemeris_file")
        if not eph_to_use or not os.path.exists(eph_to_use):
            if os.path.exists(LATEST_FILE_PATH):
                with open(LATEST_FILE_PATH, "r") as f: eph_to_use = os.path.join(EPHEMERIS_DIR, f.read().strip())
            else:
                messagebox.showerror("Error", "No valid ephemeris file."); return
        if not os.path.exists(eph_to_use):
            messagebox.showerror("Error", f"Ephemeris file not found."); return
        args.extend(["-e", eph_to_use])

        if os.path.exists(LATEST_TIME_PATH):
            with open(LATEST_TIME_PATH, "r") as tf:
                t_stamp = tf.read().strip()
            if t_stamp:
                args.extend(["-t", t_stamp])

        current_loc_mode = self.location_mode_var.get()
        duration_from_slider = self.duration_slider.get() if hasattr(self, 'duration_slider') else self.config.get("duration", 60)

        if "Static" in current_loc_mode:
            lat, lon = self.latlon; alt = self.altitude if self.altitude is not None else DEFAULT_ALTITUDE_METERS
            if lat is None or lon is None:
                messagebox.showerror("Error", "Lat/Lon not set."); return
            args.extend(["-l", f"{float(lat):.7f},{float(lon):.7f},{float(alt):.1f}"])
            duration_for_sim = str(duration_from_slider)
        elif "Route" in current_loc_mode:
            if self.start_latlon[0] is None or self.end_latlon[0] is None:
                messagebox.showerror("Error", "Route addresses not geocoded."); return
            start_alt = self.start_altitude if self.start_altitude is not None else DEFAULT_ALTITUDE_METERS
            end_alt = self.end_altitude if self.end_altitude is not None else DEFAULT_ALTITUDE_METERS
            motion_file = self._generate_route_motion_file(self.start_latlon, self.end_latlon, start_alt, end_alt, duration_from_slider, use_roads=self.use_roads_var.get())
            if not motion_file:
                messagebox.showerror("Error", "Failed to generate route CSV."); return
            args.extend(["-x", motion_file])
            duration_for_sim = str(min(duration_from_slider, 3600))
        else:
            motion_file = self.motion_file_path_var.get()
            if not motion_file or not os.path.exists(motion_file):
                messagebox.showerror("Error", "Motion file not found."); return
            if "ECEF" in current_loc_mode: args.extend(["-u", motion_file])
            elif "LLH" in current_loc_mode: args.extend(["-x", motion_file])
            elif "NMEA" in current_loc_mode: args.extend(["-g", motion_file])
            duration_for_sim = str(min(duration_from_slider, 3600))

        args.extend(["-d", duration_for_sim, "-b", "8", "-o", "-"])

        gain = int(self.config.get("gain", 43))
        freq_hz = int(self.config.get("frequency_hz", 1575420000))

        hackrf_cmd = [
            "hackrf_transfer", "-t", "/dev/stdin",
            "-f", str(freq_hz),
            "-s", "2600000",
            "-a", "1",
            "-x", str(gain),
            "-R"
        ]

        self.add_to_terminal(f"Stream CMD: {' '.join(shlex.quote(a) for a in args)}")
        self.add_to_terminal(f"HackRF CMD: {' '.join(hackrf_cmd)}")

        def _run_stream():
            try:
                gen_proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_sim_env)
                hackrf_proc = subprocess.Popen(hackrf_cmd, stdin=gen_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                gen_proc.stdout.close()
                self.proc = hackrf_proc
                self.running = True
                self.is_streaming = True
                self.master.after(0, self._update_all_button_states)
                self.add_to_terminal(f"Streaming... Gen PID={gen_proc.pid} HackRF PID={hackrf_proc.pid}")
                for line in iter(hackrf_proc.stdout.readline, ''):
                    if line.strip():
                        self.master.after(0, self.add_to_terminal, line.strip())
                hackrf_proc.wait()
                gen_proc.wait()
            except Exception as e:
                self.master.after(0, self.add_to_terminal, f"Stream error: {e}")
            finally:
                self.running = False
                self.is_streaming = False
                self.proc = None
                self.master.after(0, self._update_all_button_states)
                self.master.after(0, self.add_to_terminal, "Stream ended.")
                if hasattr(self, 'action_buttons') and 'stream' in self.action_buttons:
                    self.master.after(0, lambda: self.action_buttons['stream'].config(style='TButton'))

        import threading
        threading.Thread(target=_run_stream, daemon=True).start()


    def toggle_auto_blast(self):
        self.auto_blast_enabled.set(not self.auto_blast_enabled.get())
        self.config["auto_blast_enabled"] = self.auto_blast_enabled.get(); save_config(self.config)
        self._update_auto_blast_button_style()
        self.add_to_terminal(f"Auto Blast {'Enabled' if self.auto_blast_enabled.get() else 'Disabled'}.")
        if self.auto_blast_enabled.get() and self.running and not self.is_manual_blast_initial_phase and not self.auto_blast_active_phase:
            self._schedule_next_auto_blast()
        elif not self.auto_blast_enabled.get(): self._cancel_auto_blast_timer()

    def update_auto_blast_interval(self, val_str):
        minutes = int(float(val_str))
        if hasattr(self, 'auto_blast_interval_label') and self.auto_blast_interval_label.winfo_exists():
            self.auto_blast_interval_label.config(text=f"{minutes}m")
        self.config["auto_blast_interval_min"] = minutes; save_config(self.config)
        self.add_to_terminal(f"Auto Blast interval set to {minutes} minutes.")
        if self.auto_blast_enabled.get() and self.running and not self.is_manual_blast_initial_phase and not self.auto_blast_active_phase:
            self._schedule_next_auto_blast()

    def _schedule_next_auto_blast(self):
        self._cancel_auto_blast_timer()
        if self.auto_blast_enabled.get() and self.running and not self.is_manual_blast_initial_phase and not self.auto_blast_active_phase:
            interval_min = self.config.get("auto_blast_interval_min", 5)
            interval_ms = interval_min * 60 * 1000
            self.add_to_terminal(f"Scheduling next auto blast in {interval_min} minutes.")
            if self.master and self.master.winfo_exists():
                self.auto_blast_timer_id = self.master.after(interval_ms, self._initiate_auto_blast_cycle)

    def _cancel_auto_blast_timer(self):
        if self.auto_blast_timer_id is not None and self.master and self.master.winfo_exists():
            self.master.after_cancel(self.auto_blast_timer_id); self.auto_blast_timer_id = None

    def _initiate_auto_blast_cycle(self):
        if not self.auto_blast_enabled.get() or not self.running or self.is_manual_blast_initial_phase or self.auto_blast_active_phase:
            if self.auto_blast_enabled.get() and self.running: self._schedule_next_auto_blast()
            return
        self.add_to_terminal("Initiating auto blast cycle...")
        self.auto_blast_active_phase = True; self._update_all_button_states()
        self.auto_blast_original_gain_temp = self.config.get("gain", 15)
        self.auto_blast_original_loop_state_temp = self.is_looping_active
        current_proc_to_stop = self.proc
        current_proc_pid_to_stop = current_proc_to_stop.pid if current_proc_to_stop else None
        self.running = False; self.is_looping_active = False; self.proc = None
        if current_proc_to_stop and current_proc_to_stop.poll() is None:
            self.add_to_terminal(f"Pausing current transmission (PID: {current_proc_pid_to_stop}) for auto blast...")
            try:
                current_proc_to_stop.terminate(); current_proc_to_stop.wait(timeout=0.5)
            except subprocess.TimeoutExpired: current_proc_to_stop.kill(); current_proc_to_stop.wait()
            except Exception as e: self.add_to_terminal(f"Error stopping current tx (PID: {current_proc_pid_to_stop}) for auto blast: {e}")
        self.add_to_terminal(f"Auto Blasting at {BLAST_GAIN_DB}dB for {AUTO_BLAST_DURATION_SEC}s...")
        self._start_hackrf(loop=False, is_blast_phase_override=True, blast_duration_override_sec=AUTO_BLAST_DURATION_SEC, gain_override_db=BLAST_GAIN_DB, is_auto_blast_cycle=True)

    def _finish_auto_blast_cycle(self):
        self.add_to_terminal("Auto blast 5s phase finished. Reverting to normal operation...")
        self.auto_blast_active_phase = False
        original_gain = self.auto_blast_original_gain_temp
        original_loop = self.auto_blast_original_loop_state_temp
        self.auto_blast_original_gain_temp = None; self.auto_blast_original_loop_state_temp = None
        self.add_to_terminal(f"Restoring gain to {original_gain}dB and loop state: {original_loop}.")
        self._start_hackrf(loop=original_loop, is_blast_phase_override=False, gain_override_db=original_gain)

    def _on_map_type_change(self, event=None):
        new_map_type = self.map_type_var.get()
        self.config["map_type"] = new_map_type; save_config(self.config)
        self.update_map()

    def _prepare_playback_map_timeline(self, motion_filepath, signal_duration_sec):
        self.motion_playback_timeline = []
        self.active_signal_duration_sec = float(signal_duration_sec)
        current_loc_mode = self.location_mode_var.get()
        if not ("Route" in current_loc_mode or "User Motion (LLH .csv)" in current_loc_mode):
            self.motion_playback_timeline = None; return
        if not motion_filepath or not os.path.exists(motion_filepath):
            self.motion_playback_timeline = None; return
        try:
            with open(motion_filepath, 'r', newline='') as csvfile:
                reader = csv.reader(csvfile)
                for row_num, row in enumerate(reader):
                    if len(row) >= 4:
                        try:
                            time_val, lat_val, lon_val, alt_val = float(row[0]), float(row[1]), float(row[2]), float(row[3])
                            self.motion_playback_timeline.append((time_val, lat_val, lon_val, alt_val))
                        except ValueError as ve: print(f"Skipping row {row_num+1} in motion file '{os.path.basename(motion_filepath)}': invalid number format: {ve} - Row: {row}")
                    elif len(row) >= 3:
                        try:
                            time_val, lat_val, lon_val = float(row[0]), float(row[1]), float(row[2])
                            self.motion_playback_timeline.append((time_val, lat_val, lon_val, DEFAULT_ALTITUDE_METERS))
                            if row_num == 0: self.add_to_terminal(f"Note: Motion file '{os.path.basename(motion_filepath)}' missing altitude. Using default {DEFAULT_ALTITUDE_METERS}m.")
                        except ValueError as ve: print(f"Skipping row {row_num+1} in motion file (3-col fallback): invalid number format: {ve} - Row: {row}")
                    elif row: print(f"Skipping malformed row {row_num+1} in motion file: Expected 3 or 4 columns, got {len(row)}. Row: {row}")
            if not self.motion_playback_timeline:
                self.motion_playback_timeline = None
                self.add_to_terminal(f"Warning: No valid data points in motion file '{os.path.basename(motion_filepath)}'. Playback updates will not occur.")
        except Exception as e:
            self.motion_playback_timeline = None
            self.add_to_terminal(f"Error preparing playback timeline from '{os.path.basename(motion_filepath)}': {e}")
            print(f"Full error preparing map timeline: {traceback.format_exc()}")

    def _update_map_during_playback(self):
        if self.map_update_timer_id: self.master.after_cancel(self.map_update_timer_id); self.map_update_timer_id = None
        should_update_display = (self.running and self.motion_playback_timeline and
                                 self.playback_start_time is not None and not self.auto_blast_active_phase and
                                 ("Route" in self.location_mode_var.get() or "User Motion (LLH .csv)" in self.location_mode_var.get()))
        if not should_update_display:
            if self.active_map_enabled.get(): self.map_playback_center_latlon = None; self.update_map()
            return

        elapsed_time = time.time() - self.playback_start_time; current_signal_time = elapsed_time
        if self.is_looping_active and self.active_signal_duration_sec > 0: current_signal_time = elapsed_time % self.active_signal_duration_sec
        elif elapsed_time > self.active_signal_duration_sec:
            if self.motion_playback_timeline:
                last_point = self.motion_playback_timeline[0]
                for point_data in self.motion_playback_timeline:
                    if point_data[0] <= self.active_signal_duration_sec: last_point = point_data
                    else: break
                self.map_playback_center_latlon = (last_point[1], last_point[2])
                if self.active_map_enabled.get():
                    self.update_map()
                    display_gain_end = BLAST_GAIN_DB if self.is_manual_blast_initial_phase or self.auto_blast_active_phase else \
                                     (self.current_sim_gain_db if self.current_sim_gain_db is not None else self.config.get("gain", 15))
                    self.add_to_terminal(f"Playback End: T+{last_point[0]:<5.1f}s | Gain: {display_gain_end:<2}dB | Lat: {last_point[1]:<8.4f}, Lon: {last_point[2]:<9.4f}, Alt: {last_point[3]:<6.1f}m")
            self.map_playback_center_latlon = None; return

        found_lat, found_lon, found_alt = None, None, None
        if current_signal_time < self.motion_playback_timeline[0][0]: _, found_lat, found_lon, found_alt = self.motion_playback_timeline[0]
        elif current_signal_time >= self.motion_playback_timeline[-1][0]: _, found_lat, found_lon, found_alt = self.motion_playback_timeline[-1]
        else:
            for i in range(len(self.motion_playback_timeline) - 1):
                time_i, lat_i, lon_i, alt_i = self.motion_playback_timeline[i]
                time_i1, _, _, _ = self.motion_playback_timeline[i+1]
                if time_i <= current_signal_time < time_i1: found_lat, found_lon, found_alt = lat_i, lon_i, alt_i; break
        if found_lat is None and self.motion_playback_timeline: _, found_lat, found_lon, found_alt = self.motion_playback_timeline[0]

        if found_lat is not None and found_lon is not None and found_alt is not None:
            self.map_playback_center_latlon = (found_lat, found_lon)
            if self.active_map_enabled.get():
                self.update_map()
                display_gain_playback = BLAST_GAIN_DB if self.is_manual_blast_initial_phase or self.auto_blast_active_phase else \
                                       (self.current_sim_gain_db if self.current_sim_gain_db is not None else self.config.get("gain", 15))
                self.add_to_terminal(f"Playback: T+{current_signal_time:<5.1f}s | Gain: {display_gain_playback:<2}dB | Lat: {found_lat:<8.4f}, Lon: {found_lon:<9.4f}, Alt: {found_alt:<6.1f}m")
        elif self.active_map_enabled.get(): self.map_playback_center_latlon = None; self.update_map()
        if self.running and not self.auto_blast_active_phase:
            self.map_update_timer_id = self.master.after(MAP_UPDATE_INTERVAL_MS, self._update_map_during_playback)

    def _map_zoom_in(self):
        if hasattr(self, 'zoom_slider'):
            if self.zoom_slider.get() < self.zoom_slider.cget("to"): self.zoom_slider.set(self.zoom_slider.get() + 1)
    def _map_zoom_out(self):
        if hasattr(self, 'zoom_slider'):
            if self.zoom_slider.get() > self.zoom_slider.cget("from"): self.zoom_slider.set(self.zoom_slider.get() - 1)

    def _adjust_slider_value(self, slider_widget, change):
        current_val = slider_widget.get(); resolution = slider_widget.cget("resolution")
        new_val = current_val + change; from_ = slider_widget.cget("from"); to = slider_widget.cget("to")
        if resolution > 0:
            if abs(resolution) < 1:
                res_str = f"{resolution:.10f}".rstrip('0')
                if '.' in res_str: num_decimals = len(res_str.split('.')[1]); new_val = round(new_val, num_decimals)
                else: new_val = round(new_val / resolution) * resolution
            else: new_val = round(new_val)
        new_val = max(from_, min(new_val, to)); slider_widget.set(new_val)

    def _adjust_frequency_step(self, increment_param=True):
        current_val_mhz = self.freq_slider.get(); step = 0.001
        if increment_param: new_val_mhz = current_val_mhz + step
        else: new_val_mhz = current_val_mhz - step
        new_val_mhz = round(new_val_mhz, 3)
        new_val_mhz = max(self.freq_slider.cget("from"), min(new_val_mhz, self.freq_slider.cget("to")))
        self.freq_slider.set(new_val_mhz)

    def _on_location_mode_change(self, event=None):
        mode = self.location_mode_var.get()
        is_static_mode = "Static" in mode; is_route_mode = "Route" in mode; is_user_motion_mode = "User Motion" in mode
        if not hasattr(self, 'loc_mode_frame') or not self.loc_mode_frame.winfo_exists():
            if hasattr(self, 'master') and self.master.winfo_exists(): self.master.after(50, self._on_location_mode_change)
            return
        widget_pady = 1
        static_widgets_map = {
            self.static_address_label: {"row": 1, "column": 0, "sticky": "w", "padx": 2, "pady": widget_pady},
            self.static_address_entry: {"row": 1, "column": 1, "sticky": "ew", "padx": 2, "pady": widget_pady},
            self.static_lookup_button: {"row": 1, "column": 2, "sticky": "e", "padx": 2, "pady": widget_pady},
            self.static_loc_label_title: {"row": 2, "column": 0, "sticky": "w", "padx": 2, "pady": widget_pady},
            self.static_loc_label_value: {"row": 2, "column": 1, "columnspan": 2, "sticky": "w", "padx": 2, "pady": widget_pady},
            self.static_alt_label_title: {"row": 3, "column": 0, "sticky": "w", "padx": 2, "pady": widget_pady},
            self.static_alt_label_value: {"row": 3, "column": 1, "columnspan": 2, "sticky": "w", "padx": 2, "pady": widget_pady}
        }
        route_widgets_map = {
            self.start_address_label: {"row": 1, "column": 0, "sticky": "w", "padx": 2, "pady": widget_pady},
            self.start_address_entry: {"row": 1, "column": 1, "sticky": "ew", "padx": 2, "pady": widget_pady},
            self.start_lookup_button: {"row": 1, "column": 2, "padx": 2, "pady": widget_pady},
            self.start_loc_label: {"row": 2, "column": 0, "columnspan": 3, "sticky": "w", "padx": 2, "pady": widget_pady},
            self.start_alt_label: {"row": 3, "column": 0, "columnspan": 3, "sticky": "w", "padx": 2, "pady": widget_pady},
            self.end_address_label: {"row": 4, "column": 0, "sticky": "w", "padx": 2, "pady": widget_pady},
            self.end_address_entry: {"row": 4, "column": 1, "sticky": "ew", "padx": 2, "pady": widget_pady},
            self.end_lookup_button: {"row": 4, "column": 2, "padx": 2, "pady": widget_pady},
            self.end_loc_label: {"row": 5, "column": 0, "columnspan": 3, "sticky": "w", "padx": 2, "pady": widget_pady},
            self.end_alt_label:          {"row": 6, "column": 0, "columnspan": 3, "sticky": "w", "padx": 2, "pady": widget_pady},
            self.use_roads_check:         {"row": 7, "column": 0, "columnspan": 2, "sticky": "w", "padx": 2, "pady": widget_pady},
            self.stream_mode_check:       {"row": 9, "column": 0, "columnspan": 2, "sticky": "w", "padx": 2, "pady": widget_pady},
            self.real_drive_time_button:  {"row": 7, "column": 2, "sticky": "ew", "padx": 2, "pady": widget_pady},
            self.route_time_label:        {"row": 8, "column": 0, "columnspan": 3, "sticky": "w", "padx": 2, "pady": widget_pady}
        }
        motion_widgets_map = {
            self.motion_file_label: {"row": 1, "column": 0, "sticky": "w", "padx": 2, "pady": widget_pady},
            self.motion_file_entry: {"row": 1, "column": 1, "sticky": "ew", "padx": 2, "pady": widget_pady},
            self.browse_motion_button: {"row": 1, "column": 2, "padx": 2, "pady": widget_pady},
            self.motion_file_info_label: {"row": 2, "column": 0, "columnspan": 3, "sticky": "w", "padx": 2, "pady": (0,widget_pady)}
        }
        all_widget_maps = [static_widgets_map, route_widgets_map, motion_widgets_map]
        active_map = None
        if is_static_mode: active_map = static_widgets_map
        elif is_route_mode: active_map = route_widgets_map
        elif is_user_motion_mode: active_map = motion_widgets_map
        for widget_map_to_process in all_widget_maps:
            for widget, grid_opts in widget_map_to_process.items():
                if not widget.winfo_exists(): continue
                if widget_map_to_process == active_map: widget.grid(**grid_opts)
                else: widget.grid_remove()
        if hasattr(self, 'duration_slider') and self.duration_slider.winfo_exists():
            duration_state = tk.NORMAL if ("Static" in mode or "Route" in mode) else tk.DISABLED
            for w_name in ['duration_slider', 'duration_label', 'duration_plus_btn', 'duration_minus_btn']:
                widget = getattr(self, w_name, None)
                if widget and widget.winfo_exists(): widget.config(state=duration_state)
        self.config["location_mode"] = mode
        if is_static_mode and hasattr(self, 'static_address_entry') and self.static_address_entry.winfo_exists():
            self.config["address"] = self.static_address_entry.get()
        if is_route_mode and hasattr(self, 'start_address_var') and hasattr(self, 'end_address_var'):
            self.config["start_address"] = self.start_address_var.get()
            self.config["end_address"] = self.end_address_var.get()
        if is_user_motion_mode and hasattr(self, 'motion_file_path_var'):
            self.config["motion_file_path"] = self.motion_file_path_var.get()
        save_config(self.config)
        self.map_playback_center_latlon = None
        self._update_all_button_states(); self.update_map()
        if hasattr(self, 'scrollable_control_frame') and self.scrollable_control_frame.winfo_exists():
            self.master.after(50, lambda: self.control_canvas.config(scrollregion=self.control_canvas.bbox("all")))

    def _browse_motion_file(self):
        file_path = filedialog.askopenfilename(title="Select User Motion File", filetypes=(("CSV files", "*.csv"), ("NMEA GGA files", "*.txt *.gga"), ("All files", "*.*")))
        if file_path:
            self.motion_file_path_var.set(file_path)
            self.config["motion_file_path"] = file_path; save_config(self.config)
            self.add_to_terminal(f"Motion file selected: {os.path.basename(file_path)}")
        self._update_all_button_states()

    def lookup_static_address(self):
        address = self.static_address_entry.get()
        if not address: messagebox.showerror("Error", "Please enter an address."); return
        self.add_to_terminal(f"Lookup (Static): {address}")
        try:
            lat, lon = geocode_address(address)
            if lat is None or lon is None:
                messagebox.showerror("Error", "Failed to geocode address (Nominatim)."); self.add_to_terminal("Nominatim geocode failed."); return
            self.latlon = (lat, lon)
            self.static_loc_label_value.config(text=f"{float(lat):.4f},{float(lon):.4f}")
            self.add_to_terminal(f"Found (Static Lat/Lon): {lat:.4f}, {lon:.4f}")
            fetched_altitude = get_elevation(lat, lon, Maps_API_KEY)
            if fetched_altitude is not None:
                self.altitude = round(fetched_altitude, 1)
                self.static_alt_label_value.config(text=f"{self.altitude:.1f}m")
                self.add_to_terminal(f"Altitude (Google Elevation API): {self.altitude:.1f}m")
            else:
                self.altitude = None
                self.static_alt_label_value.config(text="N/A or API Error")
                self.add_to_terminal("Altitude lookup via Google Elevation API failed or API key issue.")
            self.map_playback_center_latlon = None
            self.config.update({"address": address, "latitude": lat, "longitude": lon, "altitude": self.altitude}); save_config(self.config)
            self.update_map()
        except Exception as e:
            messagebox.showerror("Error", f"Geocoding/Elevation error: {e}"); self.add_to_terminal(f"Geocode/Elevation error: {e}")
            self.altitude = None; self.static_alt_label_value.config(text="Error")
            self.config.update({"altitude": None}); save_config(self.config)
        self._update_all_button_states()

    def _lookup_start_address(self):
        address = self.start_address_var.get()
        if not address: messagebox.showerror("Error", "Enter Start Address."); return
        self.add_to_terminal(f"Lookup Start: {address}")
        lat, lon = geocode_address(address)
        if lat is not None and lon is not None:
            self.start_latlon = (lat, lon)
            if self.latlon[0] is None or "Route" in self.location_mode_var.get(): self.latlon = (lat, lon)
            self.start_loc_label.config(text=f"Start: {lat:.4f},{lon:.4f}")
            self.add_to_terminal(f"Start Loc (Lat/Lon): {lat:.4f},{lon:.4f}")
            fetched_altitude = get_elevation(lat, lon, Maps_API_KEY)
            if fetched_altitude is not None:
                self.start_altitude = round(fetched_altitude, 1)
                self.start_alt_label.config(text=f"Start Alt: {self.start_altitude:.1f}m")
                self.add_to_terminal(f"Start Altitude (Google): {self.start_altitude:.1f}m")
            else:
                self.start_altitude = None
                self.start_alt_label.config(text="Start Alt: N/A or API Error")
                self.add_to_terminal("Start altitude lookup (Google) failed or API key issue.")
            self.map_playback_center_latlon = None
            self._last_road_route_cache = None  # FIX: invalidate cached route — start coord changed
            self.config["start_address"] = address; self.config["start_latlon"] = self.start_latlon; self.config["start_altitude"] = self.start_altitude
            if "Route" in self.location_mode_var.get():
                self.config["latitude"] = lat; self.config["longitude"] = lon
            save_config(self.config); self.update_map()
        else:
            messagebox.showerror("Error", "Failed to geocode Start Address (Nominatim).")
            self.start_loc_label.config(text="Start Lat,Lon: Error"); self.start_alt_label.config(text="Start Alt: -")
            self.start_altitude = None; self.config.update({"start_altitude": None}); save_config(self.config)
        self._update_all_button_states()

    def _lookup_end_address(self):
        address = self.end_address_var.get()
        if not address: messagebox.showerror("Error", "Enter End Address."); return
        self.add_to_terminal(f"Lookup End: {address}")
        lat, lon = geocode_address(address)
        if lat is not None and lon is not None:
            self.end_latlon = (lat, lon)
            self.end_loc_label.config(text=f"End: {lat:.4f},{lon:.4f}")
            self.add_to_terminal(f"End Loc (Lat/Lon): {lat:.4f},{lon:.4f}")
            fetched_altitude = get_elevation(lat, lon, Maps_API_KEY)
            if fetched_altitude is not None:
                self.end_altitude = round(fetched_altitude, 1)
                self.end_alt_label.config(text=f"End Alt: {self.end_altitude:.1f}m")
                self.add_to_terminal(f"End Altitude (Google): {self.end_altitude:.1f}m")
            else:
                self.end_altitude = None
                self.end_alt_label.config(text="End Alt: N/A or API Error")
                self.add_to_terminal("End altitude lookup (Google) failed or API key issue.")
            self._last_road_route_cache = None  # FIX: invalidate cached route — end coord changed
            self.config["end_address"] = address; self.config["end_latlon"] = self.end_latlon; self.config["end_altitude"] = self.end_altitude
            save_config(self.config)
        else:
            messagebox.showerror("Error", "Failed to geocode End Address (Nominatim).")
            self.end_loc_label.config(text="End Lat,Lon: Error"); self.end_alt_label.config(text="End Alt: -")
            self.end_altitude = None; self.config.update({"end_altitude": None}); save_config(self.config)
        self._update_all_button_states()

    def _update_all_button_states(self):
        action_buttons_exist = hasattr(self, 'action_buttons') and "gen" in self.action_buttons and self.action_buttons["gen"].winfo_exists()
        if not action_buttons_exist:
            if hasattr(self, 'master') and self.master.winfo_exists():
                self.master.after(100, self._update_all_button_states)
            return

        # --- Determine current state ---
        is_generating = isinstance(self.gps_sim_proc, subprocess.Popen) and self.gps_sim_proc.poll() is None
        is_remote_generating = self.remote_generation_in_progress
        is_spoofing_or_looping = self.running
        is_transferring_sim = self.transfer_in_progress
        is_transferring_custom = self.custom_transfer_in_progress
        is_updating_eph = self.ephemeris_update_running
        is_auto_blast_phase_active = self.auto_blast_active_phase
        can_generate = False
        current_loc_mode = self.location_mode_var.get()
        if "Static" in current_loc_mode:
            if self.latlon[0] is not None and self.latlon[1] is not None: can_generate = True
        elif "Route" in current_loc_mode:
            if self.start_latlon[0] is not None and self.end_latlon[0] is not None: can_generate = True
        else: # User motion modes
            if self.motion_file_path_var.get() and os.path.exists(self.motion_file_path_var.get()): can_generate = True

        # --- Initialize button properties ---
        gen_text, gen_state, gen_style = "Gen", tk.NORMAL, "TButton"
        remote_gen_text, remote_gen_state, remote_gen_style = "Remote Gen", tk.NORMAL, "TButton"
        spoof_text, spoof_state, spoof_style = "Sim", tk.NORMAL, "TButton"
        loop_text, loop_state, loop_style = "Loop", tk.NORMAL, "TButton"
        update_eph_text, update_eph_state, update_eph_style = "Update Eph", tk.NORMAL, "TButton"
        transfer_sim_text, transfer_sim_state, transfer_sim_style = "To SD", tk.NORMAL, "TButton"
        transfer_custom_text, transfer_custom_state, transfer_custom_style = "File->SD", tk.NORMAL, "TButton"
        stop_state = tk.DISABLED
        auto_blast_btn_state = tk.NORMAL
        active_map_btn_state = tk.NORMAL

        is_glo_jamming = self.glo_jam_active
        active_operation_present = is_generating or is_remote_generating or is_spoofing_or_looping or is_transferring_sim or is_transferring_custom or is_updating_eph or is_auto_blast_phase_active or is_glo_jamming

        base_style = "NonActive.TButton" if active_operation_present else "TButton"
        gen_style = remote_gen_style = spoof_style = loop_style = update_eph_style = transfer_sim_style = transfer_custom_style = base_style

        # --- Determine properties based on active operation ---
        if is_generating:
            gen_text, gen_state, gen_style = "Generating...", tk.DISABLED, "ActiveGenerate.TButton"
        elif is_remote_generating:
            remote_gen_text, remote_gen_state, remote_gen_style = "Remote Gen...", tk.DISABLED, "ActiveRemoteGenerate.TButton"
        elif is_auto_blast_phase_active:
            spoof_text, spoof_style, spoof_state = "AutoBlasting", "ActiveSim.TButton", tk.DISABLED
            loop_text, loop_style, loop_state = "AutoBlasting", "ActiveLoop.TButton", tk.DISABLED
        elif is_spoofing_or_looping:
            if self.is_manual_blast_initial_phase:
                if self.intended_loop_state_after_manual_blast:
                    loop_text, loop_style, loop_state = "Looping (Blast)...", "ActiveLoop.TButton", tk.DISABLED
                else:
                    spoof_text, spoof_style, spoof_state = "Simulating (Blast)...", "ActiveSim.TButton", tk.DISABLED
            elif self.is_looping_active:
                loop_text, loop_state, loop_style = "Looping...", tk.DISABLED, "ActiveLoop.TButton"
            else:
                spoof_text, spoof_state, spoof_style = "Simulating...", tk.DISABLED, "ActiveSim.TButton"
        elif is_updating_eph:
            update_eph_text, update_eph_state, update_eph_style = "Updating Eph...", tk.DISABLED, "ActiveUpdate.TButton"
        elif is_transferring_sim:
            transfer_sim_text, transfer_sim_state, transfer_sim_style = "Copying...", tk.DISABLED, "ActiveTransfer.TButton"
        elif is_transferring_custom:
            transfer_custom_text, transfer_custom_state, transfer_custom_style = "Xfer File...", tk.DISABLED, "ActiveCustomTransfer.TButton"

        # --- Fine-tune states for active operations ---
        if active_operation_present:
            stop_state = tk.NORMAL
            if not is_generating: gen_state = tk.DISABLED
            if not is_remote_generating: remote_gen_state = tk.DISABLED
            if not is_updating_eph: update_eph_state = tk.DISABLED
            if not is_transferring_sim: transfer_sim_state = tk.DISABLED
            if not is_transferring_custom: transfer_custom_state = tk.DISABLED
            # Disable sim/loop if they are not the active process
            if not (is_spoofing_or_looping and not self.is_looping_active and not self.is_manual_blast_initial_phase) and not is_auto_blast_phase_active:
                 spoof_state = tk.DISABLED
            if not (is_spoofing_or_looping and self.is_looping_active and not self.is_manual_blast_initial_phase) and not is_auto_blast_phase_active:
                 loop_state = tk.DISABLED
            # Disable secondary controls during primary operations
            if is_generating or is_remote_generating or is_transferring_sim or is_transferring_custom or is_updating_eph or is_auto_blast_phase_active:
                auto_blast_btn_state = tk.DISABLED
            if is_generating or is_remote_generating or is_transferring_sim or is_transferring_custom or is_updating_eph:
                active_map_btn_state = tk.DISABLED
        else:
            # --- Set states for idle application ---
            stop_state = tk.DISABLED
            gen_state = tk.NORMAL if can_generate else tk.DISABLED
            if gen_state == tk.DISABLED: gen_style = "NonActive.TButton"
            remote_gen_state = tk.NORMAL if can_generate else tk.DISABLED
            if remote_gen_state == tk.DISABLED: remote_gen_style = "NonActive.TButton"
            spoof_state = tk.NORMAL
            loop_state = tk.NORMAL
            update_eph_state = tk.NORMAL
            transfer_sim_state = tk.NORMAL if os.path.exists(SIM_OUTPUT) and os.path.getsize(SIM_OUTPUT) > 0 else tk.DISABLED
            if transfer_sim_state == tk.DISABLED: transfer_sim_style = "NonActive.TButton"
            transfer_custom_state = tk.NORMAL
            auto_blast_btn_state = tk.NORMAL
            active_map_btn_state = tk.NORMAL

        # --- Apply configurations to all buttons ---
        self.action_buttons["gen"].config(text=gen_text, state=gen_state, style=gen_style)
        self.action_buttons["remote_gen"].config(text=remote_gen_text, state=remote_gen_state, style=remote_gen_style)
        self.action_buttons["sim"].config(text=spoof_text, state=spoof_state, style=spoof_style)
        self.action_buttons["loop"].config(text=loop_text, state=loop_state, style=loop_style)
        # Stream button — active only when actually streaming, otherwise enabled when idle
        _is_streaming = getattr(self, 'is_streaming', False)
        if _is_streaming:
            _stream_text, _stream_style, _stream_state = "Streaming...", "ActiveLoop.TButton", tk.DISABLED
        elif active_operation_present and not _is_streaming:
            _stream_text, _stream_style, _stream_state = "Stream", "NonActive.TButton", tk.DISABLED
        else:
            _stream_text, _stream_style, _stream_state = "Stream", "TButton", tk.NORMAL
        self.action_buttons["stream"].config(text=_stream_text, state=_stream_state, style=_stream_style)
        self.action_buttons["update_eph"].config(text=update_eph_text, state=update_eph_state, style=update_eph_style)
        self.action_buttons["to_sd"].config(text=transfer_sim_text, state=transfer_sim_state, style=transfer_sim_style)
        self.action_buttons["file->sd"].config(text=transfer_custom_text, state=transfer_custom_state, style=transfer_custom_style)
        self.action_buttons["stop"].config(state=stop_state, style="TButton" if stop_state == tk.NORMAL else "NonActive.TButton")
        self.action_buttons["quit"].config(state=tk.NORMAL, style='TButton')
        self.action_buttons["auto_blast"].config(state=auto_blast_btn_state)

        self._update_auto_blast_button_style()
        self._update_glo_jam_button_style()
        if hasattr(self, 'active_map_button'):
            self.active_map_button.config(state=active_map_btn_state)
            self._update_active_map_button_style()

        if self.master and self.master.winfo_exists():
            self.master.update_idletasks()


    def on_canvas_resize(self, event):
        new_width = event.width; new_height = event.height
        if new_width > 1 and new_height > 1 and (self.current_map_width != new_width or self.current_map_height != new_height):
            self.current_map_width = new_width; self.current_map_height = new_height
            if hasattr(self, '_after_id_map_resize') and self._after_id_map_resize:
                 if self.master and self.master.winfo_exists(): self.master.after_cancel(self._after_id_map_resize)
            if self.master and self.master.winfo_exists(): self._after_id_map_resize = self.master.after(500, self.update_map)

    def update_duration_label(self, val_str):
        s_val = int(float(val_str))
        if hasattr(self, 'duration_label') and self.duration_label.winfo_exists(): self.duration_label.config(text=f"{s_val}s")
        self.config["duration"] = s_val; save_config(self.config)

    def update_gen_cores(self, val_str):
        s_val = int(float(val_str))
        if hasattr(self, 'gen_cores_label') and self.gen_cores_label.winfo_exists(): self.gen_cores_label.config(text=f"{s_val}c")
        self.config["gen_cores"] = s_val; save_config(self.config)

    def update_blast_duration(self, val_str):
        s_val = int(float(val_str))
        if hasattr(self, 'blast_duration_label') and self.blast_duration_label.winfo_exists(): self.blast_duration_label.config(text=f"{s_val}s")
        self.config["blast_duration_sec"] = s_val; save_config(self.config)

    def update_gain(self, val_str):
        db = int(float(val_str))
        if hasattr(self, 'gain_label') and self.gain_label.winfo_exists(): self.gain_label.config(text=f"{db}dB")
        new_gain = db; current_config_gain = self.config.get("gain"); self.config["gain"] = new_gain; save_config(self.config)
        if self.auto_blast_active_phase:
            self.add_to_terminal(f"Gain changed to {new_gain}dB. Will apply after current auto-blast cycle.")
            self.auto_blast_original_gain_temp = new_gain; return
        if self.is_manual_blast_initial_phase:
            if self.after_manual_blast_id:
                if self.master and self.master.winfo_exists(): self.master.after_cancel(self.after_manual_blast_id)
                self.after_manual_blast_id = None
            if self.proc and self.proc.poll() is None:
                try: self.proc.terminate(); self.proc.wait(timeout=0.2)
                except: self.proc.kill()
            self.is_manual_blast_initial_phase = False; self.running = False; self.proc = None
            self._start_hackrf(loop=self.intended_loop_state_after_manual_blast, is_blast_phase_override=False, gain_override_db=new_gain); return
        if current_config_gain == new_gain and not (self.running and self.proc): return
        if self.running and self.proc:
            self.add_to_terminal(f"Gain -> {new_gain}dB. Restarting simulation...")
            was_looping = self.is_looping_active; current_proc_to_stop = self.proc
            self.running = False; self.proc = None; self.is_looping_active = False
            try:
                if current_proc_to_stop and current_proc_to_stop.poll() is None:
                    current_proc_to_stop.terminate()
                    try: current_proc_to_stop.wait(timeout=0.5)
                    except subprocess.TimeoutExpired: current_proc_to_stop.kill(); current_proc_to_stop.wait()
            except Exception as e: self.add_to_terminal(f"Err stop gain upd: {e}")
            self._start_hackrf(loop=was_looping, is_blast_phase_override=False, gain_override_db=new_gain)

    def update_frequency(self, val_str):
        mhz = round(float(val_str), 3)
        if hasattr(self, 'freq_label') and self.freq_label.winfo_exists(): self.freq_label.config(text=f"{mhz:.3f}MHz")
        new_freq_hz = int(mhz * 1e6); current_config_freq_hz = self.config.get("frequency_hz", int(DEFAULT_FREQ_HZ_STR))
        self.config["frequency_hz"] = new_freq_hz; save_config(self.config)
        if self.auto_blast_active_phase: self.add_to_terminal(f"Frequency changed. Will apply after current auto-blast cycle if applicable."); return
        if self.is_manual_blast_initial_phase: return
        if current_config_freq_hz == new_freq_hz and not (self.running and self.proc): return
        if self.running and self.proc:
            self.add_to_terminal(f"Frequency -> {mhz:.3f}MHz. Restarting simulation...")
            was_looping = self.is_looping_active; current_proc_to_stop = self.proc
            self.running = False; self.proc = None; self.is_looping_active = False
            try:
                if current_proc_to_stop and current_proc_to_stop.poll() is None:
                    current_proc_to_stop.terminate()
                    try: current_proc_to_stop.wait(timeout=0.5)
                    except subprocess.TimeoutExpired: current_proc_to_stop.kill(); current_proc_to_stop.wait()
            except Exception as e: self.add_to_terminal(f"Err stop freq upd: {e}")
            self._start_hackrf(loop=was_looping, is_blast_phase_override=False)

    def set_default_frequency(self):
        self.add_to_terminal(f"Setting frequency to default: {DEFAULT_FREQ_MHZ:.3f} MHz")
        if hasattr(self, 'freq_slider'): self.freq_slider.set(DEFAULT_FREQ_MHZ)

    def update_map_on_zoom(self, val_str):
        val = int(float(val_str))
        if hasattr(self, 'zoom_label') and self.zoom_label.winfo_exists(): self.zoom_label.config(text=f"Z{val}")
        self.config["map_zoom"] = val; save_config(self.config); self.update_map()

    def update_map(self, *_):
        if not (hasattr(self, 'canvas') and self.canvas.winfo_exists()): return
        display_lat, display_lon = None, None; current_loc_mode = self.location_mode_var.get()
        is_dynamic_mode_running = self.running and self.active_map_enabled.get() and \
                                  ("Route" in current_loc_mode or "User Motion (LLH .csv)" in current_loc_mode) and \
                                  not self.auto_blast_active_phase
        if is_dynamic_mode_running and self.map_playback_center_latlon: display_lat, display_lon = self.map_playback_center_latlon
        else:
            if "Static" in current_loc_mode: display_lat, display_lon = self.latlon
            elif "Route" in current_loc_mode:
                if self.start_latlon[0] is not None: display_lat, display_lon = self.start_latlon
                elif self.latlon[0] is not None: display_lat, display_lon = self.latlon
            elif "User Motion" in current_loc_mode:
                 if self.motion_playback_timeline and len(self.motion_playback_timeline) > 0:
                     display_lat, display_lon = self.motion_playback_timeline[0][1], self.motion_playback_timeline[0][2]
                 elif self.latlon[0] is not None: display_lat, display_lon = self.latlon
            elif self.latlon[0] is not None: display_lat, display_lon = self.latlon
        if display_lat is not None and display_lon is not None:
            zoom = self.zoom_slider.get() if hasattr(self, 'zoom_slider') else self.config.get("map_zoom", 14)
            map_width = max(1, self.current_map_width); map_height = max(1, self.current_map_height)
            current_map_type = self.map_type_var.get() if hasattr(self, 'map_type_var') else self.config.get("map_type", "roadmap")
            try: f_lat = float(display_lat); f_lon = float(display_lon); i_zoom = int(float(zoom))
            except (ValueError, TypeError) as e:
                self.add_to_terminal(f"Invalid map parameters for API call: {e}"); self.canvas.delete("all")
                self.canvas.create_text(map_width/2, map_height/2, text="Invalid map params.", anchor="center"); return
            # FIX: skip API call if map params are identical to last fetch
            new_params = (round(f_lat, 4), round(f_lon, 4), i_zoom, map_width, map_height, current_map_type)
            if new_params == self._last_map_params and self.map_image is not None:
                return  # same map already displayed — no API call
            self._last_map_params = new_params
            image_data = download_static_map(f_lat, f_lon, i_zoom, map_width, map_height, maptype=current_map_type)
            if image_data:
                try:
                    os.makedirs(TEMP_DIR, exist_ok=True)
                    temp_map_path = os.path.join(TEMP_DIR, f"map_google_{time.time()}_{os.getpid()}.png")
                    with open(temp_map_path, "wb") as f: f.write(image_data)
                    image = Image.open(temp_map_path); self.map_image = ImageTk.PhotoImage(image)
                    self.canvas.delete("all"); self.canvas.create_image(map_width/2, map_height/2, anchor="center", image=self.map_image)
                    try: os.remove(temp_map_path)
                    except: pass
                except Exception as e: self.add_to_terminal(f"Map display error: {e}")
            else:
                self.canvas.delete("all"); text_to_display = "Map load failed."
                if not Maps_API_KEY or Maps_API_KEY == "YOUR_Maps_API_KEY_HERE":
                    text_to_display = "Google Maps API Key missing/invalid."
                self.canvas.create_text(map_width/2, map_height/2, text=text_to_display, anchor="center")
        else:
            self.canvas.delete("all"); map_width = max(1, self.current_map_width); map_height = max(1, self.current_map_height)
            self.canvas.create_text(map_width/2, map_height/2, text="Set location for map.", anchor="center")

    def update_ephemeris(self):
        if self.ephemeris_update_running: messagebox.showwarning("In Progress", "Ephemeris update is already running."); return
        if self.is_any_operation_active(exclude_ephem_update=True): messagebox.showwarning("Busy", "Another operation is active. Please stop it first."); return
        self.ephemeris_update_running = True; self._update_all_button_states()
        self.add_to_terminal("Attempting to update ephemeris...")
        threading.Thread(target=self._perform_ephemeris_update_thread, daemon=True).start()

    def select_ephemeris_file(self):
        """Browse and select a previously downloaded ephemeris file."""
        file_path = filedialog.askopenfilename(
            title="Select Ephemeris File",
            initialdir=EPHEMERIS_DIR if os.path.exists(EPHEMERIS_DIR) else os.path.expanduser("~"),
            filetypes=(("RINEX Nav files", "*.??n"), ("All files", "*.*"))
        )
        if not file_path:
            self.add_to_terminal("Ephemeris file selection cancelled.")
            return
        file_size = os.path.getsize(file_path)
        if file_size < 150_000:
            if not messagebox.askyesno("Small File Warning",
                f"{os.path.basename(file_path)} is only {file_size//1024}KB.\n"
                "This may be an incomplete file and could cause generation errors.\n\n"
                "Use it anyway?"):
                self.add_to_terminal("Ephemeris selection cancelled by user.")
                return
        self.config["ephemeris_file"] = file_path
        save_config(self.config)
        with open(LATEST_FILE_PATH, "w") as f:
            f.write(file_path + "\n")
        self.add_to_terminal(f"Ephemeris set to: {os.path.basename(file_path)} ({file_size//1024}KB)")
        self._update_all_button_states()

    def _perform_ephemeris_update_thread(self):
        filepath, timestamp = None, None; error_message = None
        try:
            from gpsdata import download_ephemeris
            filepath, timestamp = download_ephemeris()
            if filepath and os.path.exists(filepath) and timestamp:
                eph_filename = os.path.basename(filepath)
                self.config["ephemeris_file"] = filepath; save_config(self.config)
                if self.master and self.master.winfo_exists():
                    self.master.after(0, lambda: messagebox.showinfo("Ephemeris Updated", f"Downloaded {eph_filename}\nTimestamp: {timestamp}"))
                    self.master.after(0, self.add_to_terminal, f"Eph. updated: {eph_filename}")
            else: error_message = "Failed to update ephemeris (download_ephemeris returned invalid data or file not found)."
        except ImportError: error_message = "gpsdata module not found. Please ensure it's installed and in your Python path."
        except TypeError as te:
             if "got an unexpected keyword argument" in str(te) or ("takes" in str(te) and "but" in str(te) and "were given" in str(te)):
                 error_message = f"Ephemeris update failed (function signature mismatch in gpsdata.download_ephemeris): {te}"
             else: error_message = f"Ephemeris update failed (TypeError): {te}"
        except Exception as e: error_message = f"Ephemeris update failed: {e}\n{traceback.format_exc()}"
        finally:
            self.ephemeris_update_running = False
            if error_message and self.master and self.master.winfo_exists():
                self.master.after(0, lambda m=error_message: messagebox.showerror("Error", m))
                self.master.after(0, self.add_to_terminal, error_message)
            if self.master and self.master.winfo_exists(): self.master.after(0, self._update_all_button_states)

    def add_to_terminal(self, message):
        if hasattr(self, 'terminal_output') and self.terminal_output and self.terminal_output.winfo_exists():
            try:
                if self.master.winfo_exists():
                    self.terminal_output.configure(state='normal')
                    current_lines_str = self.terminal_output.index('end-1c').split('.')[0]
                    try:
                        num_lines = int(current_lines_str)
                    except ValueError:
                        num_lines = 0
                    if num_lines >= MAX_TERMINAL_LINES:
                        lines_to_delete = (num_lines - MAX_TERMINAL_LINES) + 1
                        if lines_to_delete > 0:
                            self.terminal_output.delete("1.0", f"{lines_to_delete + 1}.0")
                    self.terminal_output.insert(tk.END, message + "\n")
                    self.terminal_output.see(tk.END)
                    self.terminal_output.configure(state='disabled')
            except tk.TclError: pass
        print(message)

    def generate(self):
        if self.is_any_operation_active(exclude_generate=True): messagebox.showwarning("Busy", "Another operation is active. Please stop it first."); return
        self.motion_playback_timeline = None; self.active_signal_duration_sec = 0; self.map_playback_center_latlon = None
        self.gps_sim_proc = None

        self.add_to_terminal("Generating GPS signal file...")

        _cores = int(self.config.get("gen_cores", 1))
        _use_exec = GPS_SDR_SIM_4CORE_EXECUTABLE if _cores > 1 else GPS_SDR_SIM_EXECUTABLE
        import os as _os; _sim_env = _os.environ.copy(); _sim_env['GPSSIM_NTHREADS'] = str(_cores)
        args = [_use_exec]; eph_to_use = self.config.get("ephemeris_file")
        if not eph_to_use or not os.path.exists(eph_to_use):
            if os.path.exists(LATEST_FILE_PATH):
                with open(LATEST_FILE_PATH, "r") as f: eph_filename_from_record = f.read().strip()
                eph_to_use = os.path.join(EPHEMERIS_DIR, eph_filename_from_record)
            else: messagebox.showerror("Error", "No valid ephemeris file. Update ephemeris first."); self.add_to_terminal("Error: No valid ephemeris."); self._update_all_button_states(); return
        if not os.path.exists(eph_to_use):
            messagebox.showerror("Error", f"Ephemeris file '{os.path.basename(eph_to_use)}' not found. Update ephemeris."); self.add_to_terminal(f"Error: Eph file '{os.path.basename(eph_to_use)}' missing."); self._update_all_button_states(); return
        args.extend(["-e", eph_to_use])
        # Automatically align simulation time with data file
        if os.path.exists(LATEST_TIME_PATH):
            with open(LATEST_TIME_PATH, "r") as tf:
                t_stamp = tf.read().strip()
            if t_stamp:
                args.extend(["-t", t_stamp])
                self.add_to_terminal(f"Aligned simulation time to: {t_stamp}")
        current_loc_mode = self.location_mode_var.get(); motion_file_for_sim = None
        duration_from_slider = self.duration_slider.get() if hasattr(self, 'duration_slider') else self.config.get("duration", 60)
        duration_for_sim_cmd_val = 0
        if "Static" in current_loc_mode:
            lat, lon = self.latlon; alt = self.altitude if self.altitude is not None else DEFAULT_ALTITUDE_METERS
            if lat is None or lon is None: messagebox.showerror("Error", "Lat/Lon not set for Static mode."); self.add_to_terminal("Error: Lat/Lon not set for Static."); self._update_all_button_states(); return
            args.extend(["-l", f"{float(lat):.7f},{float(lon):.7f},{float(alt):.1f}"])
            self.add_to_terminal(f"Static location: Lat={lat:.4f}, Lon={lon:.4f}, Alt={alt:.1f}m (Using {'fetched' if self.altitude is not None else 'default'} altitude)")
            duration_for_sim_cmd_val = duration_from_slider; duration_for_sim_cmd = str(duration_for_sim_cmd_val)
        elif "Route" in current_loc_mode:
            if self.start_latlon[0] is None or self.end_latlon[0] is None: messagebox.showerror("Error", "Start/End address for route not geocoded."); self.add_to_terminal("Error: Route points missing."); self._update_all_button_states(); return
            start_alt_route = self.start_altitude if self.start_altitude is not None else DEFAULT_ALTITUDE_METERS
            end_alt_route = self.end_altitude if self.end_altitude is not None else DEFAULT_ALTITUDE_METERS
            self.add_to_terminal(f"Route: StartAlt={start_alt_route:.1f}m ({'fetched' if self.start_altitude is not None else 'default'}), EndAlt={end_alt_route:.1f}m ({'fetched' if self.end_altitude is not None else 'default'})")
            motion_file_for_sim = self._generate_route_motion_file(self.start_latlon, self.end_latlon, start_alt_route, end_alt_route, duration_from_slider, use_roads=self.use_roads_var.get())
            if not motion_file_for_sim: messagebox.showerror("Error", "Failed to generate route motion file."); self.add_to_terminal("Error: Route CSV gen failed."); self._update_all_button_states(); return
            args.extend(["-x", motion_file_for_sim]);
            duration_for_sim_cmd_val = min(duration_from_slider, 3600); duration_for_sim_cmd = str(duration_for_sim_cmd_val)
        else:
            motion_file_for_sim = self.motion_file_path_var.get()
            if not motion_file_for_sim or not os.path.exists(motion_file_for_sim): messagebox.showerror("Error", "Motion file not selected or not found."); self.add_to_terminal("Error: Motion file missing."); self._update_all_button_states(); return
            if "ECEF" in current_loc_mode: args.extend(["-u", motion_file_for_sim])
            elif "LLH" in current_loc_mode: args.extend(["-x", motion_file_for_sim])
            elif "NMEA" in current_loc_mode: args.extend(["-g", motion_file_for_sim])
            duration_for_sim_cmd_val = min(duration_from_slider, 3600); duration_for_sim_cmd = str(duration_for_sim_cmd_val)
            self.add_to_terminal(f"User motion file: Altitude will be taken from the file itself.")
        args.extend(["-d", duration_for_sim_cmd, "-b", "8", "-o", SIM_OUTPUT])
        self.add_to_terminal(f"Eph: {eph_to_use}"); os.makedirs(os.path.dirname(SIM_OUTPUT), exist_ok=True)
        self.add_to_terminal(f"Out: {SIM_OUTPUT}");
        if motion_file_for_sim: self.add_to_terminal(f"Using motion file: {motion_file_for_sim}")
        self.add_to_terminal(f"CMD: {' '.join(shlex.quote(arg) for arg in args)}")
        if motion_file_for_sim and ("Route" in current_loc_mode or "User Motion (LLH .csv)" in current_loc_mode):
            self._prepare_playback_map_timeline(motion_file_for_sim, duration_for_sim_cmd_val)
        else: self.active_signal_duration_sec = duration_for_sim_cmd_val
        try:
            self.gps_sim_proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, universal_newlines=True, env=_sim_env)
            self._update_all_button_states()

            def stream_watcher_generate(identifier, stream, app_instance_ref, is_stderr_stream=False):
                try:
                    for line in iter(stream.readline, ''):
                        if line and app_instance_ref.master and app_instance_ref.master.winfo_exists():
                            app_instance_ref.master.after(0, app_instance_ref.add_to_terminal, line.strip())
                    if stream: stream.close()
                except Exception as e_stream:
                    if app_instance_ref.master and app_instance_ref.master.winfo_exists():
                         app_instance_ref.master.after(0, app_instance_ref.add_to_terminal, f"Error in {identifier} stream watcher: {e_stream}")

            def process_finisher(app_instance_ref, temp_motion_file_to_delete=None):
                current_proc_ref = app_instance_ref.gps_sim_proc; return_code = -1
                if isinstance(current_proc_ref, subprocess.Popen):
                    try: return_code = current_proc_ref.wait()
                    except Exception as e_wait_proc: print(f"Error waiting for gps_sim_proc: {e_wait_proc}")
                app_instance_ref.gps_sim_proc = None
                if app_instance_ref.master and app_instance_ref.master.winfo_exists():
                    app_instance_ref.master.after(0, app_instance_ref.add_to_terminal, f"gps-sdr-sim finished. Exit code: {return_code}.")
                    if return_code == 0:
                        if os.path.exists(SIM_OUTPUT) and os.path.getsize(SIM_OUTPUT) > 0:
                            app_instance_ref.master.after(0, lambda: messagebox.showinfo("Success", f"Generated: {os.path.basename(SIM_OUTPUT)}"))
                            app_instance_ref.master.after(0, self._update_status_bar)
                        else: app_instance_ref.master.after(0, lambda: messagebox.showerror("Error", "Generation successful, but output file issue (missing or empty)."))
                    else:
                        def _ask_proceed(rc=return_code, app=app_instance_ref):
                            if messagebox.askyesno("Generation Warning",
                                f"Generation exited with code {rc} — invalid start time.\n\n"
                                "The timestamp will be corrected to match the\n"
                                "ephemeris file and generation will retry.\n\n"
                                "Proceed?"):
                                try:
                                    eph_path = app.config.get("ephemeris_file") or ""
                                    if not eph_path or not os.path.exists(eph_path):
                                        if os.path.exists(LATEST_FILE_PATH):
                                            with open(LATEST_FILE_PATH) as _f:
                                                _p = _f.read().strip()
                                            eph_path = _p if os.path.isabs(_p) else os.path.join(EPHEMERIS_DIR, _p)
                                    derived_ts = None
                                    if eph_path and os.path.exists(eph_path):
                                        # Derive timestamp from filename (authoritative) not RINEX data line
                                        # brdc0750.26n -> day 075, year 2026 -> March 16
                                        import datetime as _dt
                                        _bn = os.path.basename(eph_path)
                                        try:
                                            _doy = int(_bn[4:7])
                                            _yr  = 2000 + int(_bn[7:9])
                                            _d   = _dt.datetime(_yr, 1, 1) + _dt.timedelta(days=_doy - 1)
                                            derived_ts = f"{_d.year}/{_d.month:02d}/{_d.day:02d},00:00:00"
                                        except Exception:
                                            derived_ts = None
                                    if derived_ts:
                                        with open(LATEST_TIME_PATH, "w") as tf:
                                            tf.write(derived_ts + "\n")
                                        app.add_to_terminal(f"Timestamp corrected to: {derived_ts}. Retrying...")
                                    else:
                                        app.add_to_terminal("Warning: Could not derive timestamp. Retrying anyway...")
                                except Exception as e_fix:
                                    app.add_to_terminal(f"Warning: Timestamp fix failed: {e_fix}. Retrying anyway...")
                                app.master.after(0, app.generate)
                            else:
                                app.add_to_terminal(f"Generation aborted by user (exit code {rc}).")
                        app_instance_ref.master.after(0, _ask_proceed)
                    if temp_motion_file_to_delete and os.path.exists(temp_motion_file_to_delete):
                        try:
                            os.remove(temp_motion_file_to_delete)
                            app_instance_ref.master.after(0, app_instance_ref.add_to_terminal, f"Temporary route file {os.path.basename(temp_motion_file_to_delete)} deleted.")
                        except Exception as e_del_temp: app_instance_ref.master.after(0, app_instance_ref.add_to_terminal, f"Error deleting temporary route file: {e_del_temp}")
                    app_instance_ref.master.after(0, app_instance_ref._update_all_button_states)

            temp_file_to_pass_to_finisher = motion_file_for_sim if "Route" in current_loc_mode else None
            threading.Thread(target=stream_watcher_generate, args=("SIM_OUT", self.gps_sim_proc.stdout, self, False), daemon=True).start()
            threading.Thread(target=stream_watcher_generate, args=("SIM_ERR", self.gps_sim_proc.stderr, self, True), daemon=True).start()
            threading.Thread(target=process_finisher, args=(self, temp_file_to_pass_to_finisher), daemon=True).start()
        except FileNotFoundError as e_fnf:
            self.add_to_terminal(f"Error: Executable not found: {e_fnf.filename}. Check PATH."); messagebox.showerror("Error", f"Executable not found: {e_fnf.filename}. Check PATH.")
            self.gps_sim_proc = None; self._update_all_button_states()
        except Exception as e_gen:
            self.add_to_terminal(f"Unexpected error during gps-sdr-sim: {e_gen}\n{traceback.format_exc()}"); messagebox.showerror("Error", f"Unexpected error starting generation: {str(e_gen)}")
            self.gps_sim_proc = None; self._update_all_button_states()

    def remote_generate(self):
        """Handler for the 'Remote Gen' button."""
        if self.is_any_operation_active(exclude_remote_generate=True):
            messagebox.showwarning("Busy", "Another operation is active. Please stop it first.")
            return

        self.remote_generation_in_progress = True
        self._update_all_button_states()
        self.add_to_terminal("Starting remote generation process...")

        self.remote_gen_worker_thread = threading.Thread(target=self._perform_remote_generation_thread, daemon=True)
        self.remote_gen_worker_thread.start()


    def _perform_remote_generation_thread(self):
        """The core logic for remote generation using a polling mechanism."""
        error_occurred = False
        start_time = time.time()
        job_id = None

        raw_url = self.config.get("remote_server_url", DEFAULT_REMOTE_SERVER_URL)
        server_base_url = raw_url.replace('/generate', '').rstrip('/')

        try:
            self.master.after(0, self.add_to_terminal, "Step 1: Submitting job to server...")
            job_id = self._submit_remote_job(server_base_url)
            self.master.after(0, self.add_to_terminal, f"--> Job submitted successfully. Job ID: {job_id}")

            self.master.after(0, self.add_to_terminal, "Step 2: Polling server for job status...")
            status_url = f"{server_base_url}/status/{job_id}"

            while time.time() - start_time < REMOTE_GEN_TOTAL_TIMEOUT_SEC:
                if not self.remote_generation_in_progress:
                    self.add_to_terminal("--> Remote generation cancelled by user.")
                    return

                response = requests.get(status_url, timeout=10)
                response.raise_for_status()
                data = response.json()
                status = data.get('status')

                if status == 'complete':
                    self.master.after(0, self.add_to_terminal, "--> Job complete! Starting download...")
                    download_url = f"{server_base_url}/download/{job_id}"
                    total_file_size = data.get('file_size') # Get file size from status
                    self._download_remote_file(download_url, total_file_size)
                    self.master.after(0, lambda: messagebox.showinfo("Success", "Remote generation and download complete."))
                    break
                elif status == 'failed':
                    error_details = data.get('error', 'Unknown server error.')
                    raise RuntimeError(f"Server reported job failure: {error_details}")
                else:
                    self.master.after(0, self.add_to_terminal, f"--> Status is '{status}'. Waiting...")
                    time.sleep(REMOTE_GEN_POLLING_INTERVAL_SEC)
            else:
                raise TimeoutError(f"Remote generation timed out after polling for {REMOTE_GEN_TOTAL_TIMEOUT_SEC} seconds.")

        except Exception as e:
            error_occurred = True
            error_msg = f"Remote generation failed: {e}"
            print(traceback.format_exc())
            self.master.after(0, self.add_to_terminal, f"ERROR: {error_msg}")
            self.master.after(0, lambda m=str(e): messagebox.showerror("Remote Generation Error", f"An error occurred: {m}"))

        finally:
            self.remote_generation_in_progress = False
            self.master.after(0, self._update_all_button_states)
            self.master.after(0, self.add_to_terminal, "Remote generation process finished.")


    def _submit_remote_job(self, server_url):
        """Prepares and sends the initial job request."""
        eph_to_use = self.config.get("ephemeris_file")
        if not eph_to_use or not os.path.exists(eph_to_use):
            if os.path.exists(LATEST_FILE_PATH):
                with open(LATEST_FILE_PATH, "r") as f:
                    eph_to_use = os.path.join(EPHEMERIS_DIR, f.read().strip())
            else: raise ValueError("No valid ephemeris file.")
        if not os.path.exists(eph_to_use):
            raise ValueError(f"Ephemeris file '{os.path.basename(eph_to_use)}' not found.")

        current_loc_mode = self.location_mode_var.get()
        duration = self.duration_slider.get() if hasattr(self, 'duration_slider') else self.config.get("duration", 60)

        params = {"duration": duration}
        if "Static" in current_loc_mode:
            lat, lon = self.latlon
            alt = self.altitude if self.altitude is not None else DEFAULT_ALTITUDE_METERS
            if lat is None or lon is None: raise ValueError("Latitude/Longitude not set.")
            params["location"] = f"{float(lat):.7f},{float(lon):.7f},{float(alt):.1f}"
        else: raise ValueError("Remote generation currently only supports 'Static' mode.")

        with open(eph_to_use, 'rb') as f:
            eph_data_encoded = base64.b64encode(f.read()).decode('utf-8')

        payload = {
            "params": params, "ephemeris_data": eph_data_encoded,
            "ephemeris_filename": os.path.basename(eph_to_use),
        }

        generate_url = f"{server_url}/generate"
        response = requests.post(generate_url, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        job_id = data.get("job_id")
        if not job_id:
            raise ValueError("Server did not return a valid Job ID.")
        return job_id

    def _download_remote_file(self, download_url, total_size):
        """Downloads the completed file from the server with progress updates."""
        try:
            self.master.after(0, self._show_download_progress)
            with requests.get(download_url, stream=True, timeout=REMOTE_GEN_TOTAL_TIMEOUT_SEC) as r:
                r.raise_for_status()
                if total_size is None:
                    total_size = int(r.headers.get('content-length', 0))

                downloaded_size = 0

                os.makedirs(os.path.dirname(SIM_OUTPUT), exist_ok=True)
                with open(SIM_OUTPUT, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if not self.remote_generation_in_progress:
                            self.master.after(0, self.add_to_terminal, "--> Download cancelled by user.")
                            return
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            self.master.after(0, self._update_download_progress, downloaded_size, total_size)

            self.master.after(0, self.add_to_terminal, f"--> File saved successfully to {SIM_OUTPUT}")
            self.master.after(0, self._update_status_bar)

        finally:
            self.master.after(0, self._hide_download_progress)

    def _fetch_real_drive_time(self):
        if self.start_latlon[0] is None or self.end_latlon[0] is None:
            messagebox.showerror("Error", "Look up both Start and End addresses first."); return
        self.add_to_terminal("Fetching real drive time from Google Directions...")
        def _fetch():
            api_key = self.config.get("Maps_api_key")
            waypoints, duration = get_road_route(self.start_latlon, self.end_latlon, api_key, log_fn=self.add_to_terminal)
            if duration is None:
                self.master.after(0, self.add_to_terminal, "Could not get drive time. Check API key.")
                self.master.after(0, lambda: self.route_time_label.config(text="Drive time fetch failed."))
                return
            mins = duration // 60; secs = duration % 60
            msg = f"Real drive time: {mins}m {secs}s ({duration}s)"
            self.master.after(0, self.add_to_terminal, msg)
            self.master.after(0, lambda: self.route_time_label.config(text=msg + " — set as duration"))
            if hasattr(self, 'duration_slider') and self.duration_slider.winfo_exists():
                clamped = max(10, min(duration, 3600))
                self.master.after(0, lambda v=clamped: self.duration_slider.set(v))
        threading.Thread(target=_fetch, daemon=True).start()

    def _generate_route_motion_file(self, start_coords, end_coords, start_alt, end_alt, duration_seconds, use_roads=True):
        if duration_seconds <= 0: self.add_to_terminal("Error: Duration for route must be positive."); return None
        num_points = max(2, int(duration_seconds * 10))
        hgt1 = float(start_alt); hgt2 = float(end_alt)
        os.makedirs(os.path.dirname(TEMP_ROUTE_MOTION_FILE), exist_ok=True)

        # Try road routing if requested
        waypoints = None
        if use_roads:
            api_key = self.config.get("Maps_api_key")
            # FIX: use cached route if start/end coords unchanged — avoids Directions API call on every Gen press
            start_key = (round(start_coords[0], 5), round(start_coords[1], 5))
            end_key   = (round(end_coords[0],   5), round(end_coords[1],   5))
            cache = getattr(self, '_last_road_route_cache', None)
            if cache and cache[0] == start_key and cache[1] == end_key:
                waypoints, real_dur = cache[2], cache[3]
                self.add_to_terminal(f"Road route (cached): {len(waypoints)} waypoints")
            else:
                waypoints, real_dur = get_road_route(start_coords, end_coords, api_key, log_fn=self.add_to_terminal)
                if waypoints:
                    self._last_road_route_cache = (start_key, end_key, waypoints, real_dur)
                    self.add_to_terminal(f"Road-following route: {len(waypoints)} waypoints")
                else:
                    self._last_road_route_cache = None
                    self.add_to_terminal("Road routing unavailable — using straight line.")

        try:
            with open(TEMP_ROUTE_MOTION_FILE, 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)
                if waypoints and len(waypoints) >= 2:
                    # Road-following: interpolate along polyline
                    cum_dist = [0.0]
                    for i in range(1, len(waypoints)):
                        lat1r, lon1r = math.radians(waypoints[i-1][0]), math.radians(waypoints[i-1][1])
                        lat2r, lon2r = math.radians(waypoints[i][0]),   math.radians(waypoints[i][1])
                        dlat = lat2r - lat1r; dlon = lon2r - lon1r
                        a = math.sin(dlat/2)**2 + math.cos(lat1r)*math.cos(lat2r)*math.sin(dlon/2)**2
                        cum_dist.append(cum_dist[-1] + 6371000.0 * 2 * math.asin(math.sqrt(a)))
                    total_dist = cum_dist[-1]

                    # ── Speed sanity check ──────────────────────────────────
                    # GPS receivers reject positions implying speed > ~90 mph.
                    # If the route distance exceeds what can be covered in
                    # duration_seconds at MAX_ROUTE_SPEED_MS, truncate the
                    # route to the reachable distance and warn the user.
                    MAX_ROUTE_SPEED_MS = 44.7  # 100 mph in m/s
                    max_dist = duration_seconds * MAX_ROUTE_SPEED_MS
                    if total_dist > max_dist:
                        # Find the waypoint index where we hit max_dist
                        trunc_idx = len(cum_dist) - 1
                        for j in range(1, len(cum_dist)):
                            if cum_dist[j] >= max_dist:
                                trunc_idx = j
                                break
                        waypoints  = waypoints[:trunc_idx + 1]
                        cum_dist   = cum_dist[:trunc_idx + 1]
                        total_dist = cum_dist[-1]
                        covered_km = total_dist / 1000.0
                        self.add_to_terminal(
                            f"WARNING: Route too long for {duration_seconds}s at 90 mph max. "
                            f"Truncated to {covered_km:.1f} km ({covered_km*0.621:.1f} mi). "
                            f"Reduce duration or pick a shorter route to cover more."
                        )

                    for i in range(num_points):
                        t        = i * 0.1
                        progress = min(t / duration_seconds, 1.0) if duration_seconds > 0 else 0
                        target_d = progress * total_dist
                        seg = 0
                        for j in range(1, len(cum_dist)):
                            if cum_dist[j] >= target_d: seg = j - 1; break
                        else: seg = len(waypoints) - 2
                        seg_len = cum_dist[seg+1] - cum_dist[seg]
                        r = (target_d - cum_dist[seg]) / seg_len if seg_len > 0 else 0.0
                        lat = waypoints[seg][0] + r * (waypoints[seg+1][0] - waypoints[seg][0])
                        lon = waypoints[seg][1] + r * (waypoints[seg+1][1] - waypoints[seg][1])
                        hgt = hgt1 + progress * (hgt2 - hgt1)
                        writer.writerow([f"{t:.1f}", f"{lat:.7f}", f"{lon:.7f}", f"{hgt:.1f}"])
                    self.add_to_terminal(f"Road-following motion file written ({num_points} points, {duration_seconds}s)")
                else:
                    # Straight-line fallback
                    lat1, lon1 = start_coords; lat2, lon2 = end_coords
                    # Speed sanity: clamp to 100 mph (44.7 m/s)
                    import math as _math2
                    def _hav(a, b):
                        R=6371000.0; la1,lo1=_math2.radians(a[0]),_math2.radians(a[1])
                        la2,lo2=_math2.radians(b[0]),_math2.radians(b[1])
                        dlat=la2-la1; dlon=lo2-lo1
                        h=_math2.sin(dlat/2)**2+_math2.cos(la1)*_math2.cos(la2)*_math2.sin(dlon/2)**2
                        return R*2*_math2.asin(_math2.sqrt(h))
                    sl_dist = _hav(start_coords, end_coords)
                    max_dist_sl = 44.7 * duration_seconds
                    if sl_dist > max_dist_sl:
                        ratio = max_dist_sl / sl_dist
                        lat2 = lat1 + ratio*(lat2-lat1); lon2 = lon1 + ratio*(lon2-lon1)
                        hgt2 = hgt1 + ratio*(hgt2-hgt1)
                        km = max_dist_sl/1000.0; miles = km*0.621371
                        self.add_to_terminal(
                            f"WARNING: Straight-line truncated to {km:.1f} km ({miles:.1f} mi) at 100 mph max.")
                    for i in range(num_points):
                        t_ratio = i / (num_points - 1) if num_points > 1 else 0
                        writer.writerow([f"{i*0.1:.1f}", f"{lat1 + t_ratio*(lat2-lat1):.7f}",
                                         f"{lon1 + t_ratio*(lon2-lon1):.7f}", f"{hgt1 + t_ratio*(hgt2-hgt1):.1f}"])
                    self.add_to_terminal(f"Straight-line motion file written ({num_points} points)")
            self.add_to_terminal(f"Route motion file: {os.path.basename(TEMP_ROUTE_MOTION_FILE)}")
            return TEMP_ROUTE_MOTION_FILE
        except Exception as e_write_csv:
            self.add_to_terminal(f"Error writing route motion file: {e_write_csv}"); return None

    def _start_hackrf(self, loop=False, is_blast_phase_override=False, blast_duration_override_sec=None, gain_override_db=None, is_auto_blast_cycle=False):
        if not is_blast_phase_override and not is_auto_blast_cycle and self.is_any_operation_active(exclude_spoof_loop=True): messagebox.showwarning("Busy", "Another operation is active."); return
        if self.running and not is_blast_phase_override and not is_auto_blast_cycle and not self.is_manual_blast_initial_phase: self.add_to_terminal("Simulation is already running."); return
        if self.gps_sim_proc and self.gps_sim_proc.poll() is None: messagebox.showwarning("Busy", "Signal generation is currently in progress."); return
        if not os.path.exists(SIM_OUTPUT) or os.path.getsize(SIM_OUTPUT) == 0: messagebox.showerror("Error", "GPS signal file (gpssim.c8) not found or empty."); return

        if not is_auto_blast_cycle:
            if self.map_update_timer_id and self.master and self.master.winfo_exists():
                self.master.after_cancel(self.map_update_timer_id); self.map_update_timer_id = None

        self.current_sim_gain_db = BLAST_GAIN_DB if is_blast_phase_override else \
                                  (gain_override_db if gain_override_db is not None else self.config.get("gain", 15))

        current_blast_duration_sec = self.config.get("blast_duration_sec", 3)
        if blast_duration_override_sec is not None: current_blast_duration_sec = blast_duration_override_sec

        rate_hz_str = str(DEFAULT_SAMPLE_RATE_HZ); freq_hz_str = str(self.config.get("frequency_hz", DEFAULT_FREQ_HZ_STR))
        cmd = [ HACKRF_TRANSFER_EXECUTABLE, "-t", SIM_OUTPUT, "-f", freq_hz_str, "-s", rate_hz_str, "-a", "1", "-x", str(self.current_sim_gain_db) ]
        effective_loop = loop and not is_blast_phase_override
        if effective_loop: cmd.append("-R")

        self.running = True; self.is_looping_active = effective_loop
        if not is_auto_blast_cycle:
            self.is_manual_blast_initial_phase = is_blast_phase_override
            if is_blast_phase_override: self.intended_loop_state_after_manual_blast = loop

        try:
            self.add_to_terminal(f"Executing HackRF command: {' '.join(shlex.quote(arg) for arg in cmd)}")
            current_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True)
            self.proc = current_process
            self.add_to_terminal(f"HackRF process started with PID: {self.proc.pid if self.proc else 'Unknown'}")
            self._update_all_button_states()

            current_loc_mode_for_playback = self.location_mode_var.get()
            if "Static" in current_loc_mode_for_playback and not is_blast_phase_override and not is_auto_blast_cycle:
                static_lat, static_lon = self.latlon
                static_alt = self.altitude if self.altitude is not None else DEFAULT_ALTITUDE_METERS
                if static_lat is not None and static_lon is not None:
                    self.add_to_terminal(f"Static Sim: Gain: {self.current_sim_gain_db}dB | Lat: {static_lat:.4f}, Lon: {static_lon:.4f}, Alt: {static_alt:.1f}m")

            if not is_blast_phase_override:
                if ("Route" in current_loc_mode_for_playback or "User Motion (LLH .csv)" in current_loc_mode_for_playback) and \
                   self.motion_playback_timeline and self.active_signal_duration_sec > 0:
                    self.playback_start_time = time.time()
                    self._update_map_during_playback()
                elif not self.active_map_enabled.get():
                    self.map_playback_center_latlon = None; self.update_map()

            if is_blast_phase_override:
                if self.after_manual_blast_id and not is_auto_blast_cycle and self.master and self.master.winfo_exists(): self.master.after_cancel(self.after_manual_blast_id)
                timer_duration_ms = int(current_blast_duration_sec * 1000)
                blast_proc_pid_for_timer = self.proc.pid if self.proc else None
                self.add_to_terminal(f"Scheduling blast transition for PID: {blast_proc_pid_for_timer} in {current_blast_duration_sec}s.")
                if self.master and self.master.winfo_exists():
                    self.after_manual_blast_id = self.master.after(timer_duration_ms, lambda pid=blast_proc_pid_for_timer: self._transition_from_blast_phase(is_auto_blast_cycle_arg=is_auto_blast_cycle, expected_blast_pid=pid))
            else:
                threading.Thread(target=self._reader_thread_for_hackrf, args=(self.proc, self), daemon=True).start()
                if self.auto_blast_enabled.get() and not self.auto_blast_active_phase and not is_auto_blast_cycle: self._schedule_next_auto_blast()

            log_msg = "HackRF command initiated."
            if is_blast_phase_override: log_msg += f" (Blast Phase PID: {self.proc.pid if self.proc else 'N/A'}, Gain {self.current_sim_gain_db}dB for {current_blast_duration_sec}s)"
            if effective_loop: log_msg += " (Looping)"
            if is_auto_blast_cycle: log_msg += " (Auto Blast Cycle)"
            self.add_to_terminal(log_msg)

        except FileNotFoundError:
            messagebox.showerror("Error", f"{HACKRF_TRANSFER_EXECUTABLE} not found. Check PATH."); self.add_to_terminal(f"Error: {HACKRF_TRANSFER_EXECUTABLE} not found.")
            self.running = False; self.is_manual_blast_initial_phase = False; self.is_looping_active = False; self.proc = None; self.current_sim_gain_db = None;
            if is_auto_blast_cycle: self.auto_blast_active_phase = False
            self._update_all_button_states()
            if self.map_update_timer_id and self.master and self.master.winfo_exists(): self.master.after_cancel(self.map_update_timer_id); self.map_update_timer_id = None
            self.map_playback_center_latlon = None; self.update_map()
        except Exception as e_hackrf:
            messagebox.showerror("Error", f"Failed to start HackRF: {e_hackrf}"); self.add_to_terminal(f"Error starting HackRF: {e_hackrf}\n{traceback.format_exc()}")
            self.running = False; self.is_manual_blast_initial_phase = False; self.is_looping_active = False; self.proc = None; self.current_sim_gain_db = None;
            if is_auto_blast_cycle: self.auto_blast_active_phase = False
            self._update_all_button_states()
            if self.map_update_timer_id and self.master and self.master.winfo_exists(): self.master.after_cancel(self.map_update_timer_id); self.map_update_timer_id = None
            self.map_playback_center_latlon = None; self.update_map()

    def _transition_from_blast_phase(self, is_auto_blast_cycle_arg=False, expected_blast_pid=None):
        self.add_to_terminal(f"Blast phase transition. Expected PID: {expected_blast_pid}, Current PID: {self.proc.pid if self.proc else 'None'}.")
        current_proc_pid = self.proc.pid if self.proc else None
        if expected_blast_pid is not None and current_proc_pid != expected_blast_pid:
            self.add_to_terminal(f"Transition aborted for blast PID {expected_blast_pid}: Current PID ({current_proc_pid}) mismatch.");
            if self.after_manual_blast_id and self.master and self.master.winfo_exists(): self.master.after_cancel(self.after_manual_blast_id); self.after_manual_blast_id = None
            return
        if self.proc and self.proc.poll() is None:
            blast_proc_to_stop = self.proc
            self.add_to_terminal(f"Stopping blast PID: {blast_proc_to_stop.pid} (matches expected: {expected_blast_pid}).")
            try:
                blast_proc_to_stop.terminate(); blast_proc_to_stop.wait(timeout=1.5)
                self.add_to_terminal(f"Blast PID: {blast_proc_to_stop.pid} terminated. Code: {blast_proc_to_stop.returncode}")
            except subprocess.TimeoutExpired:
                self.add_to_terminal(f"Blast PID: {blast_proc_to_stop.pid} timed out, killing.")
                blast_proc_to_stop.kill(); blast_proc_to_stop.wait()
                self.add_to_terminal(f"Blast PID: {blast_proc_to_stop.pid} killed. Code: {blast_proc_to_stop.returncode}")
            except Exception as e_stop_blast: self.add_to_terminal(f"Error stopping blast PID: {blast_proc_to_stop.pid}: {e_stop_blast}")
        elif self.proc: self.add_to_terminal(f"Blast PID: {self.proc.pid} (expected: {expected_blast_pid}) already finished. Code: {self.proc.returncode}.")
        else: self.add_to_terminal(f"No current process for blast transition (expected PID: {expected_blast_pid}).")
        self.proc = None; self.running = False
        if self.after_manual_blast_id and self.master and self.master.winfo_exists(): self.master.after_cancel(self.after_manual_blast_id); self.after_manual_blast_id = None
        if is_auto_blast_cycle_arg: self.is_manual_blast_initial_phase = False; self._finish_auto_blast_cycle()
        else:
            self.is_manual_blast_initial_phase = False; loop_after_manual_blast = self.intended_loop_state_after_manual_blast
            restored_gain_after_manual_blast = self.config.get("gain", 15)
            self.add_to_terminal(f"Transitioning from manual blast. Gain: {restored_gain_after_manual_blast}dB, Loop: {loop_after_manual_blast}")
            self._start_hackrf(loop=loop_after_manual_blast, is_blast_phase_override=False, gain_override_db=restored_gain_after_manual_blast)

    def _reader_thread_for_hackrf(self, process_ref, app_instance_ref):
        process_pid = process_ref.pid if process_ref else "UnknownPID"
        app_instance_ref.add_to_terminal(f"Reader thread started for HackRF PID: {process_pid}.")
        try:
            if process_ref.stdout:
                for line in iter(process_ref.stdout.readline, ''):
                    if line and app_instance_ref.master and app_instance_ref.master.winfo_exists():
                        app_instance_ref.master.after(0, app_instance_ref.add_to_terminal, line.strip())
                process_ref.stdout.close()
            ret_code = process_ref.wait()
            if app_instance_ref.master and app_instance_ref.master.winfo_exists():
                app_instance_ref.master.after(0, app_instance_ref.add_to_terminal, f"HackRF process (PID {process_pid}) finished. Code: {ret_code}.")
        except Exception as e_reader:
            if app_instance_ref.master and app_instance_ref.master.winfo_exists():
                app_instance_ref.master.after(0, app_instance_ref.add_to_terminal, f"Error in HackRF reader (PID {process_pid}): {e_reader}")
        finally:
            app_instance_ref.add_to_terminal(f"Reader thread ending for HackRF PID: {process_pid}.")
            if app_instance_ref.proc == process_ref and not app_instance_ref.is_manual_blast_initial_phase and not app_instance_ref.auto_blast_active_phase:
                app_instance_ref.add_to_terminal(f"Main simulation (PID {process_pid}) ended. Cleaning GUI state.")
                app_instance_ref.running = False; app_instance_ref.proc = None; app_instance_ref.is_looping_active = False; app_instance_ref.current_sim_gain_db = None;
                if app_instance_ref.master and app_instance_ref.master.winfo_exists():
                    if app_instance_ref.map_update_timer_id: app_instance_ref.master.after_cancel(app_instance_ref.map_update_timer_id); app_instance_ref.map_update_timer_id = None
                    current_loc_mode_final = app_instance_ref.location_mode_var.get()
                    if app_instance_ref.active_map_enabled.get() and \
                       ("Route" in current_loc_mode_final or "User Motion (LLH .csv)" in current_loc_mode_final) and \
                       app_instance_ref.motion_playback_timeline and app_instance_ref.active_signal_duration_sec > 0:
                        last_valid_point = app_instance_ref.motion_playback_timeline[0]
                        for pt_data in app_instance_ref.motion_playback_timeline:
                            if pt_data[0] <= app_instance_ref.active_signal_duration_sec: last_valid_point = pt_data
                            else: break
                        app_instance_ref.map_playback_center_latlon = (last_valid_point[1], last_valid_point[2])
                        app_instance_ref.master.after(0, app_instance_ref.update_map)
                        display_gain_final = BLAST_GAIN_DB if self.is_manual_blast_initial_phase or self.auto_blast_active_phase else \
                                         (self.current_sim_gain_db if self.current_sim_gain_db is not None else self.config.get("gain", 15))
                        self.master.after(0, self.add_to_terminal, f"Playback End: T+{last_valid_point[0]:<5.1f}s | Gain: {display_gain_final if display_gain_final is not None else '--':<2}dB | Lat: {last_valid_point[1]:<8.4f}, Lon: {last_valid_point[2]:<9.4f}, Alt: {last_valid_point[3]:<6.1f}m")

                    elif not app_instance_ref.active_map_enabled.get():
                        app_instance_ref.map_playback_center_latlon = None; app_instance_ref.master.after(0, app_instance_ref.update_map)
                    app_instance_ref._cancel_auto_blast_timer(); app_instance_ref.master.after(0, app_instance_ref._update_all_button_states)
            elif app_instance_ref.proc != process_ref: app_instance_ref.add_to_terminal(f"Reader for PID {process_pid} ending, but current self.proc is {app_instance_ref.proc.pid if app_instance_ref.proc else 'None'}.")
            else: app_instance_ref.add_to_terminal(f"Reader for PID {process_pid} ending (was blast/auto-blast). Cleanup by transition logic.")

    def start_spoofing(self):
        if self.glo_jam_enabled.get():
            self._run_glo_jam_then_hackrf(loop=False)
        else:
            self._start_hackrf(loop=False, is_blast_phase_override=True)

    def _loop_stream(self):
        """Stream mode loop — pipes gps-sdr-sim stdout to hackrf_transfer continuously."""
        if self.is_any_operation_active():
            messagebox.showwarning("Busy", "Another operation is active. Please stop it first.")
            return

        _cores = int(self.config.get("gen_cores", 1))
        _use_exec = GPS_SDR_SIM_4CORE_EXECUTABLE if _cores > 1 else GPS_SDR_SIM_EXECUTABLE
        import os as _os; _sim_env = _os.environ.copy(); _sim_env['GPSSIM_NTHREADS'] = str(_cores)

        eph_to_use = self.config.get("ephemeris_file")
        if not eph_to_use or not os.path.exists(eph_to_use):
            if os.path.exists(LATEST_FILE_PATH):
                with open(LATEST_FILE_PATH, "r") as f: eph_to_use = os.path.join(EPHEMERIS_DIR, f.read().strip())
            else:
                messagebox.showerror("Error", "No valid ephemeris file."); return

        current_loc_mode = self.location_mode_var.get()
        duration_from_slider = self.duration_slider.get() if hasattr(self, 'duration_slider') else self.config.get("duration", 60)

        def _build_args():
            args = [_use_exec, "-e", eph_to_use]
            if os.path.exists(LATEST_TIME_PATH):
                with open(LATEST_TIME_PATH, "r") as tf:
                    t_stamp = tf.read().strip()
                if t_stamp: args.extend(["-t", t_stamp])
            if "Static" in current_loc_mode:
                lat, lon = self.latlon; alt = self.altitude if self.altitude is not None else DEFAULT_ALTITUDE_METERS
                args.extend(["-l", f"{float(lat):.7f},{float(lon):.7f},{float(alt):.1f}"])
                args.extend(["-d", str(duration_from_slider)])
            elif "Route" in current_loc_mode:
                start_alt = self.start_altitude if self.start_altitude is not None else DEFAULT_ALTITUDE_METERS
                end_alt = self.end_altitude if self.end_altitude is not None else DEFAULT_ALTITUDE_METERS
                motion_file = self._generate_route_motion_file(self.start_latlon, self.end_latlon, start_alt, end_alt, duration_from_slider, use_roads=self.use_roads_var.get())
                if not motion_file: return None
                args.extend(["-x", motion_file, "-d", str(min(duration_from_slider, 3600))])
            else:
                motion_file = self.motion_file_path_var.get()
                if not motion_file or not os.path.exists(motion_file): return None
                if "ECEF" in current_loc_mode: args.extend(["-u", motion_file])
                elif "LLH" in current_loc_mode: args.extend(["-x", motion_file])
                elif "NMEA" in current_loc_mode: args.extend(["-g", motion_file])
                args.extend(["-d", str(min(duration_from_slider, 3600))])
            args.extend(["-b", "8", "-o", "-"])
            return args

        gain = int(self.config.get("gain", 43))
        freq_hz = int(self.config.get("frequency_hz", 1575420000))
        hackrf_cmd = ["hackrf_transfer", "-t", "/dev/stdin", "-f", str(freq_hz),
                      "-s", "2600000", "-a", "1", "-x", str(gain)]

        self.add_to_terminal("Stream loop starting...")

        def _run_loop():
            import subprocess as _sp
            hackrf_proc = None
            try:
                hackrf_proc = _sp.Popen(hackrf_cmd, stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.STDOUT)
                self.proc = hackrf_proc
                self.running = True
                self.master.after(0, self._update_all_button_states)
                self.add_to_terminal(f"HackRF PID={hackrf_proc.pid}")
                while self.running:
                    args = _build_args()
                    if not args:
                        self.master.after(0, self.add_to_terminal, "Error building stream args.")
                        break
                    gen_proc = _sp.Popen(args, stdout=_sp.PIPE, stderr=_sp.PIPE, env=_sim_env)
                    self.add_to_terminal(f"Gen PID={gen_proc.pid} streaming...")
                    try:
                        while self.running:
                            chunk = gen_proc.stdout.read(65536)
                            if not chunk: break
                            hackrf_proc.stdin.write(chunk)
                            hackrf_proc.stdin.flush()
                    except BrokenPipeError:
                        break
                    gen_proc.stdout.close()
                    gen_proc.wait()
                    if not self.running: break
                    self.add_to_terminal("Loop: restarting route...")
            except Exception as e:
                self.master.after(0, self.add_to_terminal, f"Stream loop error: {e}")
            finally:
                if hackrf_proc:
                    try: hackrf_proc.stdin.close()
                    except: pass
                    try: hackrf_proc.kill()
                    except: pass
                    hackrf_proc.wait()
                self.running = False
                self.proc = None
                self.master.after(0, self._update_all_button_states)
                self.master.after(0, self.add_to_terminal, "Stream loop ended.")

        import threading
        threading.Thread(target=_run_loop, daemon=True).start()

    def loop_signal(self):
        if self.stream_mode.get():
            self._loop_stream()
            return
        if self.glo_jam_enabled.get():
            self._run_glo_jam_then_hackrf(loop=True)
        else:
            self._start_hackrf(loop=True, is_blast_phase_override=True)

    def toggle_glo_jam(self):
        self.glo_jam_enabled.set(not self.glo_jam_enabled.get())
        self.config["glo_jam_enabled"] = self.glo_jam_enabled.get()
        save_config(self.config)
        self._update_glo_jam_button_style()
        self.add_to_terminal(f"GLO Jam {'Enabled — Sim/Loop will jam GLONASS first' if self.glo_jam_enabled.get() else 'Disabled'}.")

    def _update_glo_jam_button_style(self):
        btn_key = 'glo_jam'
        if hasattr(self, 'action_buttons') and btn_key in self.action_buttons and self.action_buttons[btn_key].winfo_exists():
            if self.glo_jam_enabled.get():
                self.action_buttons[btn_key].config(style='ActiveGloJam.TButton')
            else:
                self.action_buttons[btn_key].config(style='TButton')

    def update_glo_jam_duration(self, val_str):
        s_val = int(float(val_str))
        if hasattr(self, 'glo_jam_label') and self.glo_jam_label.winfo_exists():
            self.glo_jam_label.config(text=f"{s_val}s")
        self.glo_jam_duration_sec = s_val
        self.config["glo_jam_duration_sec"] = s_val
        save_config(self.config)

    def _run_glo_jam_then_hackrf(self, loop=False):
        """Jam GLONASS for N seconds then hand off to normal _start_hackrf."""
        if self.is_any_operation_active():
            messagebox.showwarning("Busy", "Another operation is active. Stop it first.")
            return
        if not os.path.exists(SIM_OUTPUT) or os.path.getsize(SIM_OUTPUT) == 0:
            messagebox.showerror("Error", "GPS sim file not found or empty. Generate first.")
            return
        jam_sec = self.glo_jam_duration_sec
        self.glo_jam_active = True
        self._update_all_button_states()
        self.add_to_terminal(f"GLO Jam: Jamming GLONASS at {GLO_JAM_FREQ_HZ/1e6:.0f} MHz for {jam_sec}s...")
        threading.Thread(target=self._glo_jam_thread, args=(jam_sec, loop), daemon=True).start()

    def _glo_jam_thread(self, jam_sec, loop):
        try:
            jam_cmd = [
                HACKRF_TRANSFER_EXECUTABLE,
                "-f", str(GLO_JAM_FREQ_HZ),
                "-s", str(GLO_JAM_SAMPLE_RATE),
                "-a", "1",
                "-x", str(GLO_JAM_GAIN_DB),
                "-t", "/dev/urandom"
            ]
            self.master.after(0, self.add_to_terminal,
                f"Jam CMD: {' '.join(shlex.quote(a) for a in jam_cmd)}")
            self.glo_jam_proc = subprocess.Popen(
                jam_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            self.master.after(0, self.add_to_terminal,
                f"GLONASS jam running (PID {self.glo_jam_proc.pid})...")
            time.sleep(jam_sec)
            # Stop jammer
            if self.glo_jam_proc and self.glo_jam_proc.poll() is None:
                self.glo_jam_proc.terminate()
                try: self.glo_jam_proc.wait(timeout=2)
                except subprocess.TimeoutExpired: self.glo_jam_proc.kill(); self.glo_jam_proc.wait()
            self.glo_jam_proc = None
            self.master.after(0, self.add_to_terminal, "Jam complete. Releasing HackRF (2s)...")
            time.sleep(2)
            # Hand off to normal GPS spoof
            self.glo_jam_active = False
            self.master.after(0, self.add_to_terminal, "Starting GPS spoof...")
            self.master.after(0, self._start_hackrf, loop, True)  # loop, is_blast_phase_override
        except Exception as e:
            self.master.after(0, self.add_to_terminal, f"GLO Jam error: {e}")
            self.glo_jam_active = False
            self.glo_jam_proc = None
            self.master.after(0, self._update_all_button_states)


    def stop_spoofing(self):
        action_taken = False; self.add_to_terminal("Stop command. Halting operations...")
        if self.glo_jam_active or self.glo_jam_proc:
            self.add_to_terminal("Stopping GLONASS jam...")
            self.glo_jam_active = False
            if self.glo_jam_proc and self.glo_jam_proc.poll() is None:
                try:
                    self.glo_jam_proc.terminate()
                    self.glo_jam_proc.wait(timeout=1)
                except Exception: self.glo_jam_proc.kill()
            self.glo_jam_proc = None
            action_taken = True
        self._cancel_auto_blast_timer()
        if self.auto_blast_active_phase: self.add_to_terminal("Auto Blast active phase stopped."); action_taken = True
        self.auto_blast_active_phase = False
        if self.map_update_timer_id and self.master and self.master.winfo_exists():
            self.master.after_cancel(self.map_update_timer_id); self.map_update_timer_id = None
            self.add_to_terminal("Map/Terminal coord update timer stopped."); action_taken = True
        self.playback_start_time = None
        if self.after_manual_blast_id is not None and self.master and self.master.winfo_exists():
            self.master.after_cancel(self.after_manual_blast_id); self.after_manual_blast_id = None
            self.add_to_terminal("Manual blast transition timer cancelled."); action_taken = True
        if self.is_manual_blast_initial_phase: self.add_to_terminal("Manual blast initial phase stopped."); action_taken = True
        self.is_manual_blast_initial_phase = False
        current_gps_sim_proc = self.gps_sim_proc
        if isinstance(current_gps_sim_proc, subprocess.Popen) and current_gps_sim_proc.poll() is None:
            self.add_to_terminal(f"Stopping gps-sdr-sim (PID: {current_gps_sim_proc.pid})..."); action_taken = True
            try:
                current_gps_sim_proc.terminate()
                try: current_gps_sim_proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired: current_gps_sim_proc.kill(); current_gps_sim_proc.wait()
                self.add_to_terminal("gps-sdr-sim stop request sent.")
            except Exception as e_stop_gen: self.add_to_terminal(f"Error stopping gps-sdr-sim: {e_stop_gen}")
        self.gps_sim_proc = None

        if self.remote_generation_in_progress:
            self.add_to_terminal("Stopping remote generation process..."); action_taken = True
            self.remote_generation_in_progress = False

        current_hackrf_proc = self.proc; hackrf_pid_to_stop = current_hackrf_proc.pid if current_hackrf_proc else "None"
        if isinstance(current_hackrf_proc, subprocess.Popen) and current_hackrf_proc.poll() is None:
            self.add_to_terminal(f"Stopping HackRF (PID: {hackrf_pid_to_stop})..."); action_taken = True
            try:
                current_hackrf_proc.terminate()
                try: current_hackrf_proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired: current_hackrf_proc.kill(); current_hackrf_proc.wait()
                self.add_to_terminal(f"HackRF (PID: {hackrf_pid_to_stop}) stop request sent.")
            except Exception as e_stop_hackrf: self.add_to_terminal(f"Error stopping HackRF (PID: {hackrf_pid_to_stop}): {e_stop_hackrf}")
        elif self.running : self.add_to_terminal(f"HackRF (Expected PID: {hackrf_pid_to_stop}) was running but process already stopped/transitioned. Resetting flags."); action_taken = True
        self.proc = None
        if self.transfer_in_progress or self.custom_transfer_in_progress:
            self.add_to_terminal("Cancelling active file transfer..."); action_taken = True
            if self.transfer_watchdog_id is not None and self.master and self.master.winfo_exists(): self.master.after_cancel(self.transfer_watchdog_id); self.transfer_watchdog_id = None
            if self.custom_transfer_watchdog_id is not None and self.master and self.master.winfo_exists(): self.master.after_cancel(self.custom_transfer_watchdog_id); self.custom_transfer_watchdog_id = None
            if self.transfer_progress_timer_id is not None and self.master and self.master.winfo_exists(): self.master.after_cancel(self.transfer_progress_timer_id); self.transfer_progress_timer_id = None
            self.add_to_terminal("Transfer GUI tracking stopped. Background OS operations may continue or be interrupted.")
        if self.ephemeris_update_running:
            self.add_to_terminal("Ephemeris update active. Resetting GUI flag (thread may continue)."); action_taken = True
            self.ephemeris_update_running = False
        self.running = False; self.is_looping_active = False; self.transfer_in_progress = False; self.custom_transfer_in_progress = False; self.current_sim_gain_db = None;
        if not action_taken and not self.is_any_operation_active(): self.add_to_terminal("No operations were active.")
        if not self.active_map_enabled.get(): self.map_playback_center_latlon = None
        self.update_map(); self._update_all_button_states(); self.add_to_terminal("All stoppable operations halted and GUI states reset.")

    def _start_transfer_ui_feedback(self, is_custom_transfer):
        """Handles UI updates when a transfer starts."""
        if hasattr(self, 'transfer_progressbar') and self.transfer_progressbar.winfo_exists():
            self.transfer_progressbar.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
            self.transfer_progressbar.start(10)

        watchdog_id_attr = "custom_transfer_watchdog_id" if is_custom_transfer else "transfer_watchdog_id"
        if getattr(self, watchdog_id_attr, None) is not None and self.master and self.master.winfo_exists():
            self.master.after_cancel(getattr(self, watchdog_id_attr))

        new_watchdog_id = self.master.after(TRANSFER_WATCHDOG_TIMEOUT_MS, lambda: self._check_transfer_timeout(is_custom_transfer))
        setattr(self, watchdog_id_attr, new_watchdog_id)

        self.transfer_start_time = time.time()
        if self.transfer_progress_timer_id is not None and self.master and self.master.winfo_exists():
            self.master.after_cancel(self.transfer_progress_timer_id)
        self._update_transfer_progress_timer(is_custom_transfer)

    def _stop_transfer_ui_feedback(self, is_custom_transfer):
        """Handles UI updates when a transfer stops (success or fail)."""
        if hasattr(self, 'transfer_progressbar') and self.transfer_progressbar.winfo_exists():
            self.transfer_progressbar.stop()
            self.transfer_progressbar.pack_forget()

        watchdog_id_attr = "custom_transfer_watchdog_id" if is_custom_transfer else "transfer_watchdog_id"
        current_watchdog_id = getattr(self, watchdog_id_attr, None)
        if current_watchdog_id is not None and self.master and self.master.winfo_exists():
            self.master.after_cancel(current_watchdog_id)
            setattr(self, watchdog_id_attr, None)

        if self.transfer_progress_timer_id and self.master and self.master.winfo_exists():
            self.master.after_cancel(self.transfer_progress_timer_id)
            self.transfer_progress_timer_id = None

        transfer_flag_attr = "custom_transfer_in_progress" if is_custom_transfer else "transfer_in_progress"
        setattr(self, transfer_flag_attr, False)
        self._update_all_button_states()


    def prompt_and_transfer_to_hackrf(self):
        if self.transfer_in_progress or self.custom_transfer_in_progress: messagebox.showwarning("In Progress", "File transfer already in progress."); return
        if self.is_any_operation_active(exclude_transfer_sim=True, exclude_transfer_custom=True): messagebox.showwarning("Busy", "Another operation active."); return
        if not os.path.exists(SIM_OUTPUT) or os.path.getsize(SIM_OUTPUT) == 0: messagebox.showerror("Error", "gpssim.c8 not found or empty."); self.add_to_terminal("Transfer Error: gpssim.c8 not found/empty."); return

        user_response = messagebox.askokcancel("HackRF SD/USB Mode", f"Ensure HackRF is in 'SD/USB Mass Storage' mode OR use a standard SD card reader.\n\nCopy '{os.path.basename(SIM_OUTPUT)}' to: {HACKRF_SD_GPS_PATH}?\n\nThis process uses OS commands for copy and size check.\nIMPORTANT: You MUST manually unmount the SD card from the OS after this operation completes to ensure data is saved.\n\nThis can take time. GUI may appear unresponsive.\n\nOK to proceed, or Cancel.")
        if not user_response: self.add_to_terminal("File transfer to HackRF SD cancelled."); return

        self.transfer_in_progress = True; self.current_transfer_filename = os.path.basename(SIM_OUTPUT); self._update_all_button_states()
        self.add_to_terminal(f"Starting OS-level transfer of {self.current_transfer_filename} to SD target...")
        self._start_transfer_ui_feedback(is_custom_transfer=False)
        threading.Thread(target=self._perform_transfer_thread, args=(SIM_OUTPUT, False), daemon=True).start()

    def prompt_and_transfer_custom_file(self):
        if self.transfer_in_progress or self.custom_transfer_in_progress: messagebox.showwarning("In Progress", "File transfer already in progress."); return
        if self.is_any_operation_active(exclude_transfer_sim=True, exclude_transfer_custom=True): messagebox.showwarning("Busy", "Another operation active."); return

        source_file_path = filedialog.askopenfilename(title="Select File to Transfer to HackRF SD", filetypes=(("All files", "*.*"),))
        if not source_file_path: self.add_to_terminal("Custom file transfer cancelled (no file selected)."); return
        if not os.path.exists(source_file_path) or os.path.getsize(source_file_path) == 0: messagebox.showerror("Error", f"Selected file '{os.path.basename(source_file_path)}' not found or empty."); self.add_to_terminal(f"Custom Transfer Error: Selected file '{source_file_path}' not found/empty."); return

        user_response = messagebox.askokcancel("HackRF SD/USB Mode", f"Ensure HackRF is in 'SD/USB Mass Storage' mode OR use a standard SD card reader.\n\nCopy '{os.path.basename(source_file_path)}' to: {HACKRF_SD_GPS_PATH}?\n\nThis process uses OS commands for copy and size check.\nIMPORTANT: You MUST manually unmount the SD card from the OS after this operation completes to ensure data is saved.\n\nThis can take time. GUI may appear unresponsive.\n\nOK to proceed, or Cancel.")
        if not user_response: self.add_to_terminal("Custom file transfer to HackRF SD cancelled."); return

        self.custom_transfer_in_progress = True; self.current_transfer_filename = os.path.basename(source_file_path); self._update_all_button_states()
        self.add_to_terminal(f"Starting OS-level custom file transfer of '{self.current_transfer_filename}' to SD target...")
        self._start_transfer_ui_feedback(is_custom_transfer=True)
        threading.Thread(target=self._perform_transfer_thread, args=(source_file_path, True), daemon=True).start()

    def _update_transfer_progress_timer(self, is_custom=False):
        transfer_flag_attr = "custom_transfer_in_progress" if is_custom else "transfer_in_progress"
        if getattr(self, transfer_flag_attr) and self.master and self.master.winfo_exists():
            elapsed_time = int(time.time() - self.transfer_start_time)
            button_to_update = self.action_buttons["file->sd"] if is_custom else self.action_buttons["to_sd"]
            if hasattr(button_to_update, 'winfo_exists') and button_to_update.winfo_exists():
                current_text = button_to_update.cget("text"); base_text = "Xfer File" if is_custom else "Copying"
                if base_text in current_text or "..." in current_text: button_to_update.config(text=f"{base_text}...{elapsed_time}s")
            if self.master and self.master.winfo_exists(): self.transfer_progress_timer_id = self.master.after(1000, lambda: self._update_transfer_progress_timer(is_custom))
        else:
            if self.transfer_progress_timer_id and self.master and self.master.winfo_exists(): self.master.after_cancel(self.transfer_progress_timer_id)
            self.transfer_progress_timer_id = None

    def _run_os_command(self, cmd_list, timeout, description, shell_cmd=None, input_data=None):
        log_cmd = shell_cmd if shell_cmd else ' '.join(shlex.quote(arg) for arg in cmd_list)
        if self.master and self.master.winfo_exists():
            self.master.after(0, self.add_to_terminal, f"Executing: {description} -> {log_cmd}")

        try:
            process = subprocess.run(cmd_list, check=True, capture_output=True, text=True, timeout=timeout, input=input_data, shell=isinstance(shell_cmd, str))
            stdout = process.stdout.strip() if process.stdout else ""
            stderr = process.stderr.strip() if process.stderr else ""
            log_output = f"{description} successful." + (f"\nStdout: {stdout}" if stdout else "") + (f"\nStderr: {stderr}" if stderr else "")
            if self.master and self.master.winfo_exists(): self.master.after(0, self.add_to_terminal, log_output)
            return True, stdout, stderr
        except FileNotFoundError:
            cmd_name = cmd_list[0] if cmd_list else "command"
            err_msg = f"ERROR: Command for '{description}' ('{cmd_name}') not found."
            if self.master and self.master.winfo_exists(): self.master.after(0, self.add_to_terminal, err_msg)
            return False, "", err_msg
        except subprocess.TimeoutExpired:
            err_msg = f"ERROR: Command for '{description}' timed out after {timeout} seconds."
            if self.master and self.master.winfo_exists(): self.master.after(0, self.add_to_terminal, err_msg)
            return False, "", err_msg
        except subprocess.CalledProcessError as e:
            stdout = e.stdout.strip() if e.stdout else ""
            stderr = e.stderr.strip() if e.stderr else ""
            err_msg = f"ERROR: Command for '{description}' failed (Code: {e.returncode}).{f' Stdout: {stdout}' if stdout else ''}{f' Stderr: {stderr}' if stderr else ''}"
            if self.master and self.master.winfo_exists(): self.master.after(0, self.add_to_terminal, err_msg)
            return False, stdout, err_msg
        except Exception as e_gen:
            err_msg = f"ERROR: Unexpected error during '{description}': {str(e_gen)}"
            if self.master and self.master.winfo_exists(): self.master.after(0, self.add_to_terminal, err_msg)
            return False, "", err_msg

    def _perform_transfer_thread(self, source_file_path, is_custom_transfer=False):
        error_accumulator = []
        success_flags = {"df_check": False, "mkdir": False, "cp": False, "size_check": False}

        dest_gps_folder = HACKRF_SD_GPS_PATH
        source_filename = os.path.basename(source_file_path)
        dest_file_full_path = os.path.join(dest_gps_folder, source_filename)
        actual_mount_point = os.path.dirname(dest_gps_folder.rstrip(os.sep))

        try:
            if not os.path.ismount(actual_mount_point):
                raise Exception(f"SD Card mount point ('{actual_mount_point}') not found.")
            if not os.path.exists(source_file_path):
                raise Exception(f"Source file '{source_filename}' does not exist.")

            df_success, df_stdout, _ = self._run_os_command(["df", "--block-size=1", "--output=avail", actual_mount_point], DF_CMD_TIMEOUT, "Disk space check")
            if df_success:
                available_space_bytes = int(df_stdout.strip().splitlines()[-1].strip())
                if available_space_bytes < os.path.getsize(source_file_path):
                    raise Exception("Not enough free space on SD card.")
                success_flags["df_check"] = True
            else:
                raise Exception("Disk space check failed.")

            mkdir_success, _, _ = self._run_os_command(["mkdir", "-p", dest_gps_folder], MKDIR_CMD_TIMEOUT, "Create directory")
            if not mkdir_success: raise Exception("mkdir failed")
            success_flags["mkdir"] = True

            cp_success, _, _ = self._run_os_command(["cp", "-a", "-v", source_file_path, dest_gps_folder], CP_CMD_TIMEOUT, "Copy file")
            if not cp_success: raise Exception("cp failed")
            success_flags["cp"] = True

            source_size = os.path.getsize(source_file_path)
            dest_size = os.path.getsize(dest_file_full_path)
            if source_size == dest_size:
                success_flags["size_check"] = True
            else:
                raise Exception(f"File size mismatch! Source: {source_size} B, Dest: {dest_size} B.")

            self.master.after(0, self.add_to_terminal, "IMPORTANT: Manually unmount SD card from OS.")

        except Exception as e:
            error_accumulator.append(str(e))

        final_message = f"Transfer of '{source_filename}' " + ("succeeded." if all(success_flags.values()) else "failed.")
        if error_accumulator:
            final_message += "\n\nErrors:\n" + "\n".join(error_accumulator)

        self.master.after(0, self._stop_transfer_ui_feedback, is_custom_transfer)
        self.master.after(10, lambda: messagebox.showinfo("Transfer Status", final_message))

    def _check_transfer_timeout(self, is_custom=False):
        watchdog_attr = "custom_transfer_watchdog_id" if is_custom else "transfer_watchdog_id"
        transfer_flag_attr = "custom_transfer_in_progress" if is_custom else "transfer_in_progress"
        if getattr(self, transfer_flag_attr):
            self.add_to_terminal(f"WARNING: GUI watchdog timeout for file transfer.")
            self.master.after(0, self._stop_transfer_ui_feedback, is_custom)
            messagebox.showwarning("Transfer Timeout", "GUI tracking stopped. OS operation may still be running. Verify SD card manually.")
        setattr(self, watchdog_attr, None)

    def is_any_operation_active(self, exclude_generate=False, exclude_remote_generate=False, exclude_spoof_loop=False, exclude_transfer_sim=False, exclude_transfer_custom=False, exclude_ephem_update=False, exclude_auto_blast_active=False):
        gen_active = (not exclude_generate) and isinstance(self.gps_sim_proc, subprocess.Popen) and self.gps_sim_proc.poll() is None
        remote_gen_active = (not exclude_remote_generate) and self.remote_generation_in_progress
        spoof_loop_active = (not exclude_spoof_loop) and self.running and not self.auto_blast_active_phase
        transfer_sim_active = (not exclude_transfer_sim) and self.transfer_in_progress
        transfer_custom_active = (not exclude_transfer_custom) and self.custom_transfer_in_progress
        ephem_update_active = (not exclude_ephem_update) and self.ephemeris_update_running
        auto_blast_phase = (not exclude_auto_blast_active) and self.auto_blast_active_phase
        return gen_active or remote_gen_active or spoof_loop_active or transfer_sim_active or transfer_custom_active or ephem_update_active or auto_blast_phase

    def quit_gui(self):
        self.add_to_terminal("Quit command. Initiating shutdown...")
        self.stop_spoofing()
        if hasattr(self, 'master') and self.master and self.master.winfo_exists():
            for after_id_attr in ['transfer_watchdog_id', 'custom_transfer_watchdog_id', 'transfer_progress_timer_id', 'after_manual_blast_id', 'map_update_timer_id', 'auto_blast_timer_id', '_after_id_map_resize']:
                after_id = getattr(self, after_id_attr, None)
                if after_id:
                    try: self.master.after_cancel(after_id)
                    except tk.TclError: pass
            self.master.after(200, self.master.destroy)
        elif hasattr(self, 'master') and self.master:
            try: self.master.destroy()
            except tk.TclError: pass

if __name__ == "__main__":
    try:
        essential_transfer_commands = ["df", "mkdir", "cp", "stat"]
        missing_essential = [cmd for cmd in essential_transfer_commands if shutil.which(cmd) is None]
        if missing_essential:
            error_msg = f"Critical OS commands missing: {', '.join(missing_essential)}. Please install them."
            print(error_msg)
            try:
                root_err = tk.Tk(); root_err.withdraw(); messagebox.showerror("Startup Error", error_msg); root_err.destroy()
            except Exception: pass
            exit(1)

        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        os.makedirs(EPHEMERIS_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(SIM_OUTPUT), exist_ok=True)
        os.makedirs(TEMP_DIR, exist_ok=True)
        root = tk.Tk()
        root.geometry("800x480"); root.minsize(780, 450)
        default_font = nametofont("TkDefaultFont"); default_font.configure(size=8)
        app = GPSSpooferGUI(root)
        root.protocol("WM_DELETE_WINDOW", app.quit_gui)
        root.mainloop()
    except Exception as e_main:
        print("CRITICAL ERROR during application setup or mainloop:")
        traceback.print_exc()
        try:
            error_root = tk.Tk(); error_root.withdraw(); messagebox.showerror("Application Startup Error", f"Critical error:\n\n{e_main}\n\nSee console for details."); error_root.destroy()
        except Exception as e_msgbox_main:
            print(f"Could not show Tkinter error messagebox for main application error: {e_msgbox_main}")



