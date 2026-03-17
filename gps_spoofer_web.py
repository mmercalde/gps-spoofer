# -*- coding: utf-8 -*-
# gps_spoofer_web.py
# Flask web UI for GPS Spoofer - mobile-first, accesses gps_spoofer_core.py
# Run: python3 gps_spoofer_web.py
# Access: http://192.168.43.100:5000

import os
import threading
import time
import json
from flask import Flask, render_template_string, request, jsonify, Response

# Import shared core
from gps_spoofer_core import (
    core, load_config, save_config,
    DEFAULT_FREQ_MHZ, DEFAULT_ALTITUDE_METERS, get_local_ip
)

app = Flask(__name__)

# Download progress tracking (updated by core callback)
_download_progress = [0, 0]  # [downloaded, total]

def _on_download_progress(downloaded, total):
    _download_progress[0] = downloaded
    _download_progress[1] = total

core.on_download_progress = _on_download_progress

# ── HTML template ─────────────────────────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>GPS Simulator</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:       #0a0c0f;
    --surface:  #12161c;
    --border:   #1e2530;
    --accent:   #00ff88;
    --accent2:  #00aaff;
    --danger:   #ff3b5c;
    --warn:     #ffaa00;
    --text:     #d0d8e4;
    --muted:    #556070;
    --mono:     'Share Tech Mono', monospace;
    --sans:     'Barlow', sans-serif;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 14px;
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* ── header ── */
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 12px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  header h1 {
    font-family: var(--mono);
    font-size: 16px;
    color: var(--accent);
    letter-spacing: 2px;
    text-transform: uppercase;
  }
  .status-dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    background: var(--muted);
    transition: background 0.3s;
  }
  .status-dot.active   { background: var(--accent);  box-shadow: 0 0 8px var(--accent); }
  .status-dot.busy     { background: var(--warn);    box-shadow: 0 0 8px var(--warn); }
  .status-dot.error    { background: var(--danger);  box-shadow: 0 0 8px var(--danger); }

  /* ── main layout ── */
  main { padding: 12px; max-width: 600px; margin: 0 auto; }

  /* ── cards ── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 10px;
    overflow: hidden;
  }
  .card-header {
    padding: 10px 14px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 2px;
    text-transform: uppercase;
    border-bottom: 1px solid var(--border);
    background: rgba(255,255,255,0.02);
  }
  .card-body { padding: 12px 14px; }

  /* ── status bar ── */
  .status-bar {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 10px;
  }
  .stat {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 10px;
    flex: 1;
    min-width: 80px;
    text-align: center;
  }
  .stat-label { font-size: 9px; color: var(--muted); letter-spacing: 1px; text-transform: uppercase; }
  .stat-value { font-family: var(--mono); font-size: 13px; color: var(--accent); margin-top: 2px; }

  /* ── action buttons ── */
  .btn-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-bottom: 10px;
  }
  .btn {
    padding: 14px 8px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--surface);
    color: var(--text);
    font-family: var(--mono);
    font-size: 12px;
    letter-spacing: 1px;
    cursor: pointer;
    transition: all 0.15s;
    text-align: center;
    -webkit-appearance: none;
    user-select: none;
  }
  .btn:active { transform: scale(0.96); }
  .btn:disabled { opacity: 0.3; cursor: not-allowed; }

  .btn-gen     { border-color: var(--accent2); color: var(--accent2); }
  .btn-sim     { border-color: var(--accent);  color: var(--accent); }
  .btn-loop    { border-color: var(--accent);  color: var(--accent); }
  .btn-stream  { border-color: var(--accent2); color: var(--accent2); }
  .btn-stop    { border-color: var(--danger);  color: var(--danger); }
  .btn-eph     { border-color: var(--warn);    color: var(--warn); }
  .btn-remote  { border-color: #ff6347;        color: #ff6347; }

  .btn.active-gen    { background: var(--accent2); color: #000; border-color: var(--accent2); }
  .btn.active-sim    { background: var(--accent);  color: #000; border-color: var(--accent); }
  .btn.active-loop   { background: var(--accent);  color: #000; border-color: var(--accent); }
  .btn.active-eph    { background: var(--warn);    color: #000; border-color: var(--warn); }
  .btn.active-remote { background: #ff6347;        color: #000; border-color: #ff6347; }
  .btn.active-stop   { background: var(--danger);  color: #fff; border-color: var(--danger); }

  /* ── form controls ── */
  .form-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
  }
  .form-row label {
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 1px;
    text-transform: uppercase;
    min-width: 60px;
  }
  input[type=text], input[type=number], select {
    flex: 1;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--text);
    font-family: var(--mono);
    font-size: 13px;
    padding: 8px 10px;
    -webkit-appearance: none;
    outline: none;
  }
  input[type=text]:focus, input[type=number]:focus, select:focus {
    border-color: var(--accent2);
  }
  select option { background: var(--surface); }

  .btn-small {
    padding: 8px 12px;
    font-size: 11px;
    white-space: nowrap;
  }

  /* ── slider ── */
  .slider-row {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
  }
  .slider-row label {
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 1px;
    text-transform: uppercase;
    min-width: 60px;
  }
  input[type=range] {
    flex: 1;
    -webkit-appearance: none;
    height: 4px;
    background: var(--border);
    border-radius: 2px;
    outline: none;
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 20px; height: 20px;
    border-radius: 50%;
    background: var(--accent);
    cursor: pointer;
  }
  .slider-val {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--accent);
    min-width: 50px;
    text-align: right;
  }

  /* ── location info ── */
  .loc-info {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--accent2);
    margin-top: 4px;
    min-height: 16px;
  }

  /* ── terminal ── */
  .terminal {
    background: #060809;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px;
    height: 200px;
    overflow-y: auto;
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.6;
    color: #7a9aaa;
  }
  .terminal .log-line { border-bottom: none; }
  .terminal .log-line.new { color: var(--accent); animation: fade 2s forwards; }
  @keyframes fade { to { color: #7a9aaa; } }

  /* ── file status ── */
  .file-status {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    padding: 6px 0;
  }
  .file-status.ok { color: var(--accent); }

  /* ── progress ── */
  .progress-wrap { display: none; margin-top: 8px; }
  .progress-wrap.show { display: block; }
  .progress-bar-bg {
    background: var(--border);
    border-radius: 3px;
    height: 6px;
    overflow: hidden;
  }
  .progress-bar-fill {
    height: 100%;
    background: var(--accent2);
    border-radius: 3px;
    width: 0%;
    transition: width 0.3s;
  }
  .progress-label {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--muted);
    margin-top: 4px;
  }

  /* ── toggle ── */
  .toggle-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 0;
  }
  .toggle-label { font-size: 12px; color: var(--text); }
  .toggle {
    position: relative;
    width: 44px; height: 24px;
  }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .toggle-slider {
    position: absolute;
    cursor: pointer;
    top: 0; left: 0; right: 0; bottom: 0;
    background: var(--border);
    border-radius: 24px;
    transition: 0.3s;
  }
  .toggle-slider:before {
    content: '';
    position: absolute;
    width: 18px; height: 18px;
    left: 3px; bottom: 3px;
    background: var(--muted);
    border-radius: 50%;
    transition: 0.3s;
  }
  .toggle input:checked + .toggle-slider { background: var(--accent); }
  .toggle input:checked + .toggle-slider:before {
    transform: translateX(20px);
    background: #000;
  }
</style>
</head>
<body>

<header>
  <h1>GPS// SIMULATOR</h1>
  <div style="display:flex;align-items:center;gap:8px;">
    <span id="status-text" style="font-family:var(--mono);font-size:11px;color:var(--muted)">IDLE</span>
    <div class="status-dot" id="status-dot"></div>
  </div>
</header>

<main>

  <!-- Status Stats -->
  <div class="status-bar">
    <div class="stat">
      <div class="stat-label">Gain</div>
      <div class="stat-value" id="stat-gain">--</div>
    </div>
    <div class="stat">
      <div class="stat-label">Freq</div>
      <div class="stat-value" id="stat-freq">--</div>
    </div>
    <div class="stat">
      <div class="stat-label">Duration</div>
      <div class="stat-value" id="stat-dur">--</div>
    </div>
    <div class="stat">
      <div class="stat-label">File</div>
      <div class="stat-value" id="stat-file">--</div>
    </div>
  </div>

  <!-- Action Buttons -->
  <div class="card">
    <div class="card-header">Actions</div>
    <div class="card-body">
      <div class="btn-grid">
        <button class="btn btn-gen"    id="btn-gen"    onclick="doGen()">GEN</button>
        <button class="btn btn-remote" id="btn-remote" onclick="doRemoteGen()">REMOTE</button>
        <button class="btn btn-eph"    id="btn-eph"    onclick="doUpdateEph()">EPH</button>
        <button class="btn btn-sim"    id="btn-sim"    onclick="doSim()">SIM</button>
        <button class="btn btn-loop"   id="btn-loop"   onclick="doLoop()">LOOP</button>
        <button class="btn btn-stream" id="btn-stream" onclick="doStream()">STREAM</button>
        <button class="btn btn-stop"   id="btn-stop"   onclick="doStop()">STOP</button>
      </div>
      <div class="progress-wrap" id="progress-wrap">
        <div class="progress-bar-bg">
          <div class="progress-bar-fill" id="progress-fill"></div>
        </div>
        <div class="progress-label" id="progress-label">Downloading...</div>
      </div>
    </div>
  </div>

  <!-- Location -->
  <div class="card" id="location-card">
    <div class="card-header" style="display:flex;justify-content:space-between;align-items:center">
      <span>Location</span>
      <span id="map-coords" style="font-family:var(--mono);font-size:10px;color:var(--accent2)"></span>
    </div>

    <!-- Map overlay (shown when transmitting) -->
    <div id="map-overlay" style="display:none;position:relative;">
      <img id="map-img" src="" style="width:100%;min-height:300px;display:block;border-radius:0 0 4px 4px;object-fit:cover;background:#0a0c0f;" alt="Map">
      <div style="position:absolute;bottom:6px;right:6px;display:flex;gap:4px;">
        <button class="btn btn-small" style="padding:4px 8px;font-size:11px;opacity:0.85" onclick="mapZoomIn()">+</button>
        <button class="btn btn-small" style="padding:4px 8px;font-size:11px;opacity:0.85" onclick="mapZoomOut()">−</button>
        <select id="map-type-overlay" style="font-size:10px;padding:3px 4px;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:4px" onchange="onMapTypeChange()">
          <option value="roadmap">Road</option>
          <option value="satellite">Sat</option>
          <option value="hybrid">Hybrid</option>
          <option value="terrain">Terrain</option>
        </select>
      </div>
    </div>

    <!-- Form (shown when idle) -->
    <div id="location-form" class="card-body">
      <div class="form-row">
        <label>Mode</label>
        <select id="loc-mode" onchange="onModeChange()">
          <option value="Static (Address Lookup)">Static</option>
          <option value="Route (Start/End Address)">Route</option>
          <option value="User Motion (LLH .csv)">Motion CSV</option>
        </select>
      </div>

      <!-- Static mode -->
      <div id="mode-static">
        <div class="form-row">
          <label>Address</label>
          <input type="text" id="address" placeholder="123 Main St, City, State">
          <button class="btn btn-small btn-gen" onclick="doLookup()">LOOK</button>
        </div>
        <div class="loc-info" id="static-loc-info"></div>
      </div>

      <!-- Route mode -->
      <div id="mode-route" style="display:none">
        <div class="form-row">
          <label>Start</label>
          <input type="text" id="start-address" placeholder="Start address">
          <button class="btn btn-small btn-gen" onclick="doLookupStart()">GO</button>
        </div>
        <div class="loc-info" id="start-loc-info"></div>
        <div class="form-row" style="margin-top:8px">
          <label>End</label>
          <input type="text" id="end-address" placeholder="End address">
          <button class="btn btn-small btn-gen" onclick="doLookupEnd()">GO</button>
        </div>
        <div class="loc-info" id="end-loc-info"></div>
        <div class="toggle-row" style="margin-top:8px">
          <span class="toggle-label">Follow Roads</span>
          <label class="toggle">
            <input type="checkbox" id="use-roads-toggle" checked onchange="onUseRoadsChange(this.checked)">
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div class="form-row" style="margin-top:8px">
          <button class="btn btn-small" style="width:100%;background:#1a3a2a;color:#00ff88;border:1px solid #00ff88"
                  onclick="useRealDriveTime()" id="real-time-btn">⏱ Use Real Drive Time</button>
        </div>
        <div class="loc-info" id="route-time-info" style="color:#888;font-size:10px">Set drive time before clicking GEN</div>
      </div>

      <!-- Motion CSV mode -->
      <div id="mode-motion" style="display:none">
        <div class="form-row">
          <label>File</label>
          <input type="text" id="motion-path" placeholder="/path/to/motion.csv">
          <button class="btn btn-small btn-gen" onclick="setMotionFile()">SET</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Parameters -->
  <div class="card">
    <div class="card-header">Parameters</div>
    <div class="card-body">
      <div class="slider-row">
        <label>Gain</label>
        <input type="range" id="gain-slider" min="0" max="47" step="1" value="15"
               oninput="onGainChange(this.value)">
        <span class="slider-val" id="gain-val">15 dB</span>
      </div>
      <div class="slider-row">
        <label>Duration</label>
        <input type="range" id="dur-slider" min="10" max="3600" step="10" value="60"
               oninput="onDurChange(this.value)">
        <span class="slider-val" id="dur-val">60 s</span>
      </div>
      <div class="slider-row">
        <label>Freq</label>
        <input type="range" id="freq-slider" min="1560" max="1590" step="0.001" value="1575.420"
               oninput="onFreqChange(this.value)">
        <span class="slider-val" id="freq-val">1575.420</span>
        <button class="btn btn-small" style="padding:4px 8px;font-size:10px;margin-left:4px"
                onclick="resetFreq()">DEF</button>
      </div>
      <div class="slider-row">
        <label>Blast</label>
        <input type="range" id="blast-slider" min="1" max="10" step="1" value="3"
               oninput="onBlastChange(this.value)">
        <span class="slider-val" id="blast-val">3 s</span>
      </div>
      <div class="slider-row">
        <label>Blast Int</label>
        <input type="range" id="blast-int-slider" min="1" max="10" step="1" value="5"
               oninput="onBlastIntChange(this.value)">
        <span class="slider-val" id="blast-int-val">5 m</span>
      </div>
      <div class="slider-row">
        <label>Cores</label>
        <input type="range" id="cores-slider" min="1" max="4" step="1" value="1"
               oninput="onCoresChange(this.value)">
        <span class="slider-val" id="cores-val">1</span>
      </div>
      <div class="toggle-row">
        <span class="toggle-label">Stream Mode</span>
        <label class="toggle">
          <input type="checkbox" id="stream-mode-toggle" onchange="onStreamModeChange(this.checked)">
          <span class="toggle-slider"></span>
        </label>
      </div>
      <div class="toggle-row">
        <span class="toggle-label">Auto Blast</span>
        <label class="toggle">
          <input type="checkbox" id="auto-blast-toggle" onchange="onAutoBlastChange(this.checked)">
          <span class="toggle-slider"></span>
        </label>
      </div>
    </div>
  </div>

  <!-- File Status -->
  <div class="card">
    <div class="card-header">Output File</div>
    <div class="card-body">
      <div class="file-status" id="file-status">Checking...</div>
    </div>
  </div>

  <!-- Terminal -->
  <div class="card">
    <div class="card-header" style="display:flex;justify-content:space-between;align-items:center">
      <span>Output Log</span>
      <button class="btn btn-small" style="padding:4px 10px;font-size:10px" onclick="clearLog()">CLEAR</button>
    </div>
    <div class="card-body" style="padding:0">
      <div class="terminal" id="terminal"></div>
    </div>
  </div>

</main>

<script>
// ── state ──────────────────────────────────────────────────────────────────
let lastLogCount = 0;
let pollInterval = null;

// ── poll status every 1.5s ─────────────────────────────────────────────────
function startPolling() {
  pollInterval = setInterval(pollStatus, 1500);
  pollStatus();
}

async function pollStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    updateUI(s);
  } catch(e) {}
}

function updateUI(s) {
  // dot
  const dot = document.getElementById('status-dot');
  const txt = document.getElementById('status-text');
  dot.className = 'status-dot';
  if (s.generating || s.remote_generating || s.ephemeris_updating) {
    dot.classList.add('busy'); txt.textContent = 'BUSY';
  } else if (s.running) {
    dot.classList.add('active'); txt.textContent = s.looping ? 'LOOPING' : 'TRANSMITTING';
  } else {
    txt.textContent = 'IDLE';
  }

  // stats
  document.getElementById('stat-gain').textContent = s.gain + ' dB';
  document.getElementById('stat-freq').textContent = (s.frequency_hz / 1e6).toFixed(3);
  document.getElementById('stat-dur').textContent = s.duration + ' s';
  document.getElementById('stat-file').textContent = s.sim_file_size_mb > 0
    ? s.sim_file_size_mb.toFixed(0) + ' MB' : 'NONE';

  // file status
  const fs = document.getElementById('file-status');
  if (s.sim_file_exists && s.sim_file_size_mb > 0) {
    fs.className = 'file-status ok';
    fs.textContent = 'gpssim.c8 — ' + s.sim_file_size_mb.toFixed(1) + ' MB';
  } else {
    fs.className = 'file-status';
    fs.textContent = 'gpssim.c8 not found — run GEN first';
  }

  // buttons
  const busy = s.generating || s.remote_generating || s.ephemeris_updating ||
               s.transfer_in_progress || s.auto_blast_active;
  const anyActive = busy || s.running;

  setBtn('btn-gen',    !anyActive && s.can_generate, s.generating, 'active-gen',    s.generating ? 'GEN...' : 'GEN');
  setBtn('btn-remote', !anyActive && s.can_generate, s.remote_generating, 'active-remote', s.remote_generating ? 'REMOTE...' : 'REMOTE');
  setBtn('btn-eph',    !anyActive, s.ephemeris_updating, 'active-eph', s.ephemeris_updating ? 'EPH...' : 'EPH');
  setBtn('btn-sim',    !anyActive, s.running && !s.looping, 'active-sim', s.running && !s.looping ? 'TX...' : 'SIM');
  setBtn('btn-loop',   !anyActive, s.running && s.looping, 'active-loop', s.running && s.looping ? 'LOOP...' : 'LOOP');
  setBtn('btn-stream', !anyActive, s.is_streaming, 'active-gen', s.is_streaming ? 'STREAM...' : 'STREAM');
  setBtn('btn-stop',   anyActive || s.running, false, '', 'STOP');

  // Show map overlay when transmitting, form when idle
  if (s.running) {
    const lat = s.latitude;
    const lon = s.longitude;
    if (lat && lon && !lastMapLat) showMap(lat, lon);  // only show once on TX start
  } else if (!s.running) {
    hideMap();
  }

  // sliders sync (only update if not actively dragging)
  if (document.activeElement !== document.getElementById('gain-slider')) {
    document.getElementById('gain-slider').value = s.gain;
    document.getElementById('gain-val').textContent = s.gain + ' dB';
  }
  if (document.activeElement !== document.getElementById('dur-slider')) {
    document.getElementById('dur-slider').value = s.duration;
    document.getElementById('dur-val').textContent = s.duration + ' s';
  }
  if (document.activeElement !== document.getElementById('freq-slider')) {
    document.getElementById('freq-slider').value = (s.frequency_hz / 1e6);
    document.getElementById('freq-val').textContent = (s.frequency_hz / 1e6).toFixed(3);
  }

  // download progress
  if (s.download_progress && s.download_total > 0) {
    document.getElementById('progress-wrap').classList.add('show');
    const pct = (s.download_progress / s.download_total * 100).toFixed(1);
    document.getElementById('progress-fill').style.width = pct + '%';
    document.getElementById('progress-label').textContent =
      'Downloading: ' + (s.download_progress/1e6).toFixed(1) + ' / ' +
      (s.download_total/1e6).toFixed(1) + ' MB';
  } else {
    document.getElementById('progress-wrap').classList.remove('show');
  }

  // log
  fetchLog();
}

function setBtn(id, enabled, isActive, activeClass, label) {
  const b = document.getElementById(id);
  b.disabled = !enabled;
  b.textContent = label;
  ['active-gen','active-sim','active-loop','active-eph','active-remote','active-stop'].forEach(c => b.classList.remove(c));
  if (isActive && activeClass) b.classList.add(activeClass);
}

// ── log ─────────────────────────────────────────────────────────────────────
async function fetchLog() {
  try {
    const r = await fetch('/api/log?since=' + lastLogCount);
    const data = await r.json();
    if (data.lines && data.lines.length > 0) {
      const term = document.getElementById('terminal');
      data.lines.forEach(line => {
        const div = document.createElement('div');
        div.className = 'log-line new';
        div.textContent = line;
        term.appendChild(div);
      });
      lastLogCount = data.total;
      term.scrollTop = term.scrollHeight;
    }
  } catch(e) {}
}

function clearLog() {
  document.getElementById('terminal').innerHTML = '';
  fetch('/api/log/clear', {method:'POST'});
}

// ── location mode ───────────────────────────────────────────────────────────
function onModeChange() {
  const mode = document.getElementById('loc-mode').value;
  document.getElementById('mode-static').style.display = mode.includes('Static') ? '' : 'none';
  document.getElementById('mode-route').style.display  = mode.includes('Route')  ? '' : 'none';
  document.getElementById('mode-motion').style.display = mode.includes('Motion') ? '' : 'none';
  apiPost('/api/set_location_mode', {mode});
}

// ── API calls ────────────────────────────────────────────────────────────────
async function apiPost(url, data={}) {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)
  });
  return r.json();
}

