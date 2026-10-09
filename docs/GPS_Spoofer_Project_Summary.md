# GPS Spoofer Project — Master Summary
**Last Updated: March 1, 2026 — §3 ephemeris-timing doctrine amended October 8, 2026**

> ⚠️ **READ §3 FIRST.** The ephemeris timestamp rule changed on 2026-10-08. The old
> "always use midnight UTC 00:00:00" instruction was **REMOVED** because it causes a
> no-lock failure at the UTC-day rollover. Never restore it, and never hand-write
> `latest_time.txt` to a midnight timestamp.

---

## 1. Hardware Setup

### Primary Spoofer (Pi5)
- Raspberry Pi 5 — transmit node, runs gps-sdr-sim + hackrf_transfer
- HackRF One — connected via USB, transmits GPS L1 at 1575.42 MHz
- Proxicast active puck antenna — SMA, operates in bypass (passive) mode
- Power: Pi5 requires official 5V/5A USB-C supply — do NOT use 12V car charger
- WiFi: Pi5 connects to phone hotspot "Nuu25" at 192.168.43.100:5000

### GLONASS Jammer (PortaPack)
- HackRF One with PortaPack — standalone device, self-contained screen + battery
- Settings: Transmitter → Jammer → 1602 MHz, 10 MHz bandwidth, max power
- Purpose: Jams GLONASS L1 band so GLO2 cannot lock real GLONASS satellites

### Target Receivers
- Garmin GLO 2 — GPS + GLONASS, Bluetooth SPP, 10Hz, MediaTek chipset
- XGPS (Dual Electronics) — GPS-only, locks instantly and reliably
- Android phones — lock via mock location app (Bluetooth GNSS)

### Development Machine (Ser8)
- AMD Ryzen 16-core, Ubuntu, NVMe 476GB — primary dev/test system
- Runs gps_spoofer_web.py (Flask UI) or gps_spoofer_gui.py (tkinter)
- Files in ~/gps_spoofer/

---

## 2. Software Files

### Core Files (~/gps_spoofer/)
- `gps_spoofer_gui.py` — Pi5 tkinter GUI, road routing, 3600s max duration
- `gps_spoofer_web.py` — Ser8 Flask web UI, moving map, road routing
- `gps_spoofer_core.py` — shared backend, HackRF auto-reconnect, route generation
- `gpsdata.py` — ephemeris downloader + selector (see §3 for current selection logic)
- `glonass_inject.py` — GLONASS noise post-processor (experimental, not for Pi5)

### gps-sdr-sim Modifications
- `USER_MOTION_SIZE` changed from 3000 to 36000 in `~/gps-sdr-sim/src/gpssim.h`
- Recompiled with OpenMP on Pi5 — 5.4x speedup over stock binary
- Backup: `~/gps-sdr-sim/gps-sdr-sim.bak_pre_3600s_20260227`

### Key Features Added
- Google Directions API road-following routes with "Follow Roads" checkbox
- "Use Real Drive Time" button — sets duration to match actual route drive time
- Duration slider max increased to 3600s (1 hour)
- Moving map with real-time position overlay (Flask web UI)
- HackRF auto-reconnect on USB disconnect
- Log streaming with SSE (Server-Sent Events)

### Transmission Command (Pi5)
```bash
hackrf_transfer -t sim_output/gpssim.c8 -f 1575420000 -s 2600000 -a 1 -x 20 -R
```

---

## 3. Ephemeris Management

> **TIMING DOCTRINE CHANGED — October 8, 2026.** The former "always use midnight UTC
> `00:00:00` — do NOT use current time" rule has been **removed**: it builds a stale/
> sparse scenario at the UTC-day rollover and causes a no-lock on every receiver. The
> selector now sets `-t` to **current UTC** on a satellite-rich current-day file. Do
> not restore the old behavior. Incident details at the end of this section.

