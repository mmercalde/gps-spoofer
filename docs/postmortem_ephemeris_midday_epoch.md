# Post-mortem: hardware receivers (Garmin) not locking — midnight ephemeris epoch

**Date:** 2026-09-11
**Component:** `gpsdata.py` (ephemeris download → `latest_time.txt` → `gps-sdr-sim -t`)
**Fix commit:** `e70b889` (this repo, `main`)
**Status:** Resolved, verified on-air, deployed to pi5b + Ser8.

## Symptom

The Garmin receiver would not lock onto the spoofed GPS signal, while the Dual
XGPS (Bluetooth, A-GPS) locked every time. Initially reported as "the spoofer is
using old ephemeris."

## Root cause

When the app builds the simulation it passes `gps-sdr-sim` a start time via `-t`,
taken from `latest_time.txt`. `download_ephemeris()` wrote the **first** RINEX
record's timestamp into that file — i.e. **midnight, `00:00:00` UTC**, the very
start of the daily broadcast-ephemeris file.

Midnight is the **sparse edge** of a daily `brdc` file: only the few satellites
whose time-of-ephemeris (`toe`) sits right at 00:00 are usable at that instant.
On the failing day this yielded only **4 satellites** in view for the target
location — the bare minimum for a 3D fix, with poor geometry. That is not enough
for an autonomous receiver (Garmin) to acquire and hold a fix.

Contrast at the same location, same file:

| `-t` epoch | satellites in view |
|------------|--------------------|
| 00:00 (midnight, what was used) | **4** |
| 12:00 (midday) | **11** |
| 23:59 (other edge) | 1–2 |

The **middle of the day is satellite-rich**; both edges are sparse.

### Why it fooled us
- **The Dual XGPS always locked** because it is A-GPS (gets satellite data from
  the network); it does not need the spoofed signal to carry many satellites, only
  to see the L1 carrier. So it was never a useful indicator of signal health.
- **"It worked yesterday"** because that day's file happened to have ~9 satellites
  at its midnight edge; the failing day's file only had 4. Luck of which SVs had a
  fresh `toe` at 00:00.
- **Not the token / downloads:** on the live box (pi5b) the NASA token was valid
  and downloads were healthy. (An unrelated *retired* copy on Ser8 did have an
  expired token, but that is not the production spoofer.)
- **Not gain:** gain 0 is intentional (receivers sit close to the antenna).
- **Not the ~26 h time offset by itself:** a cold-started Garmin had previously
  accepted even a 48 h-old signal. Age was a red herring; **satellite count at the
  chosen epoch** was the deciding factor.

## Fix

One line in `gpsdata.py`: write **noon (`12:00:00`)** into `latest_time.txt`
instead of the file's first-record (midnight) timestamp, so `gps-sdr-sim`
simulates the satellite-rich midday epoch every run.

```python
# before
ts = f"20{int(p[1]):02d}/{int(p[2]):02d}/{int(p[3]):02d},{int(p[4]):02d}:{int(p[5]):02d}:00"
# after
ts = f"20{int(p[1]):02d}/{int(p[2]):02d}/{int(p[3]):02d},12:00:00"  # midday epoch: sat-rich
```

Noon is always inside a complete daily file's valid `-t` window (`tmin` 00:00 →
`tmax` 23:59), so it is safe for any file that passes the size check.

## Verification

Regenerated the sim with `-t` at noon (11 sats) vs. midnight (4 sats) — the only
variable changed — and transmitted at gain 0. The Garmin went from no-lock to a
**solid lock** after a power-cycle. Confirmed again in normal use the next day.

## Operational note (separate from the code fix)

The Sim/Loop button fires a brief **47 dB blast** (`BLAST_GAIN_DB`) before
dropping to gain 0. That blast overloads the Garmin's front end. Reliable recipe:

> **midday ephemeris + gain-0 continuous (no blast) + power-cycle the Garmin** (cold start to re-acquire).

## Deployment

- `gpsdata.py` patched on **pi5b** and **Ser8** (timestamped `.bak_pre_middayfix_*` backups).
- Committed and pushed to `mmercalde/gps-spoofer` `main` (`e70b889`).

## Recommended follow-ups (not yet done)

1. **Self-tuning epoch:** instead of hardcoding noon, pick the epoch with the most
   SVs in view for the configured location (noon as fallback).
2. **Runtime token guard:** `download_ephemeris()` only checks `status_code == 200`
   — add an `exp` check + 401 logging so an expired token screams instead of
   silently serving stale cache. (This is what bit the retired Ser8 copy.)
3. **Deploy `rotate_token.py`** to the field boxes (currently repo-only), and note
   the GUI renew requires a process restart to drop the cached in-memory token.
4. **Public token exposure:** `gpsdata.py` in this (public) repo carries a live
   NASA token — rotate it and consider moving it out of the tracked file.