function doGen()       { apiPost('/api/generate'); }
function doStream()    { apiPost('/api/stream'); }
function onStreamModeChange(v) { apiPost('/api/set_stream_mode', {enabled: v}); }
function doRemoteGen() { apiPost('/api/remote_generate'); }
function doSim()       { apiPost('/api/sim'); }
function doLoop()      { apiPost('/api/loop'); }
function doStop()      { apiPost('/api/stop'); }
function doUpdateEph() { apiPost('/api/update_ephemeris'); }

async function doLookup() {
  const addr = document.getElementById('address').value;
  const r = await apiPost('/api/lookup_static', {address: addr});
  if (r.ok) {
    document.getElementById('static-loc-info').textContent =
      'Lat: ' + r.lat.toFixed(4) + '  Lon: ' + r.lon.toFixed(4) +
      (r.alt !== null ? '  Alt: ' + r.alt.toFixed(1) + 'm' : '');
  } else {
    document.getElementById('static-loc-info').textContent = 'Lookup failed';
  }
}

async function doLookupStart() {
  const addr = document.getElementById('start-address').value;
  const r = await apiPost('/api/lookup_start', {address: addr});
  if (r.ok) {
    document.getElementById('start-loc-info').textContent =
      'Lat: ' + r.lat.toFixed(4) + '  Lon: ' + r.lon.toFixed(4) +
      (r.alt !== null ? '  Alt: ' + r.alt.toFixed(1) + 'm' : '');
  } else {
    document.getElementById('start-loc-info').textContent = 'Lookup failed';
  }
}