- Source: NASA CDDIS daily broadcast nav — `brdc{DOY}0.{YY}n`, plus the current-day hourly/partial file when the complete daily doesn't yet cover "now"
- Earthdata token: Bearer token used by `gpsdata.py`; renew at https://urs.earthdata.nasa.gov when fetches start returning HTTP 401. (The token baked into older notes and `safe_patch.py` is expired — scrub those cleartext copies.)
- **Validity is per-record, not per-file:** each satellite's ephemeris record is usable only within gps-sdr-sim's fit window (~±2 h) around its TOE. The sim start time `-t` must land where many PRNs have a record inside that window — i.e. a *satellite-rich* moment.

### Selecting the ephemeris + start time (current design)
- **Judge files by satellite coverage, NOT byte size.** The selector counts distinct PRNs whose nearest TOE is within the fit window of the candidate `-t`, and picks the file with the best coverage near *now*. A current-day **partial** file is legitimately smaller than a complete daily and must **not** be rejected for being small.
- **Set `-t` ≈ current UTC** on a current-day, satellite-rich file — this gives recency and dense coverage at once. Noon-of-day is only a degraded fallback.
- **Clamp `-t` to the file's last usable TOE** (gps-sdr-sim's reported `tmax`) before writing `latest_time.txt`, so the pointer is always a value gps-sdr-sim accepts first-try (no "Invalid start time" / generate-fail-retry).
- **Guardrail:** if the best coverage near `-t` is below threshold, **REFUSE to transmit with a loud error** — never key a stale or sparse scenario. In the first ~20–30 min after 00:00 UTC a current-day partial may be too thin; refusing and waiting a few minutes for it to fill is correct, not falling back to a stale complete file.

### If receivers won't lock — recover safely
- Re-run **Update Eph** (the selector). It fetches a current-day file and stamps a valid near-now `-t` automatically.
- **Do NOT hand-write `latest_time.txt`, and NEVER set it to `...,00:00:00`.** A midnight or stale timestamp is exactly what caused the October 2026 no-lock incident.
- A genuinely truncated/incomplete download is still bad — but detect that by **coverage**, not by a fixed byte threshold.

### Incident — October 8, 2026 (why the old doctrine was removed)
At the UTC-day rollover, a `MIN_EPH_SIZE = 200 KB` check discarded that day's fresh partial file for being "too small," fell back to the previous day's complete file, and the midday logic stamped it **noon-of-yesterday (~13 h stale)**. The resulting scenario carried only ~3 satellites, so **both** receivers failed to lock — a selection fault, not RF or clock. Fixed by: coverage-based file scoring (stale files score ~0 and cannot win), `-t` ≈ now, the `tmax` edge-clamp, and the refuse-if-sparse guardrail above. Verified end-to-end through the **production** generate path (Update Eph → Generate → Transmit) with both the XGPS150 and the cold-booted Garmin locking immediately.

---

## 4. GLO2 GLONASS Lock Problem — Root Cause & Solutions

### Root Cause
The Garmin GLO 2 receives both GPS and GLONASS. When real GLONASS satellites are visible, the GLO2 conflicts the real GLONASS position with the spoofed GPS position and refuses to lock. The XGPS is GPS-only so it locks instantly and reliably.

### Confirmed Working Solution (Two-Device)
- Pi5 + HackRF: transmits GPS spoof on 1575.42 MHz as normal
- PortaPack: transmits GLONASS noise on 1602 MHz, 10 MHz BW
- With GLONASS jammed, GLO2 falls back to GPS-only and locks on spoof
- Tested successfully indoors under metal roof — confirmed spoofed coordinates on GLO2

### PortaPack GLONASS Jammer Command (alternative via second HackRF)
```bash
hackrf_transfer -f 1602000000 -s 20000000 -a 1 -x 47 -t /dev/urandom
```

