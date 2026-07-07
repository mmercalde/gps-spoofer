"""go9_nmea.py - read the GO9's GNSS NMEA off its GPSTX UART and extract the fix.

Part of the GO9 bench-target integration (see docs/go9_integration_proposal.md).
Phase 2 verification: with the HackRF spoof running (gps_spoofer_core), confirm the
GO9's own computed fix follows the spoofed position. Baud comes from the Phase-0
logic-analyzer hunt (u-blox default is often 9600). GPSTX is 3.3 V - safe straight
into a Pi UART.

Run standalone:
    python3 go9_nmea.py --port /dev/serial0 --baud 9600 [--raw]
"""
from __future__ import annotations
import argparse


def _dm_to_deg(dm: str, hemi: str) -> float | None:
    """Convert NMEA ddmm.mmmm + hemisphere to signed decimal degrees."""
    if not dm:
        return None
    dot = dm.find(".")
    if dot < 3:
        return None
    deg = float(dm[: dot - 2])
    minutes = float(dm[dot - 2 :])
    val = deg + minutes / 60.0
    return -val if hemi in ("S", "W") else val


def parse_fix(line: str):
    """Return (lat, lon, source) from a GGA or RMC sentence, else None."""
    if not line.startswith("$"):
        return None
    f = line.strip().split(",")
    typ = f[0][3:] if len(f[0]) >= 6 else ""
    try:
        if typ == "GGA" and len(f) >= 6 and f[2] and f[4]:
            return _dm_to_deg(f[2], f[3]), _dm_to_deg(f[4], f[5]), "GGA"
        if typ == "RMC" and len(f) >= 7 and f[2] == "A" and f[3] and f[5]:
            return _dm_to_deg(f[3], f[4]), _dm_to_deg(f[5], f[6]), "RMC"
    except (ValueError, IndexError):
        return None
    return None


def run(port: str, baud: int, raw: bool = False) -> None:
    import serial  # pyserial (lazy: parser stays usable without the dep)

    with serial.Serial(port, baud, timeout=2) as ser:
        print(f"# reading NMEA on {port} @ {baud}")
        while True:
            try:
                line = ser.readline().decode("ascii", "replace").strip()
            except serial.SerialException as e:
                print(f"# serial error: {e}")
                break
            if not line:
                continue
            if raw:
                print(line)
            fix = parse_fix(line)
            if fix and fix[0] is not None:
                lat, lon, src = fix
                print(f"{src}  lat={lat:.6f}  lon={lon:.6f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="GO9 GNSS NMEA reader (Phase 2)")
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=9600,
                    help="from Phase-0 baud hunt; u-blox default often 9600")
    ap.add_argument("--raw", action="store_true", help="also print raw sentences")
    a = ap.parse_args()
    run(a.port, a.baud, a.raw)