async function doLookupEnd() {
  const addr = document.getElementById('end-address').value;
  const r = await apiPost('/api/lookup_end', {address: addr});
  if (r.ok) {
    document.getElementById('end-loc-info').textContent =
      'Lat: ' + r.lat.toFixed(4) + '  Lon: ' + r.lon.toFixed(4) +
      (r.alt !== null ? '  Alt: ' + r.alt.toFixed(1) + 'm' : '');
  } else {
    document.getElementById('end-loc-info').textContent = 'Lookup failed';
  }
}

async function setMotionFile() {
  const path = document.getElementById('motion-path').value;
  await apiPost('/api/set_motion_file', {path});
}

// ── sliders ──────────────────────────────────────────────────────────────────
let gainTimer, durTimer, freqTimer, blastTimer, blastIntTimer;

function onGainChange(v) {
  document.getElementById('gain-val').textContent = v + ' dB';
  clearTimeout(gainTimer);
  gainTimer = setTimeout(() => apiPost('/api/set_gain', {gain: parseInt(v)}), 400);
}
function onDurChange(v) {
  document.getElementById('dur-val').textContent = v + ' s';
  clearTimeout(durTimer);
  durTimer = setTimeout(() => apiPost('/api/set_duration', {duration: parseInt(v)}), 400);
}
function onFreqChange(v) {
  document.getElementById('freq-val').textContent = parseFloat(v).toFixed(3);
  clearTimeout(freqTimer);
  freqTimer = setTimeout(() => apiPost('/api/set_frequency', {freq_mhz: parseFloat(v)}), 400);
}
function onBlastChange(v) {
  document.getElementById('blast-val').textContent = v + ' s';
  clearTimeout(blastTimer);
  blastTimer = setTimeout(() => apiPost('/api/set_blast_duration', {seconds: parseInt(v)}), 400);
}
function onBlastIntChange(v) {
  document.getElementById('blast-int-val').textContent = v + ' m';
  clearTimeout(blastIntTimer);
  blastIntTimer = setTimeout(() => apiPost('/api/set_blast_interval', {minutes: parseInt(v)}), 400);
}
function onCoresChange(v) {
  document.getElementById('cores-val').textContent = v;
  apiPost('/api/set_gen_threads', {threads: parseInt(v)});
}
function onAutoBlastChange(v) {
  apiPost('/api/set_auto_blast', {enabled: v});
}