### Permanent Single-Device Solution (Recommended — ORDER THIS)
**GPS L1 Bandpass Filter — $19.59 on Amazon (ASIN: B0FGG1MM8Q)**
- Centered at 1575.42 MHz, passes GPS, blocks GLONASS (>40dB rejection at 1615 MHz)
- SMA connectors, 45x26x12mm (1.77" x 1.02" x 0.47"), aluminum housing
- Goes inline between HackRF antenna and GLO2 antenna
- No extra hardware, no power, no processing — completely passive

### PMTK Command Research (Dead End — Do Not Retry)
- GLO2 uses MediaTek (MTK) chipset — PMTK353 constellation control exists in spec
- Bluetooth SPP port is READ-ONLY — Garmin locked PMTK command input over BT
- USB port uses Garmin proprietary binary protocol — not standard NMEA serial
- No ACK to `$PMTK000*32` on either BT or USB — command interface is fully blocked
- Conclusion: Cannot disable GLONASS via software on GLO2

---

## 5. Signal Strength & Gain Notes

- Real GPS satellites: SNR 30-40 dB typical
- Spoof at 6-8 inches: SNR 45-58 dB (overdriving — too strong)
- Working gain range: `-x 20` to `-x 30` for most scenarios
- Android locks fastest, XGPS second, GLO2 slowest (due to GLONASS)
- Metal roof / indoors = natural Faraday effect, blocks real satellites, helps locking
- Faraday cage from Tesla coil experiments available for contained testing

---

## 6. Default Spoof Location

**9392 Twinford Court — 32.924986°N, 117.123176°W (San Diego area)**

---

## 7. Common Issues & Fixes

### No Fix / Receivers Not Locking
- Re-run the ephemeris selector (**Update Eph**) — it picks a current-day, satellite-rich file and sets `-t` ≈ now automatically. Do **not** hand-write `latest_time.txt`.
- **NEVER set the timestamp to midnight UTC `00:00:00`** (see §3) — that reintroduces the October 2026 no-lock bug.
- Check NTP sync on the Pi before downloading ephemeris.
- A truncated/incomplete download is still bad — but judge current-day partial files by **satellite coverage near `-t`**, not by byte size.
- If the selector refuses with a "coverage too low near now" error, that's **by design** (e.g. the first ~20–30 min after 00:00 UTC) — wait a few minutes for the current-day file to fill, then retry. Do not force a stale file.

### GLO2 Won't Lock on Spoof
- Run PortaPack GLONASS jammer on 1602 MHz simultaneously
- Or test indoors / under metal roof (blocks real GLONASS naturally)
- Or order GPS L1 bandpass filter (Amazon B0FGG1MM8Q, $19.59)
- Power cycle GLO2 after spoof is already transmitting

### HackRF Not Found
```bash
hackrf_info        # verify connection
pkill -f gps_spoofer && pkill -f hackrf_transfer   # kill zombies
```

---

## 8. NASA Earthdata Token

- Renew at: https://urs.earthdata.nasa.gov → Generate Token (renew whenever ephemeris fetches start returning HTTP 401)
- Update in `gpsdata.py`: `TOKEN = "<new_token>"`
- Test: `python3 -c "import gpsdata; print(gpsdata.download_ephemeris())"`
- Do NOT leave cleartext tokens in helper scripts (e.g. `safe_patch.py`) — scrub them.

### Token Update Command
```bash
python3 -c "
import re
new_token = '<paste_new_token_here>'
with open('/home/michael/gps_spoofer/gpsdata.py', 'r') as f:
    content = f.read()
new_content = re.sub(r'TOKEN = \"[^\"]+\"', f'TOKEN = \"{new_token}\"', content)
with open('/home/michael/gps_spoofer/gpsdata.py', 'w') as f:
    f.write(new_content)
print('Token updated')
"
```

---

## 9. File Locations

| File | Location |
|------|----------|
| GUI (Pi5) | `~/gps_spoofer/gps_spoofer_gui.py` |
| Web UI (Ser8) | `~/gps_spoofer/gps_spoofer_web.py` |
| Core backend | `~/gps_spoofer/gps_spoofer_core.py` |
| Ephemeris downloader/selector | `~/gps_spoofer/gpsdata.py` |
| GLONASS injector | `~/gps_spoofer/glonass_inject.py` |
| gps-sdr-sim binary | `~/gps-sdr-sim/gps-sdr-sim` |
| gpssim.h (modified) | `~/gps-sdr-sim/src/gpssim.h` |
| Ephemeris files | `~/gps_spoofer/ephemeris/` |
| Sim output | `~/gps_spoofer/sim_output/gpssim.c8` |
