# -*- coding: utf-8 -*-
# gps_spoofer_web.py
# Flask web UI for GPS Spoofer — field control surface (Pi5b + HackRF).
#
# This is a DROP-IN frontend rewrite of the original gps_spoofer_web.py.
# It keeps the exact same HTTP API (routes + JSON shapes) and the exact same
# gps_spoofer_core.py backend, so the tkinter GUI is unaffected.  The only
# backend-adjacent change is a read-only web-layer enrichment of /api/status
# (the "ephemeris" block) which reads the existing RINEX cache pointers that
# gpsdata.py already writes — no core.py edits.
#
# Run: python3 gps_spoofer_web.py
# Access: http://<pi-ip>:5000

import os
import threading
import json
from datetime import datetime

from flask import Flask, render_template_string, request, jsonify, Response

from gps_spoofer_core import (
    core, load_config, save_config,
    DEFAULT_FREQ_MHZ, DEFAULT_ALTITUDE_METERS, get_local_ip,
    EPHEMERIS_DIR, LATEST_FILE_PATH, LATEST_TIME_PATH,
)

app = Flask(__name__)


@app.after_request
def _no_store(resp):
    """Never let the browser cache the HTML/JS shell — the field UI must always be fresh."""
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


# ---------------------------------------------------------------------------
# Remote-generation download progress (updated by core callback)
# ---------------------------------------------------------------------------
_download_progress = [0, 0]  # [downloaded, total]
_map_tiles_fetched = [0]     # Google Static Maps requests this session (cost visibility)


def _on_download_progress(downloaded, total):
    _download_progress[0] = downloaded
    _download_progress[1] = total


core.on_download_progress = _on_download_progress


# ---------------------------------------------------------------------------
# Ephemeris cache snapshot (read-only; derived from gpsdata.py's pointer files)
# ---------------------------------------------------------------------------
def _ephemeris_info():
    """Return a small dict describing the cached RINEX ephemeris, for the UI.

    Reads the same pointer files gpsdata.py writes (~/gps_spoofer/ephemeris/).
    Never raises: any failure degrades to the safe "no ephemeris" answer.
    """
    info = {
        "file_exists": False,
        "size_bytes": 0,
        "epoch": None,
        "age_hours": None,
        "basename": None,
    }
    path = None
    try:
        if os.path.exists(LATEST_FILE_PATH):
            with open(LATEST_FILE_PATH) as f:
                path = f.read().strip()
        if path and os.path.exists(path):
            info["file_exists"] = True
            info["basename"] = os.path.basename(path)
            try:
                info["size_bytes"] = os.path.getsize(path)
            except OSError:
                pass
        if os.path.exists(LATEST_TIME_PATH):
            with open(LATEST_TIME_PATH) as f:
                info["epoch"] = f.read().strip()
        age_from = None
        dl_path = os.path.join(EPHEMERIS_DIR, "latest_download.txt")
        if os.path.exists(dl_path):
            try:
                with open(dl_path) as f:
                    age_from = datetime.strptime(f.read().strip(), "%Y-%m-%dT%H:%M:%S")
            except Exception:
                age_from = None
        if age_from is None and info["file_exists"] and path:
            try:
                age_from = datetime.utcfromtimestamp(os.path.getmtime(path))
            except OSError:
                age_from = None
        if age_from is not None:
            info["age_hours"] = round((datetime.utcnow() - age_from).total_seconds() / 3600.0, 1)
    except Exception:
        pass
    return info


def _hackrf_present():
    """Cheap filesystem check for an attached HackRF One (USB VID 1d50 / PID 6089).

    No subprocess — just walks sysfs.  Returns True/False; never raises.
    """
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


