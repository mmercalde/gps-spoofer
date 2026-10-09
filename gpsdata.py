import os, gzip, shutil, requests, subprocess, sys, time
from datetime import datetime, timedelta

TOKEN = "PASTE_EARTHDATA_BEARER_TOKEN_HERE"  # do NOT commit a real token; set via GUI "RENEW TOKEN" or rotate_token.py

DIR  = os.path.expanduser("~/gps_spoofer/ephemeris")
L_T  = os.path.join(DIR, "latest_time.txt")
L_F  = os.path.join(DIR, "latest_file.txt")
L_DL = os.path.join(DIR, "latest_download.txt")
MIN_EPH_SIZE = 200_000  # 200KB minimum — incomplete files are ~50KB (legacy; selection now uses SV coverage, not size)

TOE_WINDOW_SEC   = 2 * 3600   # GPS broadcast ephemeris fit interval is ~±2h around TOE
MIN_NEAR_TOE_PRN = 6          # min distinct SVs within the fit window of -t for a lockable scenario (Garmin-safe)
GPS_SDR_SIM = os.path.expanduser("~/gps-sdr-sim/gps-sdr-sim")  # authority on a file's usable time window


class EphemerisStaleError(Exception):
    """No available ephemeris has enough satellites near the requested sim-time.
    Raised instead of silently keying a sparse/stale scenario (the tonight-bug guardrail)."""
    pass


