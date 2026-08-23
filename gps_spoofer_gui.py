# -*- coding: utf-8 -*-
"""GPS Spoofer — Pi5 touchscreen GUI (redesigned, two-page).

Thin tkinter view over gps_spoofer_core.py (the same shared backend the web UI
uses).  Target display: Waveshare 4.3" capacitive touch, 800x480 landscape,
MIPI DSI.

Two pages, toggled from the top bar:
  PAGE 1  "MAIN"     — map, target location, gain + duration.
  PAGE 2  "SETTINGS" — actions (generate/transmit), frequency/blast/auto-blast,
                       maintenance (EPH/remote/SD), and the log.

Run:  python3 gps_spoofer_gui.py
Env:  GPS_GUI_WINDOWED=1  run windowed (dev) instead of borderless fullscreen.
"""

import base64
import io
import json
import os
import re
import shutil
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from tkinter import ttk

from gps_spoofer_core import (
    core, DEFAULT_FREQ_MHZ, DEFAULT_FREQ_HZ_STR, get_local_ip,
    download_static_map, get_road_route,
)

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    Image = ImageTk = None
    HAS_PIL = False

# ── palette (matches the web UI) ────────────────────────────────────────────
BG       = "#0b0e13"
SURFACE  = "#111722"
SURFACE2 = "#161d2b"
BORDER   = "#232c3c"
TEXT     = "#e7edf6"
MUTED    = "#8794a8"
MUTED2   = "#5b6878"
IDLE     = "#6b7a8d"
GO       = "#2ee6a0"
INFO     = "#38bdf8"
WARN     = "#fbbf24"
DANGER   = "#fb5a6d"
REMOTE   = "#c084fc"

TITLE_FONT = ("Helvetica", 13, "bold")
STATE_FONT = ("Helvetica", 12, "bold")
BTN_FONT   = ("Helvetica", 10, "bold")
LABEL_FONT = ("Helvetica", 9)
SMALL_FONT = ("Helvetica", 8)
MONO_FONT  = ("Courier", 9)
ENTRY_FONT = ("Helvetica", 11)
LOG_FONT   = ("Courier", 11)

W, H = 800, 480

GENERATE_TOKEN_URL = "https://urs.earthdata.nasa.gov/api/users/token"
DEFAULT_EDL_UID = "bajacali"


def _hackrf_present():
    """Cheap sysfs check for a HackRF One (VID 1d50 / PID 6089). No subprocess."""
    try:
        base = "/sys/bus/usb/devices"
        if not os.path.isdir(base):
            return False
        for dev in os.listdir(base):
            try:
                vid = open(os.path.join(base, dev, "idVendor")).read().strip()
                pid = open(os.path.join(base, dev, "idProduct")).read().strip()
                if vid == "1d50" and pid == "6089":
                    return True
            except (OSError, IOError):
                continue
    except Exception:
        pass
    return False