// ── map ──────────────────────────────────────────────────────────────────────
let mapZoom = 15;
let mapType = 'roadmap';
let lastMapLat = null;
let lastMapLon = null;
let mapRefreshTimer = null;

function updateMap(lat, lon) {
  if (!lat || !lon) return;
  lastMapLat = lat; lastMapLon = lon;
  const card = document.getElementById('location-card');
  const w = card ? card.offsetWidth : 600;
  const h = Math.round(w * 0.55);
  const url = `/api/map_image?lat=${lat}&lon=${lon}&zoom=${mapZoom}&w=${w}&h=${h}&type=${mapType}&t=${Date.now()}`;
  document.getElementById('map-img').src = url;
  document.getElementById('map-coords').textContent =
    lat.toFixed(4) + ', ' + lon.toFixed(4);
}

function showMap(lat, lon) {
  document.getElementById('map-overlay').style.display = 'block';
  document.getElementById('location-form').style.display = 'none';
  updateMap(lat, lon);
  // Refresh map every 3s during playback
  // No auto-refresh — map shown once on TX start, zoom buttons trigger manual refresh
}

function hideMap() {
  document.getElementById('map-overlay').style.display = 'none';
  document.getElementById('location-form').style.display = 'block';
  if (mapRefreshTimer) { clearInterval(mapRefreshTimer); mapRefreshTimer = null; }
  lastMapLat = null; lastMapLon = null;  // reset so map reloads on next TX
}

