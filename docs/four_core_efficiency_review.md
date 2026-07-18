# Four-core (`gpssim_pi5_configurable.c`) efficiency review

**Status:** fixes applied to `gpssim_pi5_configurable.c` and **bench-validated on the
pi5b field unit** (software side — see "On-hardware bench results" below). Binary built
as `~/gps-sdr-sim/gps-sdr-sim-4core-fixed`; production binaries and the Python are
unchanged. Still pending: live RF lock test (HackRF transmit + receiver lock) and
go-live (swap binary + restore the Python `gen_cores` control).
**Context:** the four-core generation path drew heavily on the field Pi5's marginal
power supply while delivering little speedup. This documents why, and what changed.

## Root cause: the serial pre-roll cancelled the parallel speedup

Each 0.1 s epoch splits `iq_buff_size` samples into `pi5_nthreads` contiguous chunks,
one per thread. Before the parallel region runs, each thread needs the channel state
at the start of its chunk. The original code computed that by stepping sample-by-sample
through `snap_advance()` — **serially, on one core**, before any thread could start.

`snap_advance()` runs essentially the same per-sample arithmetic as the real generator
`gen_chunk()`. So at 2.6 MHz with ~10 channels:

| Work per epoch                | Iterations | Where            |
|-------------------------------|-----------:|------------------|
| Serial pre-roll (3 chunks)    |    ~1.95 M | one core, blocking |
| Parallel generation (÷4)      |    ~0.65 M | per core         |

The serial pre-roll was ~3× the parallel per-core work, so wall-time per epoch was
roughly the **same as single-core**, while total CPU work rose to ~1.75× — more current
draw and heat across all four cores for almost no time saved. On a marginal PSU this is
the worst case: maximum power, minimal benefit, no race-to-idle.

## Fix 1 — closed-form O(1) `snap_advance()`

Both accumulators advance by a constant per sample (carrier by `carr_phasestep`, code
by `f_code*delt`), so the end state is a direct calculation, not a loop:

- Carrier: `carr_phase += carr_phasestep * n` (unsigned 32-bit wrap = intended modulo).
- Code: total advance `n * f_code * delt`, number of 1023-chip wraps by division, then
  advance `icode/ibit/iword/dataBit` by whole code periods with carries.

Pre-roll cost drops from ~1.95 M iterations to a few dozen operations. The four-core
path should now approach a true ~4× speedup **and** use less total energy than before
(cores finish sooner and return to idle).

**Verified:** a standalone harness (`scratchpad/test_snap.c`) ran 60,000 cases across
varied start states, step counts (1 … 519,000), and carrier steps (incl. negative) —
**0 mismatches** vs. the original iterative version.

**Caveat:** computing `n*step` in one multiply rounds slightly differently than n
sequential adds, so `code_phase` can differ by <1 LSB at a chunk seam. Sub-sample,
negligible for signal generation; did not surface in testing.

## Fix 2 — race condition in the pseudorange loop

The per-channel pseudorange loop was `#pragma omp parallel for ... private(i)` only.
The scalars `sv`, `path_loss`, `ant_gain`, and `ibs` are function-level, written inside
the loop, so they were **shared** across threads — a data race. A thread could read
another channel's `path_loss`/`ant_gain` when setting `gain[i]`, yielding intermittently
wrong channel gains (only with `path_loss_enable == TRUE`, the default). This is a
likely contributor to the path's "finicky"/unreliable reputation. Fixed by adding those
scalars to the `private(...)` clause (and switching the small fixed loop to
`schedule(static)`).

## On-hardware bench results (pi5b, 2026-07-17)

Built with the original flags (`gcc gpssim_pi5_configurable.c -O3 -mcpu=cortex-a76
-fopenmp -D_FILE_OFFSET_BITS=64 -DUSER_MOTION_SIZE=36000 -I~/gps-sdr-sim/src -lm`).
Test scenario: static `32.924986,-117.123176,100`, ephemeris `brdc1980.26n`,
`-t 2026/07/17,00:00:00 -d 30 -b 8` @ 2.6 MHz (155,480,000-byte output).

**Correctness — byte-exact vs. the trusted single-core binary:**

| Binary                          | vs single-core (`md5`) | Bytes differing        |
|---------------------------------|------------------------|------------------------|
| New 4-core, `GPSSIM_NTHREADS`=1/3/4 | identical           | **0 / 155,480,000**    |
| Old 4-core (pre-fix)            | mismatch               | 1 / 155,480,000 (race) |

New 4-core output is bit-identical to single-core at every thread count (the <1 LSB
seam concern did not surface), and deterministic across reruns. The old binary's
1-byte difference is the pseudorange race (Fix 2) producing one wrong gain sample.

**Speed (30 s scenario):**

| Version                        | Time  | Note                                   |
|--------------------------------|-------|----------------------------------------|
| Single-core (current prod)     | 4.0 s | baseline                               |
| **Old 4-core (pre-fix)**       | 5.2 s | *slower than single-core* — full 4-core power, negative benefit |
| **New 4-core, 4 threads**      | 0.8 s | ~5x vs single-core, ~6.5x vs old 4-core |
| New 4-core, 3 threads          | 1.1 s | ~3.6x vs single-core                   |
| New 4-core, 1 thread           | 3.1 s | gen refactor alone beats old single-core |

**Power/thermal:** `throttled=0x0` throughout (no undervoltage); temp 56.5 -> 65.9 C.
The power win is race-to-idle: cores draw full power for ~0.8 s instead of ~5.2 s.

## Not yet changed (optional follow-ups)

- **Snapshot copies the immutable `ca[1023]` and `dwrd[]` per thread each epoch**
  (~160 KB/s of memcpy). Storing them as `const` pointers into `chan[]` would cut it
  ~99%. Structural change; deferred.
- **The 12→8-bit `>>4` conversion is parallelized but memory-bound** — spinning up all
  cores for near-zero speedup. Consider running it serially or folding it into
  `gen_chunk`'s output store.
- **Default thread count.** `GPSSIM_NTHREADS` (env var, 1–`PI5_NTHREADS_MAX`) already
  lets you cap threads with no recompile. On a marginal PSU, `3` is a good balance:
  most of the speedup, one core free for the OS and `hackrf_transfer`, lower peak draw.

## Deploying / testing the change

```bash
# On the Pi5, rebuild the 4-core binary from the edited source:
gcc -O3 -fopenmp -I~/gps-sdr-sim gpssim_pi5_configurable.c \
    ~/gps-sdr-sim/getopt.o -lm -o ~/gps-sdr-sim/gps-sdr-sim-4core
# (match the exact build flags/objects used originally for this binary)

# Bench-test before field use: generate a known static fix, confirm a receiver
# still locks and reports the spoofed position, and watch for undervoltage:
GPSSIM_NTHREADS=3 ~/gps-sdr-sim/gps-sdr-sim-4core -e <eph> -l <lat,lon,alt> -d 60 -b 8 -o /tmp/t.c8
vcgencmd get_throttled   # 0x0 = no undervoltage/throttle events
```