class GPSSpooferGUI:
    def __init__(self, root):
        self.root = root
        self.core = core
        self._map_photo = None
        self._last_map_key = None
        self._last_map_fetch = 0.0
        self._map_loading = False
        self._map_fetch_count = 0
        self._last_map_fail = 0.0
        self._poll_after = None
        self._clock_after = None
        self._buttons = {}
        self._mode_buttons = []
        self._roads_buttons = []
        self._current_page = 1
        self._token_cache_key = None
        self._token_cache_value = None
        self._token_busy = False

        self._configure_window(root)
        self._build_styles()
        self._build_ui()
        self._bind_core()
        self._render(self.core.get_status_dict())
        self._schedule_poll()
        self._tick_clock()

    # ── window ──────────────────────────────────────────────────────────────
    def _configure_window(self, root):
        root.title("GPS-SIM")
        root.configure(bg=BG)
        if os.environ.get("GPS_GUI_WINDOWED") == "1":
            root.geometry(f"{W}x{H}")
        else:
            root.overrideredirect(True)
            root.geometry(f"{W}x{H}+0+0")
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

    def _build_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("TLabelframe", background=BG, foreground=MUTED, bordercolor=BORDER, relief="flat")
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=SMALL_FONT)
        style.configure("TEntry", fieldbackground=SURFACE2, foreground=TEXT, insertcolor=TEXT, font=ENTRY_FONT)
        style.configure("TCheckbutton", background=BG, foreground=TEXT, font=LABEL_FONT)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _mk_frame(self, parent, **kw):
        return tk.Frame(parent, bg=BG, **kw)

    def _mk_button(self, parent, text, cmd, color, bgc=SURFACE2):
        b = tk.Button(parent, text=text, command=cmd, bg=bgc, fg=color,
                      activebackground=bgc, activeforeground=color,
                      relief="flat", bd=0, highlightthickness=1,
                      highlightbackground=BORDER, font=BTN_FONT,
                      disabledforeground=MUTED2, takefocus=0, pady=6)
        return b

    def _set_btn_active(self, key, active, color):
        b = self._buttons.get(key)
        if not b:
            return
        if active:
            b.configure(bg=color, fg="#04130c", activebackground=color)
        else:
            b.configure(bg=SURFACE2, fg=color, activebackground=SURFACE2)

    # ── UI construction ─────────────────────────────────────────────────────
    def _build_ui(self):
        self._build_topbar()
        self._build_pages()
        self._build_statusbar()

    def _build_topbar(self):
        bar = tk.Frame(self.root, bg=SURFACE, height=40)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        tk.Label(bar, text="GPS-SIM", bg=SURFACE, fg=GO, font=TITLE_FONT).pack(side="left", padx=(10, 0))
        self._state_dot = tk.Label(bar, text="●", bg=SURFACE, fg=IDLE, font=("Helvetica", 14))
        self._state_dot.pack(side="left", padx=(8, 2))
        self._state_label = tk.Label(bar, text="IDLE", bg=SURFACE, fg=TEXT, font=STATE_FONT)
        self._state_label.pack(side="left")

        self._nav_button = tk.Button(bar, text="SETTINGS", command=self._toggle_page,
                                     bg=SURFACE2, fg=INFO, activebackground=SURFACE2, activeforeground=INFO,
                                     relief="flat", bd=0, highlightthickness=1, highlightbackground=BORDER,
                                     font=BTN_FONT, takefocus=0, padx=10, pady=4)
        self._nav_button.pack(side="right", padx=(0, 8))
        self._clock_label = tk.Label(bar, text="--:--:--", bg=SURFACE, fg=MUTED, font=MONO_FONT)
        self._clock_label.pack(side="right", padx=(0, 12))
        self._hackrf_label = tk.Label(bar, text="", bg=SURFACE, font=SMALL_FONT)
        self._hackrf_label.pack(side="right", padx=(0, 10))

    def _build_pages(self):
        self._pages = tk.Frame(self.root, bg=BG)
        self._pages.grid(row=1, column=0, sticky="nsew")
        self._pages.rowconfigure(0, weight=1)
        self._pages.columnconfigure(0, weight=1)

        self._page1 = tk.Frame(self._pages, bg=BG)
        self._page1.grid(row=0, column=0, sticky="nsew")
        self._page2 = tk.Frame(self._pages, bg=BG)
        self._page2.grid(row=0, column=0, sticky="nsew")

        self._build_page_main(self._page1)
        self._build_page_settings(self._page2)
        self._show_page(1)

    def _show_page(self, n):
        if n == 1:
            self._page1.tkraise()
            self._nav_button.configure(text="SETTINGS")
        else:
            self._page2.tkraise()
            self._nav_button.configure(text="BACK")
        self._current_page = n

    def _toggle_page(self):
        self._show_page(2 if self._current_page == 1 else 1)

    # ── PAGE 1: MAIN (map + target + gain/duration) ─────────────────────────
    def _build_page_main(self, parent):
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(0, weight=1)

        left = tk.Frame(parent, bg=BG, width=300)
        left.grid(row=0, column=0, sticky="nsw", padx=(8, 4), pady=6)
        left.grid_propagate(False)

        self._build_target(left)      # location (mode + address + route/motion)
        self._build_actions(left)     # generate / transmit / loop / stop
        self._build_running(left)     # gain + duration

        right = tk.Frame(parent, bg=BG)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=6)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        self._build_map(right)        # big map (full right column)

    # ── PAGE 2: SETTINGS (actions + params + maintenance + log) ─────────────
    def _build_page_settings(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(0, weight=1)

        left = tk.Frame(parent, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=6)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        self._build_log(left)

        right = tk.Frame(parent, bg=BG)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=6)
        self._build_params(right)
        self._build_maintenance(right)
        self._build_token(right)

    # ── actions ─────────────────────────────────────────────────────────────
    def _build_actions(self, parent):
        act = ttk.Labelframe(parent, text="ACTIONS")
        act.pack(fill="x", pady=(0, 6))
        for i, (key, txt, cmd, color) in enumerate([
            ("gen", "GENERATE", self._do_generate, INFO),
            ("sim", "TRANSMIT", self._do_sim, GO),
            ("loop", "LOOP", self._do_loop, GO),
            ("stop", "STOP", self._do_stop, DANGER),
        ]):
            r, c = divmod(i, 2)
            b = self._mk_button(act, txt, cmd, color)
            b.grid(row=r, column=c, sticky="ew", padx=3, pady=3)
            act.columnconfigure(c, weight=1)
            self._buttons[key] = b

    # ── params (page 2 right) ───────────────────────────────────────────────
    def _build_params(self, parent):
        par = ttk.Labelframe(parent, text="PARAMETERS")
        par.pack(fill="x", pady=(0, 6))
        self._freq_var = tk.DoubleVar(value=core.config.get("frequency_hz", int(DEFAULT_FREQ_HZ_STR)) / 1e6)
        self._freq_val = self._make_slider(par, "Freq", self._freq_var, 1560, 1590, 0.001, self._on_freq, "{:.3f}")
        self._freq_def_btn = self._mk_button(par, "FREQ DEFAULT", self._reset_freq, MUTED)
        self._freq_def_btn.pack(fill="x", padx=6, pady=(0, 2))
        self._blast_var = tk.DoubleVar(value=core.config.get("blast_duration_sec", 3))
        self._blast_val = self._make_slider(par, "Blast", self._blast_var, 1, 10, 1, self._on_blast, "{} s")
        self._blastint_var = tk.DoubleVar(value=core.config.get("auto_blast_interval_min", 5))
        self._blastint_val = self._make_slider(par, "BlastInt", self._blastint_var, 1, 10, 1, self._on_blast_int, "{} m")
        self._auto_blast_btn = self._mk_button(par, "AUTO-BLAST OFF", self._toggle_auto_blast, MUTED)
        self._auto_blast_btn.pack(fill="x", padx=6, pady=(2, 4))

    # ── maintenance (page 2 right) ──────────────────────────────────────────
    def _build_maintenance(self, parent):
        maint = ttk.Labelframe(parent, text="MAINTENANCE")
        maint.pack(fill="x")
        for key, txt, cmd, color in [
            ("remote", "REMOTE GENERATE", self._do_remote, REMOTE),
            ("eph", "UPDATE EPHEMERIS", self._do_eph, WARN),
            ("sd", "COPY .c8 → SD", self._do_sd, MUTED),
            ("quit", "QUIT", self._do_quit, DANGER),
        ]:
            self._buttons[key] = self._mk_button(maint, txt, cmd, color)
            self._buttons[key].pack(fill="x", padx=6, pady=3)

    # ── token (Earthdata) ───────────────────────────────────────────────────
    def _build_token(self, parent):
        tok = ttk.Labelframe(parent, text="TOKEN")
        tok.pack(fill="x", pady=(6, 0))
        row = self._mk_frame(tok)
        row.pack(fill="x", padx=6, pady=6)
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        self._verify_token_btn = self._mk_button(row, "VERIFY TOKEN", self._do_verify_token, INFO)
        self._verify_token_btn.grid(row=0, column=0, sticky="ew", padx=(0, 2))
        self._renew_token_btn = self._mk_button(row, "RENEW TOKEN", self._do_renew_token, WARN)
        self._renew_token_btn.grid(row=0, column=1, sticky="ew", padx=(2, 0))

    def _gpsdata_path(self):
        try:
            import gpsdata
            if getattr(gpsdata, "__file__", None):
                return gpsdata.__file__
        except Exception:
            pass
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpsdata.py")

    def _read_gpsdata_token(self):
        try:
            with open(self._gpsdata_path()) as f:
                content = f.read()
            m = re.search(r'^TOKEN[ ]*=[ ]*"([^"]*)"', content, flags=re.M)
            return m.group(1) if m else ""
        except Exception:
            return ""

    def _decode_jwt(self, token):
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))

    def _token_status(self):
        token = self._read_gpsdata_token()
        if not token:
            return None, "tok: missing", MUTED2
        if token == self._token_cache_key and self._token_cache_value:
            return self._token_cache_value
        try:
            payload = self._decode_jwt(token)
        except Exception:
            payload = {}
        exp = payload.get("exp")
        uid = payload.get("uid") or DEFAULT_EDL_UID
        now = int(time.time())
        if not exp:
            st = (None, "tok: no exp", WARN)
        elif exp <= now:
            st = ("expired", "tok: EXPIRED", DANGER)
        else:
            days = (exp - now) / 86400.0
            st = ("expiring" if days < 7 else "valid",
                  f"tok: {days:.1f}d",
                  WARN if days < 7 else GO)
        self._token_cache_key = token
        self._token_cache_value = st
        return st

    def _update_token_label(self):
        if not hasattr(self, "_token_status_label"):
            return
        if self._token_busy:
            return
        st = self._token_status()
        self._token_status_label.configure(text=st[1], fg=st[2])

    def _do_verify_token(self):
        token = self._read_gpsdata_token()
        if not token:
            self._token_status_label.configure(text="tok: missing", fg=DANGER)
            self._append_log("TOKEN: no TOKEN line found in gpsdata.py")
            return
        try:
            payload = self._decode_jwt(token)
        except Exception as e:
            self._token_status_label.configure(text="tok: malformed", fg=DANGER)
            self._append_log(f"TOKEN: malformed ({e})")
            return
        exp = payload.get("exp")
        uid = payload.get("uid") or "?"
        now = int(time.time())
        if exp and exp <= now:
            self._token_status_label.configure(text="tok: EXPIRED", fg=DANGER)
            self._append_log(f"TOKEN: EXPIRED (uid={uid})")
        elif exp:
            days = (exp - now) / 86400.0
            dt = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d")
            self._token_status_label.configure(text=f"tok: {days:.1f}d", fg=(WARN if days < 7 else GO))
            self._append_log(f"TOKEN: VALID uid={uid} expires {dt} ({days:.1f} days)")
        else:
            self._token_status_label.configure(text="tok: no exp", fg=WARN)
            self._append_log(f"TOKEN: present uid={uid} (no exp)")

    def _do_renew_token(self):
        if self._token_busy:
            return
        username, password = self._get_edl_creds()
        if not username or not password:
            self._token_status_label.configure(text="tok: cancelled", fg=MUTED)
            return
        self._token_busy = True
        self._verify_token_btn.configure(state="disabled")
        self._renew_token_btn.configure(state="disabled")
        self._token_status_label.configure(text="tok: generating…", fg=INFO)
        self._append_log("TOKEN: generating a fresh token…")
        self._run_async(lambda: self._fetch_and_apply_token(username, password), self._on_renew_done)

    def _get_edl_creds(self):
        username = os.environ.get("EDL_USERNAME") or core.config.get("edl_username") or DEFAULT_EDL_UID
        password = os.environ.get("EDL_PASSWORD") or core.config.get("edl_password") or ""
        if not username or not password:
            creds = self._credential_dialog(username or DEFAULT_EDL_UID)
            if creds is None:
                return None, None
            username, password = creds
            core.config["edl_username"] = username
            core.config["edl_password"] = password
            from gps_spoofer_core import save_config
            save_config(core.config)
        return username, password

    def _credential_dialog(self, default_username):
        dlg = tk.Toplevel(self.root)
        dlg.title("Earthdata Login")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.attributes("-topmost", True)
        dlg.geometry("+%d+%d" % (self.root.winfo_rootx() + 140, self.root.winfo_rooty() + 80))
        result = {}
        f = self._mk_frame(dlg)
        f.pack(padx=14, pady=14)
        tk.Label(f, text="EARTHDATA LOGIN", bg=BG, fg=GO, font=BTN_FONT).grid(row=0, column=0, columnspan=2, pady=(0, 8))
        tk.Label(f, text="Username", bg=BG, fg=MUTED, font=LABEL_FONT, anchor="w").grid(row=1, column=0, sticky="w", pady=2)
        user_var = tk.StringVar(value=default_username)
        tk.Entry(f, textvariable=user_var, bg=SURFACE2, fg=TEXT, insertbackground=TEXT, width=24).grid(row=1, column=1, padx=(6, 0), pady=2)
        tk.Label(f, text="Password", bg=BG, fg=MUTED, font=LABEL_FONT, anchor="w").grid(row=2, column=0, sticky="w", pady=2)
        pass_var = tk.StringVar()
        tk.Entry(f, textvariable=pass_var, show="•", bg=SURFACE2, fg=TEXT, insertbackground=TEXT, width=24).grid(row=2, column=1, padx=(6, 0), pady=2)
        tk.Label(f, text="Saved on this device for next time.", bg=BG, fg=MUTED2, font=SMALL_FONT).grid(row=3, column=0, columnspan=2, pady=(6, 4))
        btns = self._mk_frame(f)
        btns.grid(row=4, column=0, columnspan=2)
        def _submit(_e=None):
            result["user"] = user_var.get().strip()
            result["password"] = pass_var.get()
            dlg.destroy()
        self._mk_button(btns, "OK", _submit, GO).pack(side="left", padx=2)
        self._mk_button(btns, "CANCEL", dlg.destroy, DANGER).pack(side="left", padx=2)
        dlg.bind("<Return>", _submit)
        dlg.update_idletasks()
        dlg.lift()
        dlg.focus_force()
        try:
            dlg.grab_set()
        except tk.TclError:
            pass
        self.root.wait_window(dlg)
        if not result.get("user") or not result.get("password"):
            return None
        return result["user"], result["password"]

    def _fetch_and_apply_token(self, username, password):
        import requests
        current = self._read_gpsdata_token()
        # Matches NASA's "Generate a Token" button: creates a NEW token while the
        # existing one stays valid until we overwrite gpsdata.py below.
        resp = requests.post(
            GENERATE_TOKEN_URL,
            auth=(username, password),
            headers={"Accept": "application/json"},
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(f"Earthdata token generation failed (HTTP {resp.status_code}): {resp.text.strip()[:200]}")
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError("Earthdata returned non-JSON")
        token = data.get("access_token")
        if not token:
            raise RuntimeError("No access_token in Earthdata response")
        payload = self._decode_jwt(token)
        exp = payload.get("exp")
        if not exp or exp <= int(time.time()):
            raise RuntimeError("Earthdata returned a token with no valid expiry")
        if token == current:
            raise RuntimeError("generated token is identical to the current one")
        self._patch_gpsdata(token)
        return {"exp": exp, "uid": payload.get("uid")}

    def _patch_gpsdata(self, token):
        path = self._gpsdata_path()
        with open(path) as f:
            content = f.read()
        new_content, n = re.subn(r'^TOKEN[ ]*=[ ]*"[^"]*"', f'TOKEN = "{token}"', content, count=1, flags=re.M)
        if n != 1:
            raise RuntimeError("could not find TOKEN line in gpsdata.py")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"{path}.bak_tokrot_{ts}"
        shutil.copy2(path, backup)
        with open(path, "w") as f:
            f.write(new_content)
        self._last_backup = backup

    def _on_renew_done(self, result):
        self._token_busy = False
        self._verify_token_btn.configure(state="normal")
        self._renew_token_btn.configure(state="normal")
        self._token_cache_key = None
        self._token_cache_value = None
        if isinstance(result, Exception):
            self._token_status_label.configure(text="tok: failed", fg=DANGER)
            self._append_log(f"TOKEN renew failed: {result}")
            return
        exp = result.get("exp")
        uid = result.get("uid") or "?"
        dt = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d") if exp else "?"
        days = ((exp - int(time.time())) / 86400.0) if exp else None
        self._append_log(f"TOKEN renewed: uid={uid} expires {dt}" + (f" ({days:.0f}d)" if days is not None else "") + f" (backup: {getattr(self, '_last_backup', '?')})")
        self._update_token_label()

    # ── running settings (page 1 left) ──────────────────────────────────────
    def _build_running(self, parent):
        run = ttk.Labelframe(parent, text="GAIN & DURATION")
        run.pack(fill="x")
        self._gain_var = tk.DoubleVar(value=core.config.get("gain", 15))
        self._dur_var = tk.DoubleVar(value=core.config.get("duration", 60))
        self._gain_val = self._make_slider(run, "Gain", self._gain_var, 0, 47, 1, self._on_gain, "{} dB")
        self._dur_val = self._make_slider(run, "Dur", self._dur_var, 10, 3600, 10, self._on_duration, "{} s")

    # ── target (location) ───────────────────────────────────────────────────
    def _build_target(self, parent):
        f = ttk.Labelframe(parent, text="TARGET LOCATION")
        f.pack(fill="x", pady=(0, 6))

        seg = self._mk_frame(f)
        seg.pack(fill="x", padx=6, pady=(4, 4))
        for i, mode in enumerate(["Static (Address Lookup)", "Route (Start/End Address)", "User Motion (LLH .csv)"]):
            label = ["STATIC", "ROUTE", "MOTION"][i]
            b = tk.Button(seg, text=label, command=lambda m=mode: self._set_mode(m),
                          bg=SURFACE2, fg=MUTED, relief="flat", bd=0,
                          highlightthickness=1, highlightbackground=BORDER,
                          font=BTN_FONT, takefocus=0, pady=5)
            b._mode_value = mode
            b.grid(row=0, column=i, sticky="ew", padx=1)
            seg.columnconfigure(i, weight=1)
            self._mode_buttons.append(b)

        # static
        self._static_frame = self._mk_frame(f)
        self._addr_var = tk.StringVar(value=core.config.get("address", ""))
        row = self._mk_frame(self._static_frame); row.pack(fill="x", pady=2)
        tk.Label(row, text="Addr", bg=BG, fg=MUTED, font=SMALL_FONT, width=5, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=self._addr_var).pack(side="left", fill="x", expand=True)
        self._mk_button(row, "LOOKUP", self._do_lookup, INFO).pack(side="left", padx=(4, 0))
        self._static_info = tk.Label(self._static_frame, text="Enter address, tap LOOKUP", bg=BG, fg=MUTED, font=LABEL_FONT, anchor="w")
        self._static_info.pack(fill="x", pady=(0, 4))

        # route
        self._route_frame = self._mk_frame(f)
        self._start_var = tk.StringVar(value=core.config.get("start_address", ""))
        self._end_var = tk.StringVar(value=core.config.get("end_address", ""))
        r1 = self._mk_frame(self._route_frame); r1.pack(fill="x", pady=2)
        tk.Label(r1, text="Start", bg=BG, fg=MUTED, font=SMALL_FONT, width=5, anchor="w").pack(side="left")
        ttk.Entry(r1, textvariable=self._start_var).pack(side="left", fill="x", expand=True)
        self._mk_button(r1, "GO", self._do_lookup_start, INFO).pack(side="left", padx=(4, 0))
        self._start_info = tk.Label(self._route_frame, text="", bg=BG, fg=MUTED, font=LABEL_FONT, anchor="w")
        self._start_info.pack(fill="x")
        r2 = self._mk_frame(self._route_frame); r2.pack(fill="x", pady=2)
        tk.Label(r2, text="End", bg=BG, fg=MUTED, font=SMALL_FONT, width=5, anchor="w").pack(side="left")
        ttk.Entry(r2, textvariable=self._end_var).pack(side="left", fill="x", expand=True)
        self._mk_button(r2, "GO", self._do_lookup_end, INFO).pack(side="left", padx=(4, 0))
        self._end_info = tk.Label(self._route_frame, text="", bg=BG, fg=MUTED, font=LABEL_FONT, anchor="w")
        self._end_info.pack(fill="x")
        rr = self._mk_frame(self._route_frame); rr.pack(fill="x", pady=2)
        self._roads_btn = self._mk_button(rr, "ROADS ON", self._toggle_use_roads, GO)
        self._roads_btn.pack(side="left", fill="x", expand=True)
        self._mk_button(rr, "SET DRIVE TIME", self._real_drive_time, GO).pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._route_time = tk.Label(self._route_frame, text="", bg=BG, fg=MUTED, font=LABEL_FONT, anchor="w")
        self._route_time.pack(fill="x", pady=(0, 4))
        self._roads_buttons.append(self._roads_btn)

        # motion
        self._motion_frame = self._mk_frame(f)
        self._motion_var = tk.StringVar(value=core.config.get("motion_file_path", ""))
        mrow = self._mk_frame(self._motion_frame); mrow.pack(fill="x", pady=2)
        tk.Label(mrow, text="CSV", bg=BG, fg=MUTED, font=SMALL_FONT, width=5, anchor="w").pack(side="left")
        ttk.Entry(mrow, textvariable=self._motion_var).pack(side="left", fill="x", expand=True)
        self._mk_button(mrow, "SET", self._set_motion, INFO).pack(side="left", padx=(4, 0))
        self._motion_info = tk.Label(self._motion_frame, text="", bg=BG, fg=MUTED, font=LABEL_FONT, anchor="w")
        self._motion_info.pack(fill="x", pady=(0, 4))

        self._show_mode_frame(core.config.get("location_mode", "Static (Address Lookup)"))

    def _show_mode_frame(self, mode):
        self._static_frame.pack_forget()
        self._route_frame.pack_forget()
        self._motion_frame.pack_forget()
        if "Static" in mode:
            self._static_frame.pack(fill="x", padx=6)
        elif "Route" in mode:
            self._route_frame.pack(fill="x", padx=6)
        else:
            self._motion_frame.pack(fill="x", padx=6)
        self._sync_mode_buttons(mode)

    def _sync_mode_buttons(self, mode):
        for b in self._mode_buttons:
            active = getattr(b, "_mode_value", None) == mode
            b.configure(bg=(INFO if active else SURFACE2), fg=("#04130c" if active else MUTED))

    # ── sliders ─────────────────────────────────────────────────────────────
    def _make_slider(self, parent, label, var, frm, to, res, cmd, fmt):
        row = self._mk_frame(parent)
        row.pack(fill="x", padx=6, pady=1)
        tk.Label(row, text=label, bg=BG, fg=MUTED, font=SMALL_FONT, width=8, anchor="w").pack(side="left")
        s = tk.Scale(row, from_=frm, to=to, resolution=res, orient=tk.HORIZONTAL,
                     command=cmd, showvalue=0, variable=var, bg=BG, fg=TEXT,
                     troughcolor=SURFACE2, highlightthickness=0, bd=0,
                     activebackground=GO, takefocus=0)
        s.pack(side="left", fill="x", expand=True)
        val = tk.Label(row, text=fmt.format(var.get()), bg=BG, fg=GO, font=MONO_FONT, width=10, anchor="e")
        val.pack(side="left", padx=(4, 0))
        return val

    # ── map ─────────────────────────────────────────────────────────────────
    def _build_map(self, parent):
        self.map_canvas = tk.Canvas(parent, bg="#07090d", highlightthickness=1, highlightbackground=BORDER)
        self.map_canvas.grid(row=0, column=0, sticky="nsew")
        ctr = self._mk_frame(parent)
        ctr.grid(row=0, column=0, sticky="se", padx=6, pady=6)
        self._mk_button(ctr, "+", self._zoom_in, TEXT).pack(side="left", padx=1)
        self._mk_button(ctr, "−", self._zoom_out, TEXT).pack(side="left", padx=1)
        self._map_type_var = tk.StringVar(value=core.config.get("map_type", "roadmap"))
        cb = ttk.Combobox(ctr, textvariable=self._map_type_var, values=["roadmap", "satellite", "hybrid", "terrain"],
                          state="readonly", width=9)
        cb.pack(side="left", padx=2)
        cb.bind("<<ComboboxSelected>>", self._on_map_type)

    # ── log ─────────────────────────────────────────────────────────────────
    def _build_log(self, parent):
        f = ttk.Labelframe(parent, text="OUTPUT LOG")
        f.grid(row=0, column=0, sticky="nsew")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        self._terminal = tk.Text(f, bg="#06080c", fg="#7e94a8", font=LOG_FONT, wrap="word", height=12, width=42,
                                 relief="flat", bd=0, padx=6, pady=2, state="disabled", highlightthickness=0)
        self._terminal.grid(row=0, column=0, sticky="nsew")
        clear = self._mk_button(f, "CLEAR", self._clear_log, MUTED)
        clear.grid(row=0, column=1, sticky="ne", padx=4, pady=2)

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg=SURFACE, height=22)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        self._file_status = tk.Label(bar, text="", bg=SURFACE, font=SMALL_FONT)
        self._file_status.pack(side="left", padx=8)
        self._map_tiles_label = tk.Label(bar, text="map: 0 tiles", bg=SURFACE, fg=MUTED, font=SMALL_FONT)
        self._map_tiles_label.pack(side="left", padx=20)
        self._token_status_label = tk.Label(bar, text="tok: —", bg=SURFACE, fg=MUTED, font=SMALL_FONT)
        self._token_status_label.pack(side="left", padx=20)
        self._eph_status = tk.Label(bar, text="", bg=SURFACE, font=SMALL_FONT)
        self._eph_status.pack(side="right", padx=8)

    # ── core bindings ───────────────────────────────────────────────────────
    def _bind_core(self):
        self.core.on_state_change = lambda: self._post(self._refresh)
        self.core.on_download_progress = lambda d, t: self._post(lambda: self._render_download(d, t))
        self.core.on_transfer_done = lambda ok, msg: self._post(lambda: self._render(self.core.get_status_dict()))
        self.core.log.register_callback(lambda m: self._post(lambda: self._append_log(m)))

    # ── render ──────────────────────────────────────────────────────────────
    def _refresh(self):
        self._render(self.core.get_status_dict())

    def _render(self, s):
        state, tone = self._classify(s)
        self._state_dot.configure(fg=tone)
        self._state_label.configure(text=state, fg=(TEXT if tone == IDLE else tone))
        hr = _hackrf_present()
        self._hackrf_label.configure(text="HackRF ✓" if hr else "HackRF ✗", fg=(GO if hr else DANGER))
        self._sync_mode_buttons(s.get("location_mode", ""))
        self._sync_roads_buttons()
        self._render_buttons(s)
        self._render_statusbar(s)
        self._update_token_label()
        self._maybe_refresh_map(s)

    def _classify(self, s):
        if s.get("ephemeris_update_running"):
            return "UPDATING EPH", WARN
        if s.get("generating"):
            return "GENERATING", INFO
        if s.get("remote_generating"):
            return "REMOTE GEN", REMOTE
        if s.get("running"):
            if s.get("is_looping"):
                return "LOOPING", GO
            if s.get("auto_blast_active"):
                return "AUTO-BLAST", WARN
            if s.get("is_blast_phase"):
                return "BLAST", WARN
            return "TRANSMITTING", GO
        if s.get("transfer_in_progress"):
            return "TRANSFERRING", INFO
        return "IDLE", IDLE

    def _can_generate(self, s):
        mode = s.get("location_mode", "")
        if "Static" in mode:
            return s.get("latitude") is not None and s.get("longitude") is not None
        if "Route" in mode:
            sl = s.get("start_latlon") or [None, None]
            el = s.get("end_latlon") or [None, None]
            return bool(sl[0] is not None and el[0] is not None)
        mp = s.get("motion_file_path", "")
        return bool(mp) and os.path.exists(mp)

    def _render_buttons(self, s):
        busy = s.get("generating") or s.get("remote_generating") or s.get("ephemeris_update_running") \
            or s.get("transfer_in_progress") or s.get("auto_blast_active")
        any_active = busy or s.get("running")
        file_ready = s.get("sim_output_exists") and s.get("sim_file_size_bytes", 0) > 0
        can_gen = self._can_generate(s)

        def enable(key, on, active, color, busy_text, idle_text):
            b = self._buttons.get(key)
            if not b:
                return
            b.configure(state=("normal" if on else "disabled"), text=(busy_text if active else idle_text))
            self._set_btn_active(key, active, color)

        enable("gen", not any_active and can_gen, s.get("generating"), INFO, "GEN…", "GENERATE")
        enable("remote", not any_active and can_gen, s.get("remote_generating"), REMOTE, "REMOTE…", "REMOTE GENERATE")
        enable("eph", not any_active, s.get("ephemeris_update_running"), WARN, "EPH…", "UPDATE EPHEMERIS")
        enable("sim", not any_active and file_ready, s.get("running") and not s.get("is_looping"), GO, "TX…", "TRANSMIT")
        enable("loop", not any_active and file_ready, s.get("running") and s.get("is_looping"), GO, "LOOP…", "LOOP")
        enable("stop", True, False, DANGER, "STOP", "STOP")
        if any_active:
            self._buttons["stop"].configure(bg=DANGER, fg="#fff")
        else:
            self._buttons["stop"].configure(bg=SURFACE2, fg=DANGER)
        enable("sd", not any_active, s.get("transfer_in_progress"), MUTED, "COPYING…", "COPY .c8 → SD")
        ab = self._auto_blast_btn
        ab.configure(text=("AUTO-BLAST ON" if core.config.get("auto_blast_enabled") else "AUTO-BLAST OFF"),
                     fg=(WARN if core.config.get("auto_blast_enabled") else MUTED))

    def _render_statusbar(self, s):
        if s.get("sim_output_exists") and s.get("sim_file_size_bytes", 0) > 0:
            mb = s["sim_file_size_bytes"] / 1e6
            self._file_status.configure(text=f"gpssim.c8 — {mb:.1f} MB ready", fg=GO)
        else:
            self._file_status.configure(text="gpssim.c8 — none (GENERATE first)", fg=MUTED2)

        self._map_tiles_label.configure(text=f"map: {self._map_fetch_count} tiles")

        eph = self._ephemeris_snapshot()
        if eph:
            age = eph["age_hours"]
            stale = age is not None and age > 24
            age_txt = "?" if age is None else (f"{age:.1f}h")
            self._eph_status.configure(
                text=f"RINEX {eph['basename']} · {age_txt}" + (" · STALE" if stale else ""),
                fg=(WARN if stale else GO))
        else:
            self._eph_status.configure(text="RINEX — none (EPH first)", fg=DANGER)

    def _ephemeris_snapshot(self):
        from gps_spoofer_core import LATEST_FILE_PATH, LATEST_TIME_PATH, EPHEMERIS_DIR
        info = {"basename": None, "age_hours": None}
        try:
            path = None
            if os.path.exists(LATEST_FILE_PATH):
                with open(LATEST_FILE_PATH) as f:
                    path = f.read().strip()
            if path and os.path.exists(path):
                info["basename"] = os.path.basename(path)
            dl = os.path.join(EPHEMERIS_DIR, "latest_download.txt")
            age_from = None
            if os.path.exists(dl):
                try:
                    from datetime import datetime
                    age_from = datetime.strptime(open(dl).read().strip(), "%Y-%m-%dT%H:%M:%S")
                except Exception:
                    age_from = None
            if age_from is None and path and os.path.exists(path):
                try:
                    from datetime import datetime
                    age_from = datetime.utcfromtimestamp(os.path.getmtime(path))
                except Exception:
                    age_from = None
            if age_from is not None:
                from datetime import datetime
                info["age_hours"] = round((datetime.utcnow() - age_from).total_seconds() / 3600.0, 1)
            return info if info["basename"] else None
        except Exception:
            return None

    def _render_download(self, downloaded, total):
        if total > 0:
            self._file_status.configure(text=f"Downloading {downloaded/1e6:.1f} / {total/1e6:.1f} MB", fg=REMOTE)
        else:
            self._file_status.configure(text="Remote generation…", fg=REMOTE)

    # ── map ─────────────────────────────────────────────────────────────────
    def _maybe_refresh_map(self, s):
        lat, lon = self._map_center(s)
        zoom = core.config.get("map_zoom", 14)
        mtype = core.config.get("map_type", "roadmap")
        if lat is None or lon is None:
            # During an active transmit (e.g. the initial blast phase before
            # playback starts), keep the last good tile instead of blanking to
            # "no location selected".
            if self._last_map_key is not None and not s.get("running"):
                self._clear_map()
            return
        w = self.map_canvas.winfo_width()
        h = self.map_canvas.winfo_height()
        if w < 50 or h < 50:
            return  # canvas not laid out yet; the 1 s poll will retry
        key = (round(lat, 5), round(lon, 5), zoom, mtype, w, h)
        now = time.time()
        # Back off after a failed fetch (Google quota/403): retry at most once
        # every 5 minutes rather than every poll.
        if now - self._last_map_fail < 300.0:
            return
        running = s.get("running")
        # Cost-safe: moving map is throttled to one tile per 30 s REGARDLESS
        # of how fast the playback position changes; idle fetches only on change.
        if running:
            need = (now - self._last_map_fetch > 30.0)
        else:
            need = (key != self._last_map_key)
        if not need or self._map_loading:
            return
        self._last_map_key = key
        self._last_map_fetch = now
        self._fetch_map(lat, lon, zoom, mtype, w, h)

    def _map_center(self, s):
        pos = self.core.get_playback_position()
        if s.get("running") and pos:
            return pos[0], pos[1]
        if s.get("latitude") is not None and s.get("longitude") is not None:
            return s["latitude"], s["longitude"]
        for key in ("start_latlon", "end_latlon"):
            ll = s.get(key) or [None, None]
            if ll[0] is not None:
                return ll[0], ll[1]
        mp = s.get("map_playback_latlon")
        if mp and mp[0] is not None:
            return mp[0], mp[1]
        return None, None

    def _fetch_map(self, lat, lon, zoom, mtype, w, h):
        api_key = core.config.get("Maps_api_key")
        self._map_fetch_count += 1
        self._map_loading = True

        def work():
            try:
                data = download_static_map(lat, lon, zoom=zoom, width=w, height=h, maptype=mtype, api_key=api_key)
            except Exception:
                data = None
            self._post(lambda: self._show_map(data))

        threading.Thread(target=work, daemon=True).start()

    def _show_map(self, data):
        self._map_loading = False
        if not data or not HAS_PIL:
            # Fetch failed (e.g. Google 403 / quota).  Back off before retrying
            # so we don't hammer the API (otherwise the counter ticks 1/sec).
            self._last_map_fail = time.time()
            self._clear_map()
            return
        try:
            img = Image.open(io.BytesIO(data))
            self._map_photo = ImageTk.PhotoImage(img)
            self.map_canvas.delete("all")
            self.map_canvas.create_image(0, 0, image=self._map_photo, anchor="nw")
            self._draw_playback_dot()
        except Exception:
            self._clear_map()

    def _clear_map(self):
        self._last_map_key = None
        self.map_canvas.delete("all")
        self.map_canvas.create_text(self.map_canvas.winfo_width() / 2, self.map_canvas.winfo_height() / 2,
                                    text="No location selected", fill=MUTED2, font=LABEL_FONT)

    def _draw_playback_dot(self):
        s = self.core.get_status_dict()
        if s.get("running"):
            w = self.map_canvas.winfo_width() or 460
            h = self.map_canvas.winfo_height() or 260
            self.map_canvas.create_oval(w/2 - 8, h/2 - 8, w/2 + 8, h/2 + 8, fill=DANGER, outline="#fff", width=2)

    # ── log ─────────────────────────────────────────────────────────────────
    def _append_log(self, msg):
        self._terminal.configure(state="normal")
        self._terminal.insert("end", msg + "\n")
        self._terminal.see("end")
        lines = int(self._terminal.index("end-1c").split(".")[0])
        if lines > 200:
            self._terminal.delete("1.0", f"{lines - 200}.0")
        self._terminal.configure(state="disabled")

    def _clear_log(self):
        self._terminal.configure(state="normal")
        self._terminal.delete("1.0", "end")
        self._terminal.configure(state="disabled")
        self.core.log.clear()

    # ── polling / clock ─────────────────────────────────────────────────────
    def _schedule_poll(self):
        if self._poll_after:
            self.root.after_cancel(self._poll_after)
        self._poll_after = self.root.after(1000, self._poll)

    def _poll(self):
        self._render(self.core.get_status_dict())
        self._schedule_poll()

    def _tick_clock(self):
        self._clock_label.configure(text=time.strftime("%H:%M:%S"))
        self._clock_after = self.root.after(1000, self._tick_clock)

    # ── actions ─────────────────────────────────────────────────────────────
    def _do_generate(self):
        self._run(self.core.generate)

    def _do_remote(self):
        self._run(self.core.remote_generate)

    def _do_eph(self):
        self._run(self.core.update_ephemeris)

    def _do_sim(self):
        self._run(self.core.start_sim)

    def _do_loop(self):
        self._run(self.core.start_loop)

    def _do_stop(self):
        self.core.stop_all()
        self._render(self.core.get_status_dict())

    def _do_sd(self):
        self._run(self.core.transfer_sim_to_sd)

    def _do_quit(self):
        """Stop all RF and close the app."""
        try:
            self.core.stop_all()
        except Exception:
            pass
        self.root.destroy()

    def _run(self, fn):
        try:
            fn()
        except Exception as e:
            self._append_log(f"ERROR: {e}")
        self._render(self.core.get_status_dict())

    # ── location actions ────────────────────────────────────────────────────
    def _set_mode(self, mode):
        core.config["location_mode"] = mode
        from gps_spoofer_core import save_config
        save_config(core.config)
        self._show_mode_frame(mode)
        self._render(self.core.get_status_dict())

    def _do_lookup(self):
        addr = self._addr_var.get().strip()
        self._static_info.configure(text="Looking up…", fg=MUTED)
        self._run_async(lambda: self.core.lookup_static_address(addr), self._on_lookup_done)

    def _do_lookup_start(self):
        self._start_info.configure(text="Looking up…", fg=MUTED)
        self._run_async(lambda: self.core.lookup_start_address(self._start_var.get().strip()), self._on_start_done)

    def _do_lookup_end(self):
        self._end_info.configure(text="Looking up…", fg=MUTED)
        self._run_async(lambda: self.core.lookup_end_address(self._end_var.get().strip()), self._on_end_done)

    def _on_lookup_done(self, r):
        if r and r.get("ok"):
            self._static_info.configure(text=f"✓ Found: {r['lat']:.4f}, {r['lon']:.4f}" + (f" · {r['altitude']:.1f}m" if r.get("altitude") else ""), fg=GO)
        else:
            self._static_info.configure(text="✗ Lookup failed — check address", fg=DANGER)
        self._render(self.core.get_status_dict())

    def _on_start_done(self, r):
        if r and r.get("ok"):
            self._start_info.configure(text=f"✓ Start: {r['lat']:.4f}, {r['lon']:.4f}" + (f" · {r['altitude']:.1f}m" if r.get("altitude") else ""), fg=GO)
        else:
            self._start_info.configure(text="✗ Start not found", fg=DANGER)
        self._render(self.core.get_status_dict())

    def _on_end_done(self, r):
        if r and r.get("ok"):
            self._end_info.configure(text=f"✓ End: {r['lat']:.4f}, {r['lon']:.4f}" + (f" · {r['altitude']:.1f}m" if r.get("altitude") else ""), fg=GO)
        else:
            self._end_info.configure(text="✗ End not found", fg=DANGER)
        self._render(self.core.get_status_dict())

    def _set_motion(self):
        path = self._motion_var.get().strip()
        core.config["motion_file_path"] = path
        from gps_spoofer_core import save_config
        save_config(core.config)
        self._motion_info.configure(text=("✓ File set" if os.path.exists(path) else "✗ File NOT found"), fg=(GO if os.path.exists(path) else DANGER))
        self._render(self.core.get_status_dict())

    def _toggle_use_roads(self):
        enabled = not core.config.get("use_roads", True)
        self.core.set_use_roads(enabled)
        self._sync_roads_buttons()

    def _sync_roads_buttons(self):
        enabled = core.config.get("use_roads", True)
        for b in self._roads_buttons:
            if enabled:
                b.configure(text="ROADS ON", bg=GO, fg="#04130c", activebackground=GO)
            else:
                b.configure(text="ROADS OFF", bg=SURFACE2, fg=MUTED, activebackground=SURFACE2)

    def _real_drive_time(self):
        start = self.core.start_latlon
        end = self.core.end_latlon
        if not start or not start[0] or not end or not end[0]:
            self._route_time.configure(text="Geocode start/end first", fg=DANGER)
            return
        self._route_time.configure(text="Fetching…", fg=MUTED)
        api_key = core.config.get("Maps_api_key")
        self._run_async(lambda: get_road_route(start, end, api_key, self.core.log)[1], self._on_drive_time)

    def _on_drive_time(self, duration):
        if isinstance(duration, Exception):
            self._route_time.configure(text="Route failed", fg=DANGER)
            return
        if duration is None:
            self._route_time.configure(text="No route", fg=DANGER)
            return
        self.core.update_duration(int(duration))
        self._dur_var.set(int(duration))
        self._dur_val.configure(text=f"{int(duration)} s")
        self._route_time.configure(text=f"Drive time {int(duration)}s set", fg=GO)

    # ── params ──────────────────────────────────────────────────────────────
    def _on_gain(self, v):
        try:
            g = int(float(v))
        except ValueError:
            return
        self._gain_val.configure(text=f"{g} dB")
        self._debounce("gain", lambda: self.core.update_gain(g))

    def _on_duration(self, v):
        try:
            d = int(float(v))
        except ValueError:
            return
        self._dur_val.configure(text=f"{d} s")
        self._debounce("dur", lambda: self.core.update_duration(d))

    def _on_freq(self, v):
        try:
            mhz = float(v)
        except ValueError:
            return
        self._freq_val.configure(text=f"{mhz:.3f}")
        self._debounce("freq", lambda: self.core.update_frequency(int(mhz * 1e6)))

    def _on_blast(self, v):
        try:
            b = int(float(v))
        except ValueError:
            return
        self._blast_val.configure(text=f"{b} s")
        self._debounce("blast", lambda: self.core.update_blast_duration(b))

    def _on_blast_int(self, v):
        try:
            b = int(float(v))
        except ValueError:
            return
        self._blastint_val.configure(text=f"{b} m")
        self._debounce("bint", lambda: self.core.update_auto_blast_interval(b))

    def _reset_freq(self):
        self._freq_var.set(DEFAULT_FREQ_MHZ)
        self._freq_val.configure(text=f"{DEFAULT_FREQ_MHZ:.3f}")
        self.core.update_frequency(int(DEFAULT_FREQ_MHZ * 1e6))

    def _toggle_auto_blast(self):
        enabled = not core.config.get("auto_blast_enabled", False)
        self.core.set_auto_blast_enabled(enabled)
        self._render(self.core.get_status_dict())

    # ── map controls ────────────────────────────────────────────────────────
    def _zoom_in(self):
        self.core.update_map_zoom(min(18, core.config.get("map_zoom", 14) + 1))
        self._last_map_key = None
        self._render(self.core.get_status_dict())

    def _zoom_out(self):
        self.core.update_map_zoom(max(1, core.config.get("map_zoom", 14) - 1))
        self._last_map_key = None
        self._render(self.core.get_status_dict())

    def _on_map_type(self, event=None):
        self.core.update_map_type(self._map_type_var.get())
        self._last_map_key = None
        self._render(self.core.get_status_dict())

    # ── utils ───────────────────────────────────────────────────────────────
    def _debounce(self, key, fn, ms=500):
        after_id = getattr(self, "_db_" + key, None)
        if after_id:
            self.root.after_cancel(after_id)
        setattr(self, "_db_" + key, self.root.after(ms, fn))

    def _run_async(self, fn, on_done):
        def work():
            try:
                result = fn()
            except Exception as e:
                result = e
            self._post(lambda: on_done(result))
        threading.Thread(target=work, daemon=True).start()

    def _post(self, fn):
        """Schedule fn on the Tk main thread; no-op if the window is closing."""
        try:
            if self.root.winfo_exists():
                self.root.after(0, fn)
        except (tk.TclError, RuntimeError):
            pass


def main():
    root = tk.Tk()
    GPSSpooferGUI(root)
    root.mainloop()


if __name__ == "__main__":
    if core.config.get("frequency_hz", 0) < 1570000000 or core.config.get("frequency_hz", 0) > 1590000000:
        core.config["frequency_hz"] = int(DEFAULT_FREQ_HZ_STR)
    main()