function mapZoomIn()  { mapZoom = Math.min(18, mapZoom+1); if(lastMapLat) updateMap(lastMapLat, lastMapLon); }
function mapZoomOut() { mapZoom = Math.max(1,  mapZoom-1); if(lastMapLat) updateMap(lastMapLat, lastMapLon); }
function onMapTypeChange() {
  mapType = document.getElementById('map-type-overlay').value;
  if(lastMapLat) updateMap(lastMapLat, lastMapLon);
}

// ── init ─────────────────────────────────────────────────────────────────────
async function useRealDriveTime() {
  const btn = document.getElementById('real-time-btn');
  const info = document.getElementById('route-time-info');
  btn.textContent = '⏱ Fetching...';
  btn.disabled = true;
  try {
    const r = await fetch('/api/route_duration', {method:'POST'});
    const d = await r.json();
    if (d.error) {
      info.textContent = 'Error: ' + d.error;
    } else {
      const sec = d.duration_sec;
      const mins = Math.floor(sec / 60);
      const secs = sec % 60;
      info.textContent = `Real drive time: ${mins}m ${secs}s (${sec}s) — duration set`;
      // Set duration slider
      document.getElementById('dur-slider').value = sec;
      document.getElementById('dur-val').textContent = sec + ' s';
      await fetch('/api/set_duration', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({duration: sec})});
    }
  } catch(e) {
    info.textContent = 'Request failed: ' + e;
  }
  btn.textContent = '⏱ Use Real Drive Time';
  btn.disabled = false;
}

