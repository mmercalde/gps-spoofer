# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A GPS spoofing rig for authorized RF security research and hardware bench testing.
It drives a HackRF One (via `hackrf_transfer`) fed by `gps-sdr-sim` to synthesize and
broadcast GPS L1 C/A signals (1575.42 MHz) at a chosen location or moving route. The
target hardware is a Raspberry Pi 5 field unit; RF injection is meant to be
conducted/shielded (antenna-removed or in an RF chamber), not radiated.

## Running

There is no build system, test suite, linter, or requirements file. Everything runs
directly with `python3`. External binaries `gps-sdr-sim` and `hackrf_transfer` must
already be installed on the host.

```bash
python3 gps_spoofer_web.py     # Flask web UI, binds 0.0.0.0:5000 (mobile-first, primary UI)
python3 gps_spoofer_gui.py     # tkinter desktop GUI (needs Pillow, X display)
python3 gpsdata.py             # download/refresh GPS ephemeris from NASA CDDIS
```

Python deps (install ad hoc, no manifest): `flask`, `requests`, `geopy`, `Pillow`
(GUI only). The GO9 subproject additionally needs `python-can`, `pyserial`, and the
`can-utils` apt package.

## Architecture

**Core / UI split.** `gps_spoofer_core.py` holds all runtime logic in the
`SpooferCore` class and has **no** tkinter or Flask dependency. It exposes a
module-level singleton `core = SpooferCore()` (bottom of the file). Both
`gps_spoofer_gui.py` and `gps_spoofer_web.py` import and share that **one** instance —
they are thin view layers over the same state, so a change made in one is visible to
the other. UIs attach behavior via optional callback hooks on the instance
(`on_state_change`, `on_download_progress`, `on_transfer_done`) and read log output
from the shared thread-safe `LogBuffer` ring buffer (`core.log`).

**Signal pipeline.**
1. `gpsdata.py` fetches the daily broadcast ephemeris (RINEX `brdc`) from NASA CDDIS,
   validating NTP sync first and rejecting partial (<200 KB) files. Ephemeris is valid
   ~24 h; the sim start time is aligned to `latest_time.txt`.
2. `SpooferCore.generate()` spawns `gps-sdr-sim` as a subprocess to produce an I/Q
   sample file (`gpssim.c8`). Location modes: Static (single lat/lon), Route (Google
   Directions road-following or straight-line fallback, written as a 10 Hz LLH motion
   CSV), or user-supplied ECEF/LLH/NMEA motion files.
3. `SpooferCore._launch_hackrf()` runs `hackrf_transfer` to broadcast the I/Q file,
   optionally looping (`-R`). "Blast" is a short high-gain (47 dB) burst to force
   target reacquisition; auto-blast schedules periodic blasts.

**Local vs. remote generation.** Generation can run locally or be offloaded to a
remote server (`DEFAULT_REMOTE_SERVER_URL`, submit/poll/download job API in
`_remote_gen_thread`). Remote generation supports **Static mode only**.

**Runtime state lives outside the repo**, under `~/gps_spoofer/`: `config.json`
(persisted UI settings), `ephemeris/`, `sim_output/gpssim.c8`, `temp/`. Paths and the
`gps-sdr-sim` binary location (`~/gps-sdr-sim/gps-sdr-sim`) are hardcoded constants at
the top of `gps_spoofer_core.py`. Long-running work (generation, transfer, remote
polling, ephemeris update) runs in daemon threads; `SpooferCore` guards mutable flags
and uses `is_any_operation_active()` to serialize operations.

## C signal generators

Three source variants of `gps-sdr-sim` live at the repo root but are **not built by
this repo** — they are the source for the external binary the Python invokes, compiled
and deployed separately:
- `gpssim.c` — upstream baseline, OpenMP (`gcc ... -lm -fopenmp`).
- `gpssim_pi5_configurable.c` — Pi 5 variant with configurable worker thread count
  (`GPSSIM_NTHREADS` env var, capped at `PI5_NTHREADS_MAX` = 4).
- `gpssim_cuda.cu` — CUDA drop-in (dual RTX 3080 Ti target); build/usage in its header
  comment. Flags are identical to upstream `gps-sdr-sim`.

## GO9 bench-target subproject

A separate effort to use a salvaged Geotab GO9 telematics unit as a second spoof
target on the bench (activate it over OBD-II CAN, spoof its GNSS, read back its fix
over NMEA). See `docs/go9_integration_proposal.md` for the full plan, pinouts, and
open questions. Current state:
- `go9_ecu.py` — OBD-II ECU emulator, **stub only**, intentionally blocked on the
  Phase-0 CAN capture (`raise NotImplementedError`). Do not implement it blind; it must
  answer exactly the PIDs the GO9 polls, per a real `candump` capture.
- `go9_nmea.py` — working GNSS NMEA reader (parses GGA/RMC → lat/lon).
- `scripts/can_up.sh`, `scripts/sniff.sh` — SocketCAN bring-up and Phase-0 capture (to
  `docs/captures/`).
- `config/mcp2515-overlay.txt` — PiCAN2 Duo device-tree overlay (verify oscillator /
  INT pins against the board rev).

These modules follow house style: flat files at repo root beside
`gps_spoofer_core.py`, no package, each `__main__`-runnable.

## Conventions and gotchas

- **Earthdata token.** `gpsdata.py` carries a hardcoded NASA Earthdata JWT (`TOKEN`)
  used to authenticate ephemeris downloads. It expires periodically and is rotated by
  committing a new value (see the "Rotate Earthdata token" commits and
  `docs/token_rotation_automation_proposal.md`). Ephemeris download 401s once it
  lapses.
- **Google Maps API key** is stored in `~/gps_spoofer/config.json` as `Maps_api_key`;
  the sentinel `"YOUR_Maps_API_KEY_HERE"` means "unset" and disables geocoding,
  elevation, static maps, and road routing (which then falls back to straight-line).
- Route/motion duration is capped (dynamic mode max `USER_MOTION_SIZE/10` s; the Python
  clamps to 3600 s) and route distance is capped at 100 mph over the duration.
- Git history shows repeated revert-to-stable churn ("Mar13 stable" baseline). Prefer
  minimal, isolated changes; the working baseline is deliberately conservative.
