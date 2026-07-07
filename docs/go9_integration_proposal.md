# Proposal: Geotab GO9 as a Bench GPS-Spoof Target

**Status:** Proposal / not yet implemented
**Author:** drafted with Claude, 2026-07-06
**Scope:** `gps-spoofer` rig (Pi5b field unit, PiCAN2 Duo, HackRF One, RF chamber)

## Background / decision

The GO9 (board silk `PHXV1MAINv6`, model `GO9-LTETMOBLU`) was salvaged to harvest
its ST LSM6DSL/LSM6DSO 6-DoF IMU. That path is **dropped**: the IMU is a 2.5×3 mm
LGA-14 with a cryptic laser mark, and a pin-compatible breakout costs ~$5. Instead
we use the **whole GO9 as a unit** — bench-activate it, spoof its GNSS with the
existing HackRF rig, and read back the fix it computes. This makes the GO9 a second
spoof target alongside the GLO2, at no desolder cost.

## Objective

On the bench, make the GO9 believe it is installed in a running vehicle so it powers
up and begins tracking, then verify whether it accepts a spoofed GPS position — all
conducted/shielded, no radiation.

## Constraint (what the GO9 will *not* give us)

The GO9 does not expose its IMU/GPS locally. Its expansion port (the `G-USB` pads —
a mini-USB shell that carries **CAN + power, not USB signals**) is an *input*: it is
how third-party add-ons push data *into* the GO9, which relays it over LTE to the
MyGeotab cloud. The GO9's own sensor data leaves the same way and is read back only
via the Geotab SDK against a provisioned device on a rate plan. A surplus eBay unit
is almost certainly unprovisioned.

**Consequence:** verification of the spoof must be **local**, off the GO9's own GNSS
UART (`GPSTX/GPSRX` pads), not via the cloud. This is the one hard design driver.

## Components

**Already on hand**
- GO9 unit (salvaged) + Pi5b field unit
- PiCAN2 Duo (dual MCP2515, SPI) — provides `can0`/`can1` on the Pi5b
- HackRF One + `gps-sdr-sim` + existing `core`/web stack
- RF chamber (enclosure project — coupling antenna + microwave absorber)

**To acquire (low cost)**
- OBD-II **female** connector / pigtail breakout (~$8) — to wire GO9 power + CAN
- Bench 12 V DC supply, ≥1 A (GO9 has under/over-voltage protection; wants ~12 V)
- Jumper wires / small terminal block; twisted pair for the CAN run
- 3.3 V USB-UART adapter *or* use a Pi5b hardware UART for the `GPSTX` NMEA tap
- (Diagnostic) logic analyzer or scope — one-time, to find the GNSS UART baud
- SMA coupler / attenuator to inject HackRF into the GO9 antenna path in-chamber

## Architecture

```
  12V PSU ─────┐
               ├── OBD-II female ── GO9 (OBD-II male plug)
  Pi5b         │      pin16 +12V / pin4,5 GND / pin6 CAN-H / pin14 CAN-L
  ├─ PiCAN2 can0 ── CAN-H/CAN-L  (ECU emulation @ 500k, 120Ω term jumper ON)
  ├─ PiCAN2 can1 ── (reserved: IOX add-on protocol experiments, later)
  ├─ UART Rx ─────── GO9 GPSTX   (3.3V NMEA readback — verify pad + baud first)
  └─ HackRF ── coupler ── GO9 GNSS antenna   (inside RF chamber, conducted)
```

The GO9 uses engine data (RPM / road speed) to decide ignition is on and start a
trip; a nonzero RPM response is what moves it from parked to reporting.

## Implementation plan

### Phase 0 — CAN bring-up + capture (BLOCKING; do this before writing anything)
Nothing downstream is written until we see what the GO9 actually polls.
1. `config.txt` overlays for the Duo (verify oscillator 16 vs 12 MHz and INT pins
   against the board rev):
   ```
   dtparam=spi=on
   dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
   dtoverlay=mcp2515-can1,oscillator=16000000,interrupt=24
   ```
2. `sudo ip link set can0 up type can bitrate 500000` (retry `250000` if silent).
3. `candump -tz can0` with the GO9 powered. Capture: request IDs (`0x7DF` functional
   / `0x7E0` physical), which mode-01 PIDs it asks for, and whether it issues a
   **mode-09 PID 02 (VIN)** request. Save the log to the repo.

**Deliverable:** a `candump` capture that fully specifies the emulator's job.

### Phase 1 — ECU emulator (`go9_ecu.py`, targeted to the Phase-0 capture)
- Respond from `0x7E8` to only the PIDs observed: PID `0C` (RPM, nonzero), `0D`
  (speed), `05` (coolant), plus any others it insists on.