def _parse_nav_records(filepath):
    """Yield (prn, epoch_datetime) for each RINEX-2 brdc nav record.
    epoch is the record's TOC/line-0 time, used as the TOE-proximity anchor."""
    recs = []
    try:
        with open(filepath, errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return recs
    hdr = next((i for i, l in enumerate(lines) if "END OF HEADER" in l), None)
    if hdr is None:
        return recs
    data = [l for l in lines[hdr + 1:] if l.strip()]
    for i in range(0, len(data) - 7, 8):          # 8 lines per nav record
        toks = data[i].split()
        try:
            prn = int(toks[0])
            yy, mm, dd, hh, mi = (int(toks[1]), int(toks[2]), int(toks[3]), int(toks[4]), int(toks[5]))
            sec = int(float(toks[6])) if len(toks) > 6 else 0
            yr = 2000 + yy if yy < 80 else 1900 + yy
            recs.append((prn, datetime(yr, mm, dd, hh, mi, min(sec, 59))))
        except Exception:
            continue
    return recs


def near_toe_prn_count(filepath, when, window_sec=TOE_WINDOW_SEC):
    """Count DISTINCT PRNs that have a nav record whose epoch (TOC/TOE) is within +/-window of `when`.
    This is THE lockability metric: it is both the selection score and the guardrail check."""
    prns = {prn for prn, ep in _parse_nav_records(filepath)
            if abs((ep - when).total_seconds()) <= window_sec}
    return len(prns)


def _file_epoch_bounds(filepath):
    eps = [ep for _, ep in _parse_nav_records(filepath)]
    return (min(eps), max(eps)) if eps else (None, None)


def _gps_sdr_sim_tmax(filepath):
    """Ask gps-sdr-sim itself for this file's last USABLE sim-time (its tmax).
    An out-of-range -t makes gps-sdr-sim print 'tmin=.. tmax=..' and exit BEFORE
    generating anything, so this is a cheap, authoritative metadata probe — not a
    generate/fail/retry. A partial current-day file's last record (TOC) can sit past
    its last usable TOE; this returns the value gps-sdr-sim will actually accept.
    Returns a datetime, or None if it couldn't be determined."""
    import re
    try:
        r = subprocess.run(
            [GPS_SDR_SIM, "-e", filepath, "-t", "2099/01/01,00:00:00",
             "-l", "0,0,0", "-d", "1", "-o", os.devnull],
            capture_output=True, text=True, timeout=20)
        m = re.search(r"tmax\s*=\s*(\d{4})/(\d{2})/(\d{2}),(\d{2}):(\d{2}):(\d{2})",
                      r.stdout + r.stderr)
        if m:
            return datetime(*map(int, m.groups()))
    except Exception:
        pass
    return None


def select_best_ephemeris(when=None, candidates=None):
    """Pick the on-disk ephemeris giving the most satellites near `when` (default utcnow),
    and the sim-time -t to replay it at (= `when`, clamped into the file's epoch range so
    gps-sdr-sim won't reject it).

    Returns (filepath, ts_str). Raises EphemerisStaleError if the best candidate has fewer
    than MIN_NEAR_TOE_PRN SVs near -t, or if -t must be clamped more than the fit window away
    from `when` (i.e. only a stale file exists). This is the loud guardrail that replaces the
    old silent fall-back to noon-of-yesterday."""
    import glob
    if when is None:
        when = datetime.utcnow()
    if candidates is None:
        candidates = sorted(glob.glob(os.path.join(DIR, "brdc*.??n")))
    best = None  # (count, filepath, t_dt)
    for fp in candidates:
        tmin, tmax = _file_epoch_bounds(fp)
        if tmin is None:
            continue
        cnt = near_toe_prn_count(fp, when)        # score by SV coverage near NOW (a stale file scores ~0)
        t = min(max(when, tmin), tmax)            # -t = now, clamped into the file's replayable range
        if best is None or cnt > best[0]:
            best = (cnt, fp, t)
    if best is None:
        raise EphemerisStaleError("No ephemeris files available to select from.")
    cnt, fp, t = best
    # Clamp -t to the file's LAST USABLE time per gps-sdr-sim itself, so the value we
    # write to latest_time.txt is accepted up front (no generate/fail/retry downstream).
    # cnt stays the coverage-near-NOW score (the guardrail is about "now", not the clamp).
    tmax_usable = _gps_sdr_sim_tmax(fp)
    if tmax_usable is not None and t > tmax_usable:
        t = tmax_usable
    stale_sec = abs((t - when).total_seconds())
    if cnt < MIN_NEAR_TOE_PRN:
        raise EphemerisStaleError(
            f"Best ephemeris {os.path.basename(fp)} has only {cnt} SV(s) within "
            f"+/-{TOE_WINDOW_SEC//3600}h of {when:%Y/%m/%d %H:%M} UTC (need >= {MIN_NEAR_TOE_PRN}). "
            f"Refusing to transmit a sparse scenario.")
    if stale_sec > TOE_WINDOW_SEC:
        raise EphemerisStaleError(
            f"Best ephemeris {os.path.basename(fp)} can only be replayed at {t:%Y/%m/%d %H:%M} UTC, "
            f"{stale_sec/3600:.1f}h from now. Refusing stale scenario.")
    return fp, t.strftime("%Y/%m/%d,%H:%M:%S")


def _wait_for_ntp_sync(max_wait_sec=30):
    """
    Block until NTP service is enabled AND clock is synchronised, or timeout.
    - If NTP service is explicitly disabled (NTP=no), fails immediately.
    - If NTP is enabled but not yet synced, waits up to max_wait_sec.
    Returns True only if NTP=yes and NTPSynchronized=yes.
    """
    for attempt in range(max_wait_sec):
        try:
            out = subprocess.check_output(
                ["timedatectl", "show"],
                text=True, stderr=subprocess.DEVNULL)
            props = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
            ntp_enabled = props.get("NTP") == "yes"
            ntp_synced  = props.get("NTPSynchronized") == "yes"

            if ntp_enabled and ntp_synced:
                return True
            # If NTP service is explicitly off, no point waiting
            if not ntp_enabled:
                return False
        except Exception:
            pass
        time.sleep(1)
    return False


def _load_cached_ephemeris():
    """
    Return (filepath, timestamp, age_str) from the last successful download.
    Returns (None, None, None) if no valid cache exists.
    """
    try:
        with open(L_F, "r") as f:
            filepath = f.read().strip()
        with open(L_T, "r") as f:
            timestamp = f.read().strip()
        if not (filepath and os.path.exists(filepath) and timestamp):
            return None, None, None

        age_str = "unknown age"
        if os.path.exists(L_DL):
            with open(L_DL, "r") as f:
                dl_time_str = f.read().strip()
            try:
                dl_time = datetime.strptime(dl_time_str, "%Y-%m-%dT%H:%M:%S")
                age_secs = int((datetime.utcnow() - dl_time).total_seconds())
                if age_secs < 0:
                    age_str = "unknown age (clock issue)"
                elif age_secs < 3600:
                    age_str = f"{age_secs // 60} minutes ago"
                elif age_secs < 86400:
                    h, m = divmod(age_secs, 3600)
                    age_str = f"{h}h {m // 60}m ago"
                else:
                    d, rem = divmod(age_secs, 86400)
                    age_str = f"{d} day(s) {rem // 3600}h ago"
            except Exception:
                pass

        return filepath, timestamp, age_str
    except Exception:
        pass
    return None, None, None


def download_ephemeris():
    os.makedirs(DIR, exist_ok=True)

    print("Checking NTP sync status...", file=sys.stderr)
    synced = _wait_for_ntp_sync(max_wait_sec=30)

    if not synced:
        print("WARNING: NTP not synchronised.", file=sys.stderr)
        cached_path, cached_ts, cached_age = _load_cached_ephemeris()
        if cached_path:
            print(
                f"Using cached ephemeris:\n"
                f"  File      : {os.path.basename(cached_path)}\n"
                f"  Epoch     : {cached_ts}\n"
                f"  Downloaded: {cached_age}\n"
                f"  Note      : Ephemeris is valid for ~24hrs. "
                f"Generation may fail if the file is too old.",
                file=sys.stderr
            )
            return cached_path, cached_ts
        else:
            print(
                "ERROR: No cached ephemeris available and NTP is not synced.\n"
                "       Connect to a network so the clock can sync, then try again.",
                file=sys.stderr
            )
            return None, None

    print("NTP synchronised. Proceeding with download.", file=sys.stderr)

    now = datetime.utcnow()
    # Download the current UTC day AND recent days as candidates. Keep ALL of them,
    # INCLUDING today's small partial — selection happens afterwards by satellite
    # coverage near `now`, NOT by file size. (The old size-reject discarded today's
    # fresh partial and silently fell back to a stale complete file -> the tonight bug.)
    for i in range(3):
        date = now - timedelta(days=i)
        y, ys, doy = date.year, str(date.year)[2:], f"{date.timetuple().tm_yday:03d}"
        url = f"https://cddis.nasa.gov/archive/gnss/data/daily/{y}/{doy}/{ys}n/brdc{doy}0.{ys}n.gz"
        out = os.path.join(DIR, f"brdc{doy}0.{ys}n")
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
            if r.status_code == 200 and r.content:
                with open(out+".gz", "wb") as f: f.write(r.content)
                with gzip.open(out+".gz", "rb") as f_in, open(out, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                os.remove(out+".gz")
        except Exception:
            continue

    # Select by SV coverage near `now` (today's partial beats a stale complete file),
    # stamp -t = now, and REFUSE loudly if nothing is sat-rich near now. Replaces the
    # old "noon of whatever file won + silent cache fallback" path.
    try:
        fp, ts = select_best_ephemeris(now)
    except EphemerisStaleError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return None, None

    with open(L_T, "w") as tf: tf.write(ts + "\n")
    with open(L_F, "w") as ff: ff.write(fp + "\n")
    with open(L_DL, "w") as df: df.write(now.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
    sv = near_toe_prn_count(fp, datetime.strptime(ts, "%Y/%m/%d,%H:%M:%S"))
    print(f"Selected {os.path.basename(fp)}  -t {ts}  ({sv} SVs within "
          f"+/-{TOE_WINDOW_SEC//3600}h of -t)", file=sys.stderr)
    return fp, ts


if __name__ == "__main__":
    path, ts = download_ephemeris()
    if path:
        print(f"Ephemeris ready: {os.path.basename(path)}  ({ts})")
    else:
        print("Ephemeris download failed.")