# ---------------------------------------------------------------------------
# HTML template (single-file, no build step, no external assets — field safe)
# ---------------------------------------------------------------------------
HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0e13">
<title>GPS-SIM — Field Control</title>
<style>
  :root {
    --bg:         #0b0e13;
    --surface:    #111722;
    --surface-2:  #161d2b;
    --border:     #232c3c;
    --border-2:   #2e3a4e;
    --text:       #e7edf6;
    --muted:      #8794a8;
    --muted-2:    #5b6878;

    --idle:       #6b7a8d;
    --go:         #2ee6a0;
    --info:       #38bdf8;
    --warn:       #fbbf24;
    --danger:     #fb5a6d;
    --remote:     #c084fc;

    --mono: ui-monospace, "SF Mono", "JetBrains Mono", "Cascadia Code", "Roboto Mono", Menlo, Consolas, monospace;
    --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --radius: 12px;
    --shadow: 0 10px 30px rgba(0,0,0,.45);
  }

  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  html { color-scheme: dark; }
  body {
    background:
      radial-gradient(1200px 600px at 80% -10%, rgba(56,189,248,.06), transparent 60%),
      radial-gradient(1000px 500px at -10% 110%, rgba(46,230,160,.05), transparent 60%),
      var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 15px;
    line-height: 1.45;
    min-height: 100vh;
    padding-bottom: calc(24px + env(safe-area-inset-bottom));
  }
  [hidden] { display: none !important; }
  ::selection { background: rgba(56,189,248,.3); }

  /* ── app bar ─────────────────────────────────────────────────────────── */
  .appbar {
    position: sticky; top: 0; z-index: 60;
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 10px 16px;
    background: rgba(11,14,19,.82);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
  }
  .brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .brand-mark {
    width: 12px; height: 12px; border-radius: 3px;
    background: linear-gradient(135deg, var(--go), var(--info));
    box-shadow: 0 0 12px rgba(46,230,160,.6);
  }
  .brand-text { display: flex; flex-direction: column; line-height: 1.1; }
  .brand-title { font-family: var(--mono); font-weight: 700; font-size: 13px; letter-spacing: 3px; color: var(--text); }
  .brand-sub   { font-size: 10px; color: var(--muted-2); letter-spacing: .5px; }
  .appbar-right { display: flex; align-items: center; gap: 10px; }
  .clock { font-family: var(--mono); font-size: 12px; color: var(--muted); }

  .status-pill {
    display: flex; align-items: center; gap: 7px;
    padding: 5px 11px; border-radius: 999px;
    background: var(--surface); border: 1px solid var(--border);
    font-family: var(--mono); font-size: 11px; letter-spacing: 1px; color: var(--muted);
  }
  .dot {
    width: 9px; height: 9px; border-radius: 50%;
    background: var(--idle);
    flex: 0 0 auto;
  }
  .dot.tone-idle   { background: var(--idle); }
  .dot.tone-go     { background: var(--go);   box-shadow: 0 0 10px var(--go); }
  .dot.tone-info   { background: var(--info); box-shadow: 0 0 10px var(--info); }
  .dot.tone-warn   { background: var(--warn); box-shadow: 0 0 10px var(--warn); }
  .dot.tone-danger { background: var(--danger); box-shadow: 0 0 10px var(--danger); }
  .dot.tone-remote { background: var(--remote); box-shadow: 0 0 10px var(--remote); }
  .dot.pulse { animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { transform: scale(1); opacity: 1; } 50% { transform: scale(1.6); opacity: .55; } }

  /* ── offline banner ──────────────────────────────────────────────────── */
  #offline-banner {
    position: sticky; top: 52px; z-index: 55;
    background: var(--danger); color: #1a080b;
    text-align: center; padding: 6px 12px;
    font-size: 12px; font-weight: 700; letter-spacing: .5px;
  }

  /* ── layout ──────────────────────────────────────────────────────────── */
  .layout {
    display: grid; grid-template-columns: 1fr; gap: 12px;
    padding: 12px; max-width: 1120px; margin: 0 auto;
    align-items: start;
  }
  .area-target { grid-area: target; }
  .area-gen    { grid-area: gen; }
  .area-tx     { grid-area: tx; }
  .area-map    { grid-area: map; }
  .area-params { grid-area: params; }
  .area-log    { grid-area: log; }

  @media (min-width: 960px) {
    .layout {
      grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
      grid-template-areas:
        "target gen"
        "map    tx"
        "log    params";
    }
  }

  /* ── hero ────────────────────────────────────────────────────────────── */
  .hero {
    max-width: 1120px; margin: 12px auto 0; padding: 0 12px;
  }
  .hero-inner {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 16px 18px; box-shadow: var(--shadow);
    border-left: 4px solid var(--idle);
    transition: border-color .3s;
  }
  .hero-inner.tone-idle   { border-left-color: var(--idle); }
  .hero-inner.tone-go     { border-left-color: var(--go); }
  .hero-inner.tone-info   { border-left-color: var(--info); }
  .hero-inner.tone-warn   { border-left-color: var(--warn); }
  .hero-inner.tone-danger { border-left-color: var(--danger); }
  .hero-inner.tone-remote { border-left-color: var(--remote); }

  .hero-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
  .hero-state { font-family: var(--mono); font-size: 22px; font-weight: 700; letter-spacing: 2px; }
  .hero-detail { font-size: 13px; color: var(--muted); margin-top: 3px; min-height: 18px; word-break: break-word; }
  .hero-coords { font-family: var(--mono); font-size: 12px; color: var(--info); text-align: right; white-space: nowrap; }

  .hero-stats {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 14px;
  }
  .stat {
    background: var(--surface-2); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 10px; text-align: center; min-width: 0;
  }
  .stat-label { font-size: 9px; color: var(--muted-2); letter-spacing: 1.4px; text-transform: uppercase; }
  .stat-value { font-family: var(--mono); font-size: 15px; color: var(--text); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  .hero-progress { margin-top: 14px; }
  .progress-track { background: var(--border); border-radius: 4px; height: 7px; overflow: hidden; }
  .progress-fill { height: 100%; width: 0; background: var(--go); border-radius: 4px; transition: width .4s linear; }
  .progress-fill.info   { background: var(--info); }
  .progress-fill.warn   { background: var(--warn); }
  .progress-fill.remote { background: var(--remote); }
  .progress-fill.indeterminate { width: 40% !important; animation: slide 1.2s ease-in-out infinite; }
  @keyframes slide { 0% { margin-left: -40%; } 100% { margin-left: 100%; } }
  .progress-meta {
    display: flex; gap: 12px; justify-content: space-between;
    font-family: var(--mono); font-size: 11px; color: var(--muted); margin-top: 5px;
  }

  /* ── cards ───────────────────────────────────────────────────────────── */
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; }
  .card-header {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 10px 14px; border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, rgba(255,255,255,.02), transparent);
  }
  .card-title { font-family: var(--mono); font-size: 11px; letter-spacing: 2px; text-transform: uppercase; color: var(--muted); }
  .card-body { padding: 13px 14px; }

  /* ── segmented control ───────────────────────────────────────────────── */
  .seg { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 9px; padding: 3px; margin-bottom: 12px; }
  .seg-btn {
    appearance: none; -webkit-appearance: none; border: 0; background: transparent;
    color: var(--muted); font-family: var(--mono); font-size: 11px; letter-spacing: .5px;
    padding: 8px 4px; border-radius: 7px; cursor: pointer; transition: all .15s; white-space: nowrap;
  }
  .seg-btn.active { background: var(--border-2); color: var(--text); box-shadow: inset 0 0 0 1px var(--border-2); }

  /* ── fields / rows ───────────────────────────────────────────────────── */
  .mode-panel { display: flex; flex-direction: column; gap: 10px; }
  .field-row { display: flex; gap: 8px; align-items: center; }
  input[type=text] {
    flex: 1; min-width: 0; background: var(--bg); border: 1px solid var(--border);
    border-radius: 8px; color: var(--text); font-family: var(--mono); font-size: 13px;
    padding: 10px 12px; outline: none; -webkit-appearance: none;
  }
  input[type=text]:focus { border-color: var(--info); box-shadow: 0 0 0 3px rgba(56,189,248,.15); }
  input[type=text]::placeholder { color: var(--muted-2); }

  .loc-info { font-family: var(--mono); font-size: 12px; color: var(--info); min-height: 16px; word-break: break-word; }
  .loc-info.err { color: var(--danger); }
  .loc-info.ok  { color: var(--go); }

  .toggle-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 3px 0; }
  .toggle-label { font-size: 13px; color: var(--text); }
  .toggle-sub { font-size: 11px; color: var(--muted-2); margin-top: 2px; }

  /* ── switch ──────────────────────────────────────────────────────────── */
  .switch { position: relative; display: inline-block; width: 46px; height: 26px; flex: 0 0 auto; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .switch .slider {
    position: absolute; inset: 0; cursor: pointer; background: var(--border-2);
    border-radius: 26px; transition: .25s; border: 1px solid var(--border);
  }
  .switch .slider:before {
    content: ''; position: absolute; width: 20px; height: 20px; left: 2px; top: 2px;
    background: var(--muted); border-radius: 50%; transition: .25s;
  }
  .switch input:checked + .slider { background: var(--go); border-color: var(--go); }
  .switch input:checked + .slider:before { transform: translateX(20px); background: #062a1c; }
  .switch input:focus-visible + .slider { box-shadow: 0 0 0 3px rgba(46,230,160,.25); }

  /* ── buttons ─────────────────────────────────────────────────────────── */
  .btn {
    appearance: none; -webkit-appearance: none; border: 1px solid var(--border-2);
    background: var(--surface-2); color: var(--text); border-radius: 9px;
    font-family: var(--mono); font-size: 13px; letter-spacing: 1px; font-weight: 700;
    padding: 12px 14px; cursor: pointer; transition: all .15s; text-align: center;
    user-select: none; white-space: nowrap;
  }
  .btn:active { transform: translateY(1px) scale(.99); }
  .btn:disabled { opacity: .32; cursor: not-allowed; }
  .btn.active:disabled { opacity: 1; cursor: default; }
  .btn:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; }

  .btn-primary { border-color: var(--info); color: var(--info); }
  .btn-primary.active { background: var(--info); color: #02131f; }
  .btn-remote   { border-color: var(--remote); color: var(--remote); }
  .btn-remote.active { background: var(--remote); color: #170526; }
  .btn-amber    { border-color: var(--warn); color: var(--warn); }
  .btn-amber.active { background: var(--warn); color: #201500; }
  .btn-go       { border-color: var(--go); color: var(--go); }
  .btn-go.active { background: var(--go); color: #04130c; }
  .btn-danger   { border-color: var(--danger); color: var(--danger); }
  .btn-danger.active { background: var(--danger); color: #fff; animation: blink .8s step-end infinite; }
  @keyframes blink { 50% { opacity: .55; } }

  .btn-ghost { border-color: var(--border-2); color: var(--muted); font-size: 11px; padding: 10px 12px; }
  .btn-ghost:hover:not(:disabled) { color: var(--text); border-color: var(--info); }
  .btn-mini  { font-size: 10px; padding: 6px 9px; color: var(--muted); }
  .btn-block { display: block; width: 100%; }
  .btn-soft  { border-style: dashed; border-color: var(--go); color: var(--go); background: rgba(46,230,160,.06); }
  .btn-soft:disabled { opacity: .4; }

  .btn-grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .btn-grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
  .btn-stack  { display: flex; flex-direction: column; gap: 8px; }

  /* ── sliders ─────────────────────────────────────────────────────────── */
  .slider-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; }
  .slider-row > label { flex: 1; min-width: 0; font-size: 12px; color: var(--text); display: flex; flex-direction: column; }
  .hint-inline { font-size: 10px; color: var(--muted-2); font-weight: 400; letter-spacing: .2px; }
  .slider-row output { font-family: var(--mono); font-size: 13px; color: var(--go); min-width: 64px; text-align: right; }
  input[type=range] {
    -webkit-appearance: none; appearance: none; width: 34%; height: 5px; flex: 0 0 auto;
    background: linear-gradient(90deg, var(--border-2), var(--border-2)); border-radius: 3px; outline: none;
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none; width: 20px; height: 20px; border-radius: 50%;
    background: var(--go); cursor: pointer; border: 3px solid #0b0e13; box-shadow: 0 0 0 1px var(--go);
  }
  input[type=range]::-moz-range-thumb { width: 14px; height: 14px; border-radius: 50%; background: var(--go); cursor: pointer; border: 3px solid #0b0e13; }
  input[type=range]:focus-visible { box-shadow: 0 0 0 3px rgba(46,230,160,.2); border-radius: 3px; }

  .note { font-size: 11px; color: var(--muted-2); margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border); }

  /* ── status lines ────────────────────────────────────────────────────── */
  .file-status, .eph-status { font-family: var(--mono); font-size: 12px; margin-top: 10px; line-height: 1.5; }
  .file-status { color: var(--muted); }
  .file-status.ok { color: var(--go); }
  .eph-status.ok { color: var(--go); }
  .eph-status.warn { color: var(--warn); }
  .eph-status.bad { color: var(--danger); }
  .rf-status { font-family: var(--mono); font-size: 12px; margin-top: 10px; }
  .rf-status.ok { color: var(--go); }
  .rf-status.bad { color: var(--danger); }

  /* ── map ─────────────────────────────────────────────────────────────── */
  .map-wrap { position: relative; }
  #map-img { display: block; width: 100%; height: auto; min-height: 220px; background: #07090d; }
  .map-empty {
    display: flex; align-items: center; justify-content: center;
    min-height: 220px; padding: 24px; text-align: center;
    color: var(--muted-2); font-size: 13px;
  }
  .map-dot {
    position: absolute; top: 50%; left: 50%; width: 18px; height: 18px;
    margin: -9px 0 0 -9px; border-radius: 50%;
    background: var(--danger); border: 3px solid #fff; box-shadow: 0 0 0 6px rgba(251,90,109,.35);
    animation: mapPulse 1.4s ease-in-out infinite;
  }
  @keyframes mapPulse { 0%,100% { box-shadow: 0 0 0 4px rgba(251,90,109,.35); } 50% { box-shadow: 0 0 0 10px rgba(251,90,109,.12); } }
  .map-controls {
    position: absolute; right: 8px; bottom: 8px; display: flex; gap: 5px; align-items: center;
    background: rgba(11,14,19,.75); backdrop-filter: blur(6px); padding: 5px; border-radius: 9px; border: 1px solid var(--border);
  }
  .map-ctl {
    width: 30px; height: 30px; border-radius: 6px; border: 1px solid var(--border-2);
    background: var(--surface-2); color: var(--text); font-size: 16px; line-height: 1; cursor: pointer;
  }
  .map-controls select {
    background: var(--surface-2); color: var(--text); border: 1px solid var(--border-2);
    border-radius: 6px; font-size: 11px; padding: 6px 4px; outline: none;
  }
  .coords { font-family: var(--mono); font-size: 11px; color: var(--info); }

  /* ── terminal ────────────────────────────────────────────────────────── */
  .terminal {
    background: #06080c; padding: 12px; height: 260px; overflow-y: auto;
    font-family: var(--mono); font-size: 11.5px; line-height: 1.65; color: #7e94a8;
    border-top: 1px solid var(--border);
  }
  .log-line { word-break: break-word; }
  .log-line.t { color: #5b6878; }

  /* ── toast ───────────────────────────────────────────────────────────── */
  #toast {
    position: fixed; left: 50%; bottom: calc(20px + env(safe-area-inset-bottom)); transform: translateX(-50%) translateY(20px);
    background: var(--surface-2); border: 1px solid var(--border-2); border-left: 4px solid var(--info);
    color: var(--text); padding: 11px 16px; border-radius: 10px; font-size: 13px;
    max-width: min(90vw, 480px); box-shadow: var(--shadow); opacity: 0; pointer-events: none;
    transition: opacity .25s, transform .25s; z-index: 100;
  }
  #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
  #toast.err { border-left-color: var(--danger); }
  #toast.ok { border-left-color: var(--go); }

  @media (max-width: 520px) {
    .brand-sub { display: none; }
    .hero-state { font-size: 19px; }
    .stat-value { font-size: 13px; }
    input[type=range] { width: 30%; }
    .slider-row output { min-width: 54px; font-size: 12px; }
  }
  @media (prefers-reduced-motion: reduce) {
    * { animation: none !important; transition: none !important; }
  }
</style>
</head>
<body>

<div id="offline-banner" hidden>⚠ Lost connection to GPS-SIM — retrying…</div>

<header class="appbar">
  <div class="brand">
    <span class="brand-mark"></span>
    <div class="brand-text">
      <span class="brand-title">GPS-SIM</span>
      <span class="brand-sub">Field control · Pi5b + HackRF · v2.10</span>
    </div>
  </div>
  <div class="appbar-right">
    <span class="clock" id="clock">--:--:--</span>
    <div class="status-pill" id="status-pill">
      <span class="dot tone-idle" id="status-dot"></span>
      <span id="status-text">CONNECTING</span>
    </div>
  </div>
</header>

<div class="hero">
  <div class="hero-inner tone-idle" id="hero-inner">
    <div class="hero-top">
      <div>
        <div class="hero-state" id="hero-state">CONNECTING</div>
        <div class="hero-detail" id="hero-detail">Waiting for backend…</div>
      </div>
      <div class="hero-coords" id="hero-coords"></div>
    </div>
    <div class="hero-stats">
      <div class="stat"><div class="stat-label">Gain</div><div class="stat-value" id="stat-gain">—</div></div>
      <div class="stat"><div class="stat-label">Freq</div><div class="stat-value" id="stat-freq">—</div></div>
      <div class="stat"><div class="stat-label">Duration</div><div class="stat-value" id="stat-dur">—</div></div>
      <div class="stat"><div class="stat-label">Output</div><div class="stat-value" id="stat-file">—</div></div>
    </div>
    <div class="hero-progress" id="hero-progress" hidden>
      <div class="progress-track"><div class="progress-fill" id="progress-fill"></div></div>
      <div class="progress-meta">
        <span id="progress-elapsed"></span>
        <span id="progress-pct"></span>
        <span id="progress-remain"></span>
      </div>
    </div>
  </div>
</div>

<main class="layout">

  <!-- 1 · Target -->
  <section class="card area-target">
    <header class="card-header">
      <span class="card-title">1 · Target location</span>
      <button class="btn btn-mini" id="map-toggle-btn" title="Show / hide map">MAP</button>
    </header>
    <div class="card-body">
      <div class="seg" id="mode-seg" role="tablist" aria-label="Location mode">
        <button class="seg-btn" data-mode="Static (Address Lookup)" role="tab">Static</button>
        <button class="seg-btn" data-mode="Route (Start/End Address)" role="tab">Route</button>
        <button class="seg-btn" data-mode="User Motion (LLH .csv)" role="tab">Motion</button>
      </div>

      <div id="mode-static" class="mode-panel">
        <div class="field-row">
          <input type="text" id="address" placeholder="Address — 123 Main St, City" enterkeyhint="search">
          <button class="btn btn-ghost" id="lookup-static">LOOKUP</button>
        </div>
        <div class="loc-info" id="static-loc-info"></div>
      </div>

      <div id="mode-route" class="mode-panel" hidden>
        <div class="field-row">
          <input type="text" id="start-address" placeholder="Start address" enterkeyhint="search">
          <button class="btn btn-ghost" id="lookup-start">GO</button>
        </div>
        <div class="loc-info" id="start-loc-info"></div>
        <div class="field-row">
          <input type="text" id="end-address" placeholder="End address" enterkeyhint="search">
          <button class="btn btn-ghost" id="lookup-end">GO</button>
        </div>
        <div class="loc-info" id="end-loc-info"></div>
        <div class="toggle-row">
          <span class="toggle-label">Follow roads<span class="toggle-sub" style="display:block">Snap route to Google Directions</span></span>
          <label class="switch"><input type="checkbox" id="use-roads"><span class="slider"></span></label>
        </div>
        <button class="btn btn-soft btn-block" id="real-time-btn">Use real drive time</button>
        <div class="loc-info" id="route-time-info" style="color:var(--muted)">Drive time is applied before you generate.</div>
      </div>

      <div id="mode-motion" class="mode-panel" hidden>
        <div class="field-row">
          <input type="text" id="motion-path" placeholder="/path/to/motion.csv">
          <button class="btn btn-ghost" id="set-motion">SET</button>
        </div>
        <div class="loc-info" id="motion-info"></div>
      </div>
    </div>
  </section>

  <!-- 2 · Generate -->
  <section class="card area-gen">
    <header class="card-header"><span class="card-title">2 · Generate signal</span></header>
    <div class="card-body">
      <div class="btn-grid-2">
        <button class="btn btn-primary" id="btn-gen">GENERATE</button>
        <button class="btn btn-remote" id="btn-remote">REMOTE</button>
      </div>
      <button class="btn btn-amber btn-block" id="btn-eph" style="margin-top:8px">UPDATE EPHEMERIS</button>
      <div class="progress-wrap" id="remote-wrap" hidden style="margin-top:12px">
        <div class="progress-track"><div class="progress-fill remote" id="remote-fill"></div></div>
        <div class="progress-meta"><span id="remote-label">Downloading…</span></div>
      </div>
      <div class="file-status" id="file-status">Checking output file…</div>
      <div class="eph-status" id="eph-status">Checking ephemeris…</div>
      <div class="rf-status" id="rf-status">HackRF: checking…</div>
    </div>
  </section>

  <!-- 3 · Transmit -->
  <section class="card area-tx">
    <header class="card-header"><span class="card-title">3 · Transmit</span></header>
    <div class="card-body">
      <div class="btn-grid-3">
        <button class="btn btn-go" id="btn-sim">TRANSMIT</button>
        <button class="btn btn-go" id="btn-loop">LOOP</button>
        <button class="btn btn-danger" id="btn-stop">STOP</button>
      </div>
      <div class="note" id="tx-hint">Generate a signal first, then transmit it.</div>
    </div>
  </section>

  <!-- Map -->
  <section class="card area-map" id="map-card">
    <header class="card-header">
      <span class="card-title">Map</span>
      <span class="coords" id="map-coords"></span>
      <span class="coords" id="map-tiles">0 tiles</span>
    </header>
    <div class="map-wrap">
      <img id="map-img" alt="Map" hidden>
      <div class="map-empty" id="map-empty">No location selected — set a target above.</div>
      <div class="map-dot" id="map-dot" hidden></div>
      <div class="map-controls">
        <button class="map-ctl" id="zoom-in" title="Zoom in">+</button>
        <button class="map-ctl" id="zoom-out" title="Zoom out">−</button>
        <select id="map-type">
          <option value="roadmap">Road</option>
          <option value="satellite">Satellite</option>
          <option value="hybrid">Hybrid</option>
          <option value="terrain">Terrain</option>
        </select>
      </div>
    </div>
  </section>

  <!-- Parameters -->
  <section class="card area-params">
    <header class="card-header"><span class="card-title">Parameters</span></header>
    <div class="card-body">
      <div class="slider-row">
        <label>Gain <span class="hint-inline">0–47 dB · working 20–30</span></label>
        <input type="range" id="gain" min="0" max="47" step="1" value="15">
        <output id="gain-val">—</output>
      </div>
      <div class="slider-row">
        <label>Duration <span class="hint-inline">10–3600 s · useful ≥ 600 s</span></label>
        <input type="range" id="duration" min="10" max="3600" step="10" value="60">
        <output id="duration-val">—</output>
      </div>
      <div class="slider-row">
        <label>Frequency <span class="hint-inline">GPS L1</span></label>
        <input type="range" id="freq" min="1560" max="1590" step="0.001" value="1575.420">
        <output id="freq-val">—</output>
        <button class="btn btn-mini" id="freq-def" title="Reset to 1575.420 MHz">DEF</button>
      </div>
      <div class="slider-row">
        <label>Blast duration <span class="hint-inline">full-gain burst on start</span></label>
        <input type="range" id="blast" min="1" max="10" step="1" value="3">
        <output id="blast-val">—</output>
      </div>
      <div class="slider-row">
        <label>Blast interval <span class="hint-inline">minutes between auto-blasts</span></label>
        <input type="range" id="blast-int" min="1" max="10" step="1" value="5">
        <output id="blast-int-val">—</output>
      </div>
      <div class="toggle-row">
        <span class="toggle-label">Auto-blast<span class="toggle-sub" style="display:block">Periodic full-gain burst while looping</span></span>
        <label class="switch"><input type="checkbox" id="auto-blast"><span class="slider"></span></label>
      </div>
      <div class="note">Sample rate 2.6 MHz · stock single-core sim · stream mode OFF</div>
    </div>
  </section>

  <!-- Log -->
  <section class="card area-log">
    <header class="card-header">
      <span class="card-title">Output log</span>
      <button class="btn btn-mini" id="log-clear">CLEAR</button>
    </header>
    <div class="terminal" id="terminal"></div>
  </section>

</main>

<div id="toast" role="status"></div>

<script>
'use strict';
// ── helpers ────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const fmtFreq = hz => (hz / 1e6).toFixed(3);
const fmtDur  = s => { s = Math.round(s); const m = Math.floor(s/60), r = s%60; return m ? (m + 'm ' + String(r).padStart(2,'0') + 's') : (s + 's'); };
const fmtBytes = b => { if (!b || b <= 0) return '—'; return b >= 1e6 ? (b/1e6).toFixed(1) + ' MB' : Math.round(b/1e3) + ' KB'; };
const fmtCoord = (la, lo) => (la == null || lo == null) ? '' : la.toFixed(4) + ', ' + lo.toFixed(4);
const esc = s => String(s == null ? '' : s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

let toastTimer = null;
function toast(msg, kind) {
  const t = $('toast');
  t.textContent = msg;
  t.className = (kind === 'ok') ? 'show ok' : (kind === 'err' ? 'show err' : 'show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ''; }, 3200);
}

// ── state ──────────────────────────────────────────────────────────────────
let S = null;              // latest /api/status snapshot
let lastLog = 0;           // monotonic log cursor (server "total")
let online = true;
let pollTimer = null;
let txStartedAt = null;    // local fallback for elapsed time
let wasRunning = false;
let mapZoom = 14, mapType = 'roadmap', mapVisible = true, mapUserHidden = false;
let mapLat = null, mapLon = null, mapTimer = null, lastMapKey = '', lastMapFetchAt = 0;

// ── API ────────────────────────────────────────────────────────────────────
async function apiPost(url, data) {
  const r = await fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data || {}) });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}
async function act(url, data, okMsg) {
  try { await apiPost(url, data); if (okMsg) toast(okMsg, 'ok'); }
  catch (e) { toast('Action failed: ' + e.message, 'err'); }
}

function markOnline() {
  if (!online) { online = true; $('offline-banner').hidden = true; }
}
function markOffline() {
  if (online) { online = false; $('offline-banner').hidden = false; }
  $('status-text').textContent = 'OFFLINE';
  $('hero-state').textContent = 'OFFLINE';
  $('hero-detail').textContent = 'Cannot reach backend at :5000';
}

// ── polling (status ~1 Hz + incremental log) ───────────────────────────────
function startPolling() {
  if (pollTimer) return;
  pollStatus();
  pollTimer = setInterval(pollStatus, 1000);
}
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

async function pollStatus() {
  try {
    const r = await fetch('/api/status', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const s = await r.json();
    markOnline();
    S = s;
    updateUI(s);
    fetchLog();
  } catch (e) { markOffline(); }
}

async function fetchLog() {
  try {
    const r = await fetch('/api/log?since=' + lastLog, { cache: 'no-store' });
    if (!r.ok) return;
    const d = await r.json();
    if (d.lines && d.lines.length) {
      const term = $('terminal');
      const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 48;
      const frag = document.createDocumentFragment();
      d.lines.forEach(line => { const div = document.createElement('div'); div.className = 'log-line'; div.textContent = line; frag.appendChild(div); });
      term.appendChild(frag);
      while (term.children.length > 400) term.removeChild(term.firstChild);
      lastLog = d.total;
      if (atBottom) term.scrollTop = term.scrollHeight;
    }
  } catch (e) {}
}

// ── UI rendering ───────────────────────────────────────────────────────────
function updateUI(s) {
  const prevRunning = wasRunning;
  if (s.running && !prevRunning) txStartedAt = Date.now();
  if (!s.running) txStartedAt = null;
  wasRunning = s.running;

  const busy = s.generating || s.remote_generating || s.ephemeris_updating || s.transfer_in_progress || s.auto_blast_active;
  const anyActive = busy || s.running;

  // state classification
  let state, tone, detail, pulse;
  if (s.ephemeris_updating)       { state = 'UPDATING EPHEMERIS'; tone = 'warn';   pulse = true;  detail = 'Downloading RINEX from CDDIS…'; }
  else if (s.generating)          { state = 'GENERATING';          tone = 'info';   pulse = true;  detail = 'Running gps-sdr-sim locally…'; }
  else if (s.remote_generating)   { state = 'REMOTE GENERATING';   tone = 'remote'; pulse = true;  detail = 'Offloading to ' + (s.remote_server_url || 'remote server') + '…'; }
  else if (s.running) {
    if (s.looping)                { state = 'LOOPING';       tone = 'go';  pulse = true; }
    else if (s.auto_blast_active) { state = 'AUTO-BLAST';    tone = 'warn'; pulse = true; }
    else if (s.is_blast_phase)    { state = 'BLAST';         tone = 'warn'; pulse = true; }
    else                          { state = 'TRANSMITTING';  tone = 'go';  pulse = true; }
    const g = (s.running ? (s.current_gain_db != null ? s.current_gain_db : s.gain) : s.gain);
    detail = 'Gain ' + g + ' dB · ' + fmtFreq(s.frequency_hz) + ' MHz';
  }
  else if (s.transfer_in_progress) { state = 'TRANSFERRING'; tone = 'info'; pulse = true; detail = 'Copying to SD card…'; }
  else                            { state = 'IDLE'; tone = 'idle'; pulse = false; detail = idleDetail(s); }

  const hero = $('hero-inner');
  hero.className = 'hero-inner tone-' + tone;
  $('hero-state').textContent = state;
  $('hero-detail').textContent = detail;
  $('hero-state').style.color = tone === 'idle' ? 'var(--text)' : 'var(--' + tone + ')';
  const dot = $('status-dot');
  dot.className = 'dot tone-' + tone + (pulse ? ' pulse' : '');
  $('status-text').textContent = state;

  // live coordinates (playback position while moving, else target)
  let hLat = s.latitude, hLon = s.longitude;
  if (s.running && s.playback_lat != null) { hLat = s.playback_lat; hLon = s.playback_lon; }
  $('hero-coords').textContent = fmtCoord(hLat, hLon);

  // stats
  $('stat-gain').textContent = ((s.running && s.current_gain_db != null) ? s.current_gain_db : s.gain) + ' dB';
  $('stat-freq').textContent = fmtFreq(s.frequency_hz) + ' MHz';
  $('stat-dur').textContent = s.duration + ' s';
  $('stat-file').textContent = (s.sim_file_exists && s.sim_file_size_mb > 0) ? s.sim_file_size_mb.toFixed(1) + ' MB' : 'NONE';

  // generate / transmit buttons (derived state)
  const fileReady = s.sim_file_exists && s.sim_file_size_mb > 0;
  setBtn('btn-gen',    !anyActive && s.can_generate, s.generating,        'GENERATING…',  'GENERATE');
  setBtn('btn-remote', !anyActive && s.can_generate, s.remote_generating, 'REMOTE…',      'REMOTE');
  setBtn('btn-eph',    !anyActive,                   s.ephemeris_updating,'UPDATING…',    'UPDATE EPHEMERIS');
  setBtn('btn-sim',    !anyActive && fileReady,      s.running && !s.looping, 'TX…',       'TRANSMIT');
  setBtn('btn-loop',   !anyActive && fileReady,      s.running && s.looping,  'LOOPING…',  'LOOP');
  setBtn('btn-stop',   anyActive,                    anyActive,               'STOP',       'STOP');

  $('tx-hint').textContent = !fileReady
    ? 'No output file yet — generate a signal first.'
    : (anyActive ? 'Transmit active — STOP halts RF immediately.' : 'Ready to transmit ' + fmtDur(s.duration) + '.');

  // remote download progress
  if (s.remote_generating && s.download_total > 0) {
    $('remote-wrap').hidden = false;
    const pct = (s.download_progress / s.download_total * 100).toFixed(1);
    $('remote-fill').style.width = pct + '%';
    $('remote-label').textContent = 'Downloading ' + (s.download_progress/1e6).toFixed(1) + ' / ' + (s.download_total/1e6).toFixed(1) + ' MB (' + pct + '%)';
  } else if (s.remote_generating) {
    $('remote-wrap').hidden = false;
    $('remote-fill').classList.add('indeterminate');
    $('remote-label').textContent = 'Waiting on remote server…';
  } else {
    $('remote-wrap').hidden = true;
  }

  // file + ephemeris status
  const fs = $('file-status');
  if (fileReady) { fs.className = 'file-status ok'; fs.textContent = 'gpssim.c8 — ' + s.sim_file_size_mb.toFixed(1) + ' MB ready.'; }
  else { fs.className = 'file-status'; fs.textContent = 'gpssim.c8 not found — run GENERATE first.'; }
  renderEph(s.ephemeris);
  renderRf(s);
  const mt = $('map-tiles'); if (mt) mt.textContent = (s.map_tiles_fetched || 0) + ' tiles';

  // config controls (sync unless the user is actively editing that control)
  syncSlider('gain',     'gain-val',      s.gain,                     v => v + ' dB');
  syncSlider('duration', 'duration-val',  s.duration,                 v => fmtDur(v));
  syncSlider('freq',     'freq-val',      s.frequency_hz / 1e6,       v => v.toFixed(3) + ' MHz');
  syncSlider('blast',    'blast-val',     s.blast_duration_sec || 3,  v => v + ' s');
  syncSlider('blast-int','blast-int-val', s.auto_blast_interval_min || 5, v => v + ' min');
  syncToggle('auto-blast', !!s.auto_blast_enabled);
  syncToggle('use-roads',  s.use_roads !== undefined ? !!s.use_roads : true);

  // map
  updateMap(s, prevRunning);

  // transmit progress + elapsed
  updateProgress(s);
}

function idleDetail(s) {
  const mode = s.location_mode || '';
  if (mode.indexOf('Route') >= 0) {
    return s.start_address && s.end_address ? ('Route: ' + s.start_address + ' → ' + s.end_address) : 'Set route start and end.';
  }
  if (mode.indexOf('Motion') >= 0) {
    return s.motion_file_path ? ('Motion file: ' + s.motion_file_path) : 'Set a motion CSV path.';
  }
  return s.address ? ('Target: ' + s.address) : 'Pick a location to begin.';
}

function setBtn(id, enabled, active, busyLabel, idleLabel) {
  const b = $(id); if (!b) return;
  b.disabled = !enabled;
  b.classList.toggle('active', active);
  b.textContent = (active && busyLabel) ? busyLabel : idleLabel;
}

function syncSlider(id, outId, val, fmt) {
  const el = $(id); if (!el) return;
  if (document.activeElement !== el) {
    el.value = val;
    const out = $(outId); if (out) out.textContent = fmt(val);
  }
}
function syncToggle(id, checked) {
  const el = $(id); if (!el) return;
  if (document.activeElement !== el) el.checked = checked;
}

function renderEph(eph) {
  const el = $('eph-status'); if (!el) return;
  if (!eph || !eph.file_exists) {
    el.className = 'eph-status bad';
    el.textContent = '⚠ No ephemeris file — run UPDATE EPHEMERIS before first use (RINEX is date-critical).';
    return;
  }
  const age = eph.age_hours;
  let ageTxt = (age == null) ? 'age unknown' : (age < 1 ? Math.round(age*60) + ' min ago' : age.toFixed(1) + ' h ago');
  const stale = (age != null && age > 24);
  el.className = stale ? 'eph-status warn' : 'eph-status ok';
  el.textContent = 'RINEX ' + (eph.basename || '') + ' · ' + fmtBytes(eph.size_bytes)
    + (eph.epoch ? ' · epoch ' + eph.epoch : '')
    + ' · ' + ageTxt + (stale ? ' — STALE, update!' : '');
}

function renderRf(s) {
  const el = $('rf-status'); if (!el) return;
  if (s.hackrf_present) { el.className = 'rf-status ok'; el.textContent = '✓ HackRF One detected'; }
  else { el.className = 'rf-status bad'; el.textContent = '✗ HackRF not detected — connect the board.'; }
}

// ── progress / elapsed ─────────────────────────────────────────────────────
function updateProgress(s) {
  const bar = $('hero-progress'); if (!bar) return;
  const fill = $('progress-fill');
  fill.classList.remove('info', 'warn', 'remote', 'indeterminate');
  if (s.running) {
    const dur = Math.max(1, s.duration || 1);
    let elapsed = (s.playback_elapsed != null) ? s.playback_elapsed : (txStartedAt ? (Date.now() - txStartedAt) / 1000 : 0);
    let frac, loopTxt = '';
    if (s.looping) { const loop = Math.floor(elapsed / dur) + 1; frac = (elapsed % dur) / dur; loopTxt = 'LOOP ' + loop + ' · '; }
    else { frac = Math.min(1, elapsed / dur); }
    bar.hidden = false;
    fill.style.width = (frac * 100).toFixed(1) + '%';
    $('progress-elapsed').textContent = fmtDur(elapsed);
    $('progress-pct').textContent = Math.round(frac * 100) + '%';
    $('progress-remain').textContent = loopTxt + 'of ' + fmtDur(dur);
  } else if (s.generating || s.remote_generating || s.ephemeris_updating) {
    bar.hidden = false;
    fill.classList.add('indeterminate');
    if (s.ephemeris_updating) fill.classList.add('warn');
    else if (s.remote_generating) fill.classList.add('remote');
    else fill.classList.add('info');
    $('progress-elapsed').textContent = '';
    $('progress-pct').textContent = '';
    $('progress-remain').textContent = 'working…';
  } else {
    bar.hidden = true;
  }
}

// ── location mode ──────────────────────────────────────────────────────────
function setModeUI(mode) {
  $('mode-static').hidden = !(mode.indexOf('Static') >= 0);
  $('mode-route').hidden  = !(mode.indexOf('Route') >= 0);
  $('mode-motion').hidden = !(mode.indexOf('Motion') >= 0);
  document.querySelectorAll('#mode-seg .seg-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === mode);
    b.setAttribute('aria-selected', b.dataset.mode === mode ? 'true' : 'false');
  });
}
async function setMode(mode) {
  setModeUI(mode);
  try { await apiPost('/api/set_location_mode', { mode }); } catch (e) { toast('Failed to set mode', 'err'); }
}

// ── lookups ────────────────────────────────────────────────────────────────
async function doLookup(url, addr, infoId) {
  const info = $(infoId);
  info.className = 'loc-info'; info.textContent = 'Looking up…';
  try {
    const r = await apiPost(url, { address: addr });
    if (r.ok) {
      info.className = 'loc-info ok';
      info.textContent = 'Lat ' + r.lat.toFixed(4) + ' · Lon ' + r.lon.toFixed(4) + (r.altitude != null ? ' · Alt ' + r.altitude.toFixed(1) + ' m' : '');
    } else {
      info.className = 'loc-info err';
      info.textContent = 'Lookup failed: ' + (r.error || 'unknown');
    }
  } catch (e) { info.className = 'loc-info err'; info.textContent = 'Lookup failed: ' + e.message; }
}

async function useRealDriveTime() {
  const btn = $('real-time-btn'), info = $('route-time-info');
  btn.disabled = true; btn.textContent = 'Fetching drive time…';
  info.className = 'loc-info'; info.textContent = 'Contacting Google Directions…';
  try {
    const r = await fetch('/api/route_duration', { method: 'POST' });
    const d = await r.json();
    if (d.error) { info.className = 'loc-info err'; info.textContent = 'Error: ' + d.error; }
    else {
      const sec = d.duration_sec;
      await apiPost('/api/set_duration', { duration: sec });
      info.className = 'loc-info ok';
      info.textContent = 'Real drive time ' + fmtDur(sec) + ' — duration set.';
      $('duration').value = sec; $('duration-val').textContent = fmtDur(sec);
      toast('Duration set to ' + fmtDur(sec), 'ok');
    }
  } catch (e) { info.className = 'loc-info err'; info.textContent = 'Request failed: ' + e.message; }
  btn.disabled = false; btn.textContent = 'Use real drive time';
}

// ── sliders / toggles (debounced config writes) ────────────────────────────
const debounce = {};
function debouncedWrite(key, fn, ms) {
  clearTimeout(debounce[key]);
  debounce[key] = setTimeout(fn, ms || 400);
}
function onGain(v)        { $('gain-val').textContent = v + ' dB';          debouncedWrite('gain', () => act('/api/set_gain', { gain: parseInt(v,10) })); }
function onDur(v)         { $('duration-val').textContent = fmtDur(v);      debouncedWrite('dur',  () => act('/api/set_duration', { duration: parseInt(v,10) })); }
function onFreq(v)        { $('freq-val').textContent = parseFloat(v).toFixed(3) + ' MHz'; debouncedWrite('freq', () => act('/api/set_frequency', { freq_mhz: parseFloat(v) })); }
function onBlast(v)       { $('blast-val').textContent = v + ' s';          debouncedWrite('blast', () => act('/api/set_blast_duration', { seconds: parseInt(v,10) })); }
function onBlastInt(v)    { $('blast-int-val').textContent = v + ' min';    debouncedWrite('bint',  () => act('/api/set_blast_interval', { minutes: parseInt(v,10) })); }
function onAutoBlast(v)   { act('/api/set_auto_blast', { enabled: v }); }
function onUseRoads(v)    { act('/api/set_use_roads', { enabled: v }); }
function resetFreq() {
  $('freq').value = 1575.420; $('freq-val').textContent = '1575.420 MHz';
  act('/api/set_frequency', { freq_mhz: 1575.420 });
}

// ── map ────────────────────────────────────────────────────────────────────
function showMap() {
  mapVisible = true;
  $('map-card').hidden = false;
  refreshMap();
}
function hideMap() {
  mapVisible = false;
  $('map-card').hidden = true;
  setMapInterval(false);
}
function toggleMap() {
  if (mapVisible) { mapUserHidden = true; hideMap(); }
  else { mapUserHidden = false; showMap(); }
}

function updateMap(s, prevRunning) {
  // choose center: moving dot while transmitting, else the target
  if (s.running && s.playback_lat != null) { mapLat = s.playback_lat; mapLon = s.playback_lon; }
  else if (s.latitude != null && s.longitude != null) { mapLat = s.latitude; mapLon = s.longitude; }
  else if (s.start_latlon && s.start_latlon[0] != null) { mapLat = s.start_latlon[0]; mapLon = s.start_latlon[1]; }
  else if (s.end_latlon && s.end_latlon[0] != null) { mapLat = s.end_latlon[0]; mapLon = s.end_latlon[1]; }
  else if (s.map_playback_latlon && s.map_playback_latlon[0] != null) { mapLat = s.map_playback_latlon[0]; mapLon = s.map_playback_latlon[1]; }

  const hasCoords = mapLat != null && mapLon != null;

  // always re-open the map when transmitting starts
  if (s.running && !prevRunning) mapUserHidden = false;
  // auto-open whenever a location is known (unless the user hid it manually)
  if (hasCoords && !mapVisible && !mapUserHidden) { mapVisible = true; $('map-card').hidden = false; }

  // pulsing position dot only while actually transmitting
  $('map-dot').hidden = !s.running;

  // refresh cadence: live-follow during transmit, otherwise only on demand
  setMapInterval(s.running && mapVisible);

  $('map-toggle-btn').textContent = mapVisible ? 'HIDE MAP' : 'SHOW MAP';

  if (mapVisible && hasCoords) {
    $('map-coords').textContent = fmtCoord(mapLat, mapLon);
    refreshMap();
  } else if (mapVisible && !s.running) {
    $('map-coords').textContent = '';
    $('map-img').hidden = true;
    $('map-empty').hidden = false;
  }
}

function refreshMap() {
  if (!mapVisible || mapLat == null || mapLon == null) return;
  const card = $('map-card');
  const w = Math.min(Math.max(card.clientWidth - 2, 240), 640);
  const h = Math.round(w * 0.55);
  const key = mapLat.toFixed(5) + ',' + mapLon.toFixed(5) + ',' + mapZoom + ',' + mapType + ',' + w;
  if (key === lastMapKey) return;
  // Throttle the moving map to one tile per 30 s (prevents quota burn).
  const now = Date.now();
  if (S && S.running && (now - lastMapFetchAt < 30000)) return;
  lastMapFetchAt = now;
  lastMapKey = key;
  $('map-img').hidden = false;
  $('map-empty').hidden = true;
  $('map-img').src = '/api/map_image?lat=' + mapLat + '&lon=' + mapLon + '&zoom=' + mapZoom +
    '&w=' + w + '&h=' + h + '&type=' + mapType + '&t=' + Date.now();
}
function setMapInterval(on) {
  // Cost-safe moving map: re-check every 30 s, and refreshMap() de-duplicates
  // by key — a static location costs exactly ONE tile (movement costs at most
  // 1 tile / 30 s, only while actually transmitting).
  if (on && !mapTimer) mapTimer = setInterval(() => { if (mapVisible && S && S.running) refreshMap(); }, 30000);
  if (!on && mapTimer) { clearInterval(mapTimer); mapTimer = null; }
}
function mapZoomIn()  { mapZoom = Math.min(18, mapZoom + 1); refreshMap(); }
function mapZoomOut() { mapZoom = Math.max(1,  mapZoom - 1); refreshMap(); }
function onMapType()  { mapType = $('map-type').value; refreshMap(); }

// ── clock / elapsed ticker ─────────────────────────────────────────────────
function tick() {
  $('clock').textContent = new Date().toLocaleTimeString([], { hour12: false });
  if (S) updateProgress(S);
}

// ── init ───────────────────────────────────────────────────────────────────
async function initFromStatus() {
  try {
    const r = await fetch('/api/status', { cache: 'no-store' });
    const s = await r.json();
    markOnline();
    S = s;
    // populate inputs from saved config
    if (s.address) $('address').value = s.address;
    if (s.start_address) $('start-address').value = s.start_address;
    if (s.end_address) $('end-address').value = s.end_address;
    if (s.motion_file_path) $('motion-path').value = s.motion_file_path;
    if (s.map_type) { mapType = s.map_type; $('map-type').value = mapType; }
    if (s.map_zoom) mapZoom = s.map_zoom;
    if (s.start_latlon && s.start_latlon[0] != null) $('start-loc-info').textContent = 'Lat ' + s.start_latlon[0].toFixed(4) + ' · Lon ' + s.start_latlon[1].toFixed(4);
    if (s.end_latlon && s.end_latlon[0] != null) $('end-loc-info').textContent = 'Lat ' + s.end_latlon[0].toFixed(4) + ' · Lon ' + s.end_latlon[1].toFixed(4);
    if (s.latitude != null && s.longitude != null) $('static-loc-info').textContent = 'Lat ' + s.latitude.toFixed(4) + ' · Lon ' + s.longitude.toFixed(4) + (s.altitude != null ? ' · Alt ' + s.altitude.toFixed(1) + ' m' : '');
    setModeUI(s.location_mode || 'Static (Address Lookup)');
    updateUI(s);
  } catch (e) { markOffline(); }
}

// ── wire up ────────────────────────────────────────────────────────────────
$('mode-seg').addEventListener('click', e => { const b = e.target.closest('.seg-btn'); if (b) setMode(b.dataset.mode); });
$('lookup-static').addEventListener('click', () => doLookup('/api/lookup_static', $('address').value, 'static-loc-info'));
$('lookup-start').addEventListener('click', () => doLookup('/api/lookup_start', $('start-address').value, 'start-loc-info'));
$('lookup-end').addEventListener('click', () => doLookup('/api/lookup_end', $('end-address').value, 'end-loc-info'));
['address','start-address','end-address'].forEach(id => $(id).addEventListener('keydown', e => { if (e.key === 'Enter') { const m = {'address':'lookup-static','start-address':'lookup-start','end-address':'lookup-end'}[id]; $(m).click(); } }));
$('set-motion').addEventListener('click', async () => {
  const path = $('motion-path').value;
  const info = $('motion-info');
  try {
    const r = await apiPost('/api/set_motion_file', { path });
    info.className = r.ok ? 'loc-info ok' : 'loc-info err';
    info.textContent = r.ok ? 'Motion file set: ' + path : 'File not found: ' + path;
  } catch (e) { info.className = 'loc-info err'; info.textContent = 'Failed: ' + e.message; }
});
$('real-time-btn').addEventListener('click', useRealDriveTime);

$('gain').addEventListener('input', e => onGain(e.target.value));
$('duration').addEventListener('input', e => onDur(e.target.value));
$('freq').addEventListener('input', e => onFreq(e.target.value));
$('blast').addEventListener('input', e => onBlast(e.target.value));
$('blast-int').addEventListener('input', e => onBlastInt(e.target.value));
$('auto-blast').addEventListener('change', e => onAutoBlast(e.target.checked));
$('use-roads').addEventListener('change', e => onUseRoads(e.target.checked));
$('freq-def').addEventListener('click', resetFreq);

$('btn-gen').addEventListener('click', () => act('/api/generate'));
$('btn-remote').addEventListener('click', () => act('/api/remote_generate'));
$('btn-eph').addEventListener('click', () => act('/api/update_ephemeris'));
$('btn-sim').addEventListener('click', () => act('/api/sim'));
$('btn-loop').addEventListener('click', () => act('/api/loop'));
$('btn-stop').addEventListener('click', () => act('/api/stop'));

$('log-clear').addEventListener('click', () => { $('terminal').innerHTML = ''; fetch('/api/log/clear', { method: 'POST' }).catch(() => {}); });

$('map-toggle-btn').addEventListener('click', toggleMap);
$('zoom-in').addEventListener('click', mapZoomIn);
$('zoom-out').addEventListener('click', mapZoomOut);
$('map-type').addEventListener('change', onMapType);

// pause polling when the tab is hidden (battery friendly on phones)
document.addEventListener('visibilitychange', () => { document.hidden ? stopPolling() : (pollStatus(), startPolling()); });

setInterval(tick, 1000);
window.addEventListener('load', () => { tick(); initFromStatus().then(startPolling); });
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Flask routes (identical contract to the original gps_spoofer_web.py)
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/api/status')
def api_status():
    s = core.get_status_dict()

    # Normalize key names for the web UI
    s['gain']             = s.get('config_gain_db', 15)
    s['duration']         = s.get('duration_sec', 60)
    s['looping']          = s.get('is_looping', False)
    s['sim_file_exists']  = s.get('sim_output_exists', False)
    s['sim_file_size_mb'] = s.get('sim_file_size_bytes', 0) / 1e6
    s['ephemeris_updating'] = s.get('ephemeris_update_running', False)
    s['transfer_in_progress'] = s.get('transfer_in_progress', False) or s.get('custom_transfer_in_progress', False)
    s['use_roads'] = core.config.get('use_roads', True)

    # Compute can_generate
    mode = s.get('location_mode', '')
    if 'Static' in mode:
        s['can_generate'] = s.get('latitude') is not None and s.get('longitude') is not None
    elif 'Route' in mode:
        sl = s.get('start_latlon', [None, None])
        el = s.get('end_latlon', [None, None])
        s['can_generate'] = bool(sl and sl[0] is not None and el and el[0] is not None)
    else:
        mp = s.get('motion_file_path', '')
        s['can_generate'] = bool(mp) and os.path.exists(mp)

    # Live playback position for the moving-map dot
    pos = core.get_playback_position()
    if pos:
        s['playback_lat']     = pos[0]
        s['playback_lon']     = pos[1]
        s['playback_alt']     = pos[2]
        s['playback_elapsed'] = pos[3]
    else:
        s['playback_lat'] = s['playback_lon'] = s['playback_alt'] = s['playback_elapsed'] = None

    # Remote generation download progress
    s['download_progress'] = _download_progress[0] if core.remote_generation_in_progress else 0
    s['download_total']    = _download_progress[1] if core.remote_generation_in_progress else 0
    if not core.remote_generation_in_progress:
        _download_progress[0] = 0
        _download_progress[1] = 0

    # Read-only ephemeris cache snapshot (web-layer enrichment; no core edits)
    s['ephemeris'] = _ephemeris_info()
    s['hackrf_present'] = _hackrf_present()
    s['map_tiles_fetched'] = _map_tiles_fetched[0]

    return jsonify(s)


# Global log — monotonic counter, never resets
import collections as _collections
_log_lock = threading.Lock()
_log_total = [0]                         # monotonic count of all messages ever
_log_buffer = _collections.deque(maxlen=500)  # ring buffer of recent messages


def _on_log(msg):
    with _log_lock:
        _log_buffer.append(msg)
        _log_total[0] += 1


core.log.register_callback(_on_log)


@app.route('/api/log')
def api_log():
    since = int(request.args.get('since', 0))
    with _log_lock:
        total = _log_total[0]
        buf = list(_log_buffer)
    buf_start = total - len(buf)
    if since >= total:
        new_lines = []
    elif since <= buf_start:
        new_lines = buf
    else:
        new_lines = buf[since - buf_start:]
    return jsonify({'lines': new_lines, 'total': total})


@app.route('/api/log/clear', methods=['POST'])
def api_log_clear():
    core.log.clear()
    return jsonify({'ok': True})


@app.route('/api/route_duration', methods=['POST'])
def api_route_duration():
    from gps_spoofer_core import get_road_route
    api_key = core.config.get('Maps_api_key')
    start = core.start_latlon
    end   = core.end_latlon
    if not start or not start[0] or not end or not end[0]:
        return jsonify({'error': 'Start/end not geocoded'}), 400
    _, duration = get_road_route(start, end, api_key, core.log)
    if duration is None:
        return jsonify({'error': 'Could not get route duration'}), 400
    return jsonify({'duration_sec': duration})


@app.route('/api/generate', methods=['POST'])
def api_generate():
    return jsonify({'ok': core.generate()})


@app.route('/api/remote_generate', methods=['POST'])
def api_remote_generate():
    return jsonify({'ok': core.remote_generate()})


@app.route('/api/sim', methods=['POST'])
def api_sim():
    return jsonify({'ok': core.start_sim()})


@app.route('/api/loop', methods=['POST'])
def api_loop():
    return jsonify({'ok': core.start_loop()})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    core.stop_all()
    return jsonify({'ok': True})


@app.route('/api/update_ephemeris', methods=['POST'])
def api_update_ephemeris():
    return jsonify({'ok': core.update_ephemeris()})


@app.route('/api/map_image')
def api_map_image():
    from gps_spoofer_core import download_static_map
    lat   = request.args.get('lat', type=float)
    lon   = request.args.get('lon', type=float)
    zoom  = request.args.get('zoom', 15, type=int)
    w     = request.args.get('w', 600, type=int)
    h     = request.args.get('h', 300, type=int)
    mtype = request.args.get('type', 'roadmap')
    if lat is None or lon is None:
        return '', 404
    api_key = core.config.get('Maps_api_key')
    _map_tiles_fetched[0] += 1
    data = download_static_map(lat, lon, zoom, w, h, maptype=mtype, api_key=api_key)
    if not data:
        return '', 404
    return Response(data, mimetype='image/png')


@app.route('/api/lookup_static', methods=['POST'])
def api_lookup_static():
    data = request.get_json()
    return jsonify(core.lookup_static_address(data.get('address', '')))


@app.route('/api/lookup_start', methods=['POST'])
def api_lookup_start():
    data = request.get_json()
    return jsonify(core.lookup_start_address(data.get('address', '')))


@app.route('/api/lookup_end', methods=['POST'])
def api_lookup_end():
    data = request.get_json()
    return jsonify(core.lookup_end_address(data.get('address', '')))


@app.route('/api/set_location_mode', methods=['POST'])
def api_set_location_mode():
    data = request.get_json()
    mode = data.get('mode', 'Static (Address Lookup)')
    core.config['location_mode'] = mode
    save_config(core.config)
    return jsonify({'ok': True})


@app.route('/api/set_motion_file', methods=['POST'])
def api_set_motion_file():
    data = request.get_json()
    path = data.get('path', '')
    core.config['motion_file_path'] = path
    save_config(core.config)
    return jsonify({'ok': os.path.exists(path)})


@app.route('/api/set_gain', methods=['POST'])
def api_set_gain():
    data = request.get_json()
    core.update_gain(int(data.get('gain', 15)))
    return jsonify({'ok': True})


@app.route('/api/set_duration', methods=['POST'])
def api_set_duration():
    data = request.get_json()
    core.update_duration(int(data.get('duration', 60)))
    return jsonify({'ok': True})


@app.route('/api/set_frequency', methods=['POST'])
def api_set_frequency():
    data = request.get_json()
    hz = int(float(data.get('freq_mhz', DEFAULT_FREQ_MHZ)) * 1e6)
    core.update_frequency(hz)
    return jsonify({'ok': True})


@app.route('/api/set_blast_duration', methods=['POST'])
def api_set_blast_duration():
    data = request.get_json()
    core.update_blast_duration(int(data.get('seconds', 3)))
    return jsonify({'ok': True})


@app.route('/api/set_blast_interval', methods=['POST'])
def api_set_blast_interval():
    data = request.get_json()
    core.update_auto_blast_interval(int(data.get('minutes', 5)))
    return jsonify({'ok': True})


@app.route('/api/set_use_roads', methods=['POST'])
def api_set_use_roads():
    data = request.get_json()
    core.set_use_roads(bool(data.get('enabled', True)))
    return jsonify({'ok': True})


@app.route('/api/set_auto_blast', methods=['POST'])
def api_set_auto_blast():
    data = request.get_json()
    core.set_auto_blast_enabled(bool(data.get('enabled', False)))
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    core.config = load_config()

    from gps_spoofer_core import DEFAULT_FREQ_HZ_STR
    if core.config.get('frequency_hz', 0) < 1570000000 or core.config.get('frequency_hz', 0) > 1590000000:
        core.config['frequency_hz'] = int(DEFAULT_FREQ_HZ_STR)

    if core.config.get('address', '').strip():
        core.latlon   = (core.config.get('latitude'), core.config.get('longitude'))
        core.altitude = core.config.get('altitude')
    else:
        core.latlon   = (None, None)
        core.altitude = None

    if core.config.get('start_address', '').strip():
        core.start_latlon   = core.config.get('start_latlon', [None, None])
        core.start_altitude = core.config.get('start_altitude')
    else:
        core.start_latlon   = [None, None]
        core.start_altitude = None

    if core.config.get('end_address', '').strip():
        core.end_latlon   = core.config.get('end_latlon', [None, None])
        core.end_altitude = core.config.get('end_altitude')
    else:
        core.end_latlon   = [None, None]
        core.end_altitude = None

    # Port is overridable so a staging instance can run alongside the field
    # default without clashing (e.g. GPS_SPOOFER_WEB_PORT=5001).
    port = int(os.environ.get('GPS_SPOOFER_WEB_PORT', '5000'))

    print("GPS Simulator Web UI starting...")
    local_ip = get_local_ip()
    print(f"Access at: http://{local_ip}:{port}")
    print(f"Also try:  http://raspberrypi.local:{port}")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