- Mode-09 VIN is multi-frame → ISO-TP with flow control (`can-isotp` / python-can,
  or `isotpsend`). Include only if Phase 0 shows the GO9 blocks on it.
- Confirms success when the GO9 transitions to "engine running / trip active."

### Phase 2 — GPS spoof + local verification
- Reuse `core` HackRF path; couple into the GO9 GNSS antenna **in the RF chamber**,
  conducted, same discipline as the GLO2.
- Read the GO9's resulting fix off `GPSTX` (new threaded `pyserial` reader, same
  shape as `_hackrf_reader`). Confirm the spoofed lat/lon appears in its NMEA.

### Phase 3 — web UI integration (optional)
- Two routes alongside the existing `/api/*`: start/stop bench activation, and a
  GO9-NMEA feed.
- Overlay the GO9's live (spoofed) position on the existing map to watch it track
  the fake route in real time.

## Software components (new)

Folded into the `gps-spoofer` repo as flat modules beside `gps_spoofer_core.py`,
matching house style (no package, `__main__`-runnable):

- `go9_ecu.py` — OBD-II ECU emulator (Phase 1 stub until the capture lands).
- `go9_nmea.py` — GNSS NMEA reader (Phase 2, working; parses GGA/RMC → lat/lon).
- `scripts/can_up.sh`, `scripts/sniff.sh` — SocketCAN bring-up + Phase-0 capture.
- `config/mcp2515-overlay.txt` — PiCAN2 Duo device-tree overlay lines.
- Phase 3 (optional): routes in `gps_spoofer_web.py` (`/api/go9/start|stop|nmea`,
  SSE) reusing `core`'s HackRF path, plus a GO9-position overlay on the existing map.
- Dependencies (new, not in the base app): `python-can` (+ `can-isotp` if VIN
  needed), `pyserial`, and `can-utils` (apt) for the Phase-0 `candump`.

## Open questions (block implementation)

1. **OBD protocol / baud** — 500k vs 250k, and exact PID set. *Resolved by Phase 0.*
2. **VIN gate** — does the GO9 refuse to report without a valid mode-09 VIN, or is
   nonzero RPM + voltage enough? *Resolved by Phase 0.*
3. **`GPSTX` = GNSS NMEA?** — confirm the pad is the module's UART (not an MCU-side
   line) and find the baud (scope). Identify the GNSS module (taped `K-60…`; likely
   u-blox → may emit UBX as well as NMEA).
4. **PiCAN2 Duo board rev** — MCP2515 oscillator (16 vs 12 MHz) and INT GPIOs for
   `can0`/`can1`; wrong values = no bus.
5. **Bench termination** — PiCAN2 120 Ω jumper on; confirm whether the GO9 also
   terminates (a single 120 Ω may suffice for a short bench run).
6. **Provisioning** — if any test wants cloud-side confirmation, is the unit
   provisionable at all without a reseller relationship? (Assume no; rely on local
   NMEA.)

## Reference: pinouts, pads, invariants

- **OBD-II (GO9 plug):** pin 16 = +12 V, pin 4 = chassis GND, pin 5 = signal GND,
  pin 6 = CAN-H, pin 14 = CAN-L (ISO 15765-4, 11-bit CAN).
- **OBD-II CAN IDs:** `0x7DF` functional request, `0x7E0–0x7E7` physical request,
  `0x7E8–0x7EF` ECU response.
- **GO9 board pads (observed):** `G-USB±/G-USB0` = IOX CAN + power (not USB);
  `GPSTX/GPSRX`, `GPS3V3`, `GPSEN`, `ANT1` = GNSS module; 3× TCAN1042 = CAN
  transceivers (matches Geotab's own suggested IOX transceiver list).
- **Repo placement (folded into `gps-spoofer`):** `go9_ecu.py` + `go9_nmea.py` at
  repo root beside `gps_spoofer_core.py`; `scripts/` and `config/` at root; this doc
  at `docs/go9_integration_proposal.md`; Phase-0 captures under `docs/captures/`.
- **RF discipline:** all GNSS injection conducted/shielded inside the RF chamber —
  no radiation, consistent with the GLO2 (antenna-removed) setup.

## Out of scope (recorded for completeness)

- IMU harvest. If ever revisited: LSM6DSL WHO_AM_I (`0x0F`) = `0x6A`, LSM6DSO = `0x6C`;
  both LGA-14, pin-compatible; a $5 breakout is the sane path.
- IOX add-on data injection into MyGeotab (`can1` reserved for it) — requires a
  provisioned device to observe results; parked until a use case appears.