function onUseRoadsChange(v) {
  fetch('/api/set_use_roads', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({enabled: v})});
}

function resetFreq() {
  document.getElementById('freq-slider').value = 1575.420;
  document.getElementById('freq-val').textContent = '1575.420';
  fetch('/api/set_frequency', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({freq_mhz: 1575.420})});
}

async function initFromStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    // Populate address fields from saved config
    if (s.address) document.getElementById('address').value = s.address;
    if (s.start_address) document.getElementById('start-address').value = s.start_address;
    if (s.end_address) document.getElementById('end-address').value = s.end_address;
    if (s.motion_file_path) document.getElementById('motion-path').value = s.motion_file_path;
    // Set location mode dropdown
    const modeMap = {
      'Static (Address Lookup)': 'Static (Address Lookup)',
      'Route (Start/End Address)': 'Route (Start/End Address)',
      'User Motion (LLH .csv)': 'User Motion (LLH .csv)'
    };
    const mode = s.location_mode || 'Static (Address Lookup)';
    document.getElementById('loc-mode').value = mode;
    onModeChange();
    // Show location info if lat/lon known
    if (s.latitude && s.longitude) {
      document.getElementById('static-loc-info').textContent =
        'Lat: ' + s.latitude.toFixed(4) + '  Lon: ' + s.longitude.toFixed(4) +
        (s.altitude ? '  Alt: ' + s.altitude.toFixed(1) + 'm' : '');
    }
    // Sliders
    document.getElementById('blast-slider').value = s.blast_duration_sec || 3;
    document.getElementById('blast-val').textContent = (s.blast_duration_sec || 3) + ' s';
    document.getElementById('blast-int-slider').value = s.auto_blast_interval_min || 5;
    document.getElementById('blast-int-val').textContent = (s.auto_blast_interval_min || 5) + ' m';
    document.getElementById('cores-slider').value = s.gen_threads || 1;
    document.getElementById('cores-val').textContent = s.gen_threads || 1;
    document.getElementById('auto-blast-toggle').checked = s.auto_blast_enabled || false;
    if (s.use_roads !== undefined) document.getElementById('use-roads-toggle').checked = s.use_roads;
  } catch(e) {}
}

window.onload = () => {
  initFromStatus();
  startPolling();
};
</script>
</body>
</html>
"""

# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/api/status')
def api_status():
    s = core.get_status_dict()

    # Normalize key names for the web UI
    s['gain']           = s.get('config_gain_db', 15)
    s['duration']       = s.get('duration_sec', 60)
    s['looping']        = s.get('is_looping', False)
    s['sim_file_exists'] = s.get('sim_output_exists', False)
    s['sim_file_size_mb'] = s.get('sim_file_size_bytes', 0) / 1e6
    s['ephemeris_updating'] = s.get('ephemeris_update_running', False)
    s['transfer_in_progress'] = s.get('transfer_in_progress', False) or s.get('custom_transfer_in_progress', False)

    # Compute can_generate
    mode = s.get('location_mode', '')
    if 'Static' in mode:
        s['can_generate'] = s.get('latitude') is not None and s.get('longitude') is not None
    elif 'Route' in mode:
        sl = s.get('start_latlon', [None, None])
        el = s.get('end_latlon', [None, None])
        s['can_generate'] = sl[0] is not None and el[0] is not None
    else:
        mp = s.get('motion_file_path', '')
        s['can_generate'] = bool(mp) and os.path.exists(mp)

    # Download progress placeholders
    # Add live playback position for moving map dot
    pos = core.get_playback_position()
    if pos:
        s['playback_lat']     = pos[0]
        s['playback_lon']     = pos[1]
        s['playback_alt']     = pos[2]
        s['playback_elapsed'] = pos[3]
    else:
        s['playback_lat'] = None
        s['playback_lon'] = None
    s['download_progress'] = _download_progress[0] if core.remote_generation_in_progress else 0
    s['download_total']    = _download_progress[1] if core.remote_generation_in_progress else 0
    # Reset progress when not downloading
    if not core.remote_generation_in_progress:
        _download_progress[0] = 0
        _download_progress[1] = 0

    return jsonify(s)


# Global log - monotonic counter, never resets
import collections as _collections
_log_lock = __import__('threading').Lock()
_log_total = [0]          # monotonic count of all messages ever
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
    # How many messages are in buffer vs total ever sent
    buf_start = total - len(buf)  # index of first message in buffer
    if since >= total:
        new_lines = []
    elif since <= buf_start:
        # Client is behind buffer start - send everything in buffer
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
    """Get real driving duration from Google Directions API."""
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
    ok = core.generate()
    return jsonify({'ok': ok})


@app.route('/api/remote_generate', methods=['POST'])
def api_remote_generate():
    ok = core.remote_generate()
    return jsonify({'ok': ok})


@app.route('/api/sim', methods=['POST'])
def api_sim():
    ok = core.start_sim()
    return jsonify({'ok': ok})


@app.route('/api/loop', methods=['POST'])
def api_loop():
    ok = core.start_loop()
    return jsonify({'ok': ok})


@app.route('/api/stream', methods=['POST'])
def api_stream():
    stream_mode = core.config.get('stream_mode', False)
    if stream_mode:
        ok = core.start_stream_loop()
    else:
        ok = core.start_stream()
    return jsonify({'ok': ok})
@app.route('/api/set_stream_mode', methods=['POST'])
def api_set_stream_mode():
    data = request.get_json()
    core.config['stream_mode'] = bool(data.get('enabled', False))
    from gps_spoofer_core import save_config
    save_config(core.config)
    return jsonify({'ok': True})
@app.route('/api/stop', methods=['POST'])
def api_stop():
    core.stop_all()
    return jsonify({'ok': True})


@app.route('/api/update_ephemeris', methods=['POST'])
def api_update_ephemeris():
    ok = core.update_ephemeris()
    return jsonify({'ok': ok})


@app.route('/api/map_image')
def api_map_image():
    """Proxy Google Static Maps image to avoid exposing API key in JS."""
    from gps_spoofer_core import download_static_map
    lat  = request.args.get('lat', type=float)
    lon  = request.args.get('lon', type=float)
    zoom = request.args.get('zoom', 15, type=int)
    w    = request.args.get('w', 600, type=int)
    h    = request.args.get('h', 300, type=int)
    mtype = request.args.get('type', 'roadmap')
    if lat is None or lon is None:
        return '', 404
    api_key = core.config.get('Maps_api_key')
    data = download_static_map(lat, lon, zoom, w, h, maptype=mtype, api_key=api_key)
    if not data:
        return '', 404
    return Response(data, mimetype='image/png')


@app.route('/api/lookup_static', methods=['POST'])
def api_lookup_static():
    data = request.get_json()
    result = core.lookup_static_address(data.get('address', ''))
    return jsonify(result)


@app.route('/api/lookup_start', methods=['POST'])
def api_lookup_start():
    data = request.get_json()
    result = core.lookup_start_address(data.get('address', ''))
    return jsonify(result)


@app.route('/api/lookup_end', methods=['POST'])
def api_lookup_end():
    data = request.get_json()
    result = core.lookup_end_address(data.get('address', ''))
    return jsonify(result)


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


# ── entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Load config and restore state
    core.config = load_config()

    # Fix frequency if corrupted in config
    from gps_spoofer_core import DEFAULT_FREQ_HZ_STR
    if core.config.get('frequency_hz', 0) < 1570000000 or core.config.get('frequency_hz', 0) > 1590000000:
        core.config['frequency_hz'] = int(DEFAULT_FREQ_HZ_STR)

    # Only restore lat/lon if address was previously saved
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

    print("GPS Simulator Web UI starting...")
    local_ip = get_local_ip()
    print(f"Access at: http://{local_ip}:5000")
    print("Also try:  http://raspberrypi.local:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
