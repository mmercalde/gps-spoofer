import os, gzip, shutil, requests, subprocess, sys, time
from datetime import datetime, timedelta

TOKEN = "eyJ0eXAiOiJKV1QiLCJvcmlnaW4iOiJFYXJ0aGRhdGEgTG9naW4iLCJzaWciOiJlZGxqd3RwdWJrZXlfb3BzIiwiYWxnIjoiUlMyNTYifQ.eyJ0eXBlIjoiVXNlciIsInVpZCI6ImJhamFjYWxpIiwiZXhwIjoxNzc3NDc4ODcxLCJpYXQiOjE3NzIyOTQ4NzEsImlzcyI6Imh0dHBzOi8vdXJzLmVhcnRoZGF0YS5uYXNhLmdvdiIsImlkZW50aXR5X3Byb3ZpZGVyIjoiZWRsX29wcyIsImFjciI6ImVkbCIsImFzc3VyYW5jZV9sZXZlbCI6M30.tFCR-tqISNJxmM8Pd0tM6BQY38bQbatufsRNCD7x98G15uoZp0JTciI3YgH5A6eHE9Kq08i0QW5DmvTi3pZ4AD8bD4L6NB6RZaSHzsLzHexN4Y8fpXZV6nfkJNGvWfUNZxAdwSByj69OesqTqcDPyRF_4eJb-BmrkTMQqHpwyq7KmCDsZWxyAzw8GnONQnAHO_3z5KXk_dcz2pEnXD9rzXzKunBvgf7j5aK7WCJhOpbB1bL5oejvOORGFrekWwTJIWuG_D4a_YUyt8TRqMFlSCW0eM7TZ4qqBFTr7hEbQn9CrnOkP_Lk5kn0ss4W4RhFPxnJwO-t52l2ECoFluwGbA"

DIR  = os.path.expanduser("~/gps_spoofer/ephemeris")
L_T  = os.path.join(DIR, "latest_time.txt")
L_F  = os.path.join(DIR, "latest_file.txt")
L_DL = os.path.join(DIR, "latest_download.txt")
MIN_EPH_SIZE = 200_000  # 200KB minimum — incomplete files are ~50KB


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

    for i in range(4):
        date = datetime.utcnow() - timedelta(days=i)
        y, ys, doy = date.year, str(date.year)[2:], f"{date.timetuple().tm_yday:03d}"
        url = f"https://cddis.nasa.gov/archive/gnss/data/daily/{y}/{doy}/{ys}n/brdc{doy}0.{ys}n.gz"
        out = os.path.join(DIR, f"brdc{doy}0.{ys}n")
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
            if r.status_code == 200:
                with open(out+".gz", "wb") as f: f.write(r.content)
                with gzip.open(out+".gz", "rb") as f_in, open(out, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                os.remove(out+".gz")

                # Reject incomplete files — NASA posts partial files early in the day
                file_size = os.path.getsize(out)
                if file_size < MIN_EPH_SIZE:
                    print(f"WARNING: {os.path.basename(out)} too small ({file_size//1024}KB < 200KB), trying previous day.", file=sys.stderr)
                    os.remove(out)
                    continue

                with open(out, "r") as f:
                    for line in f:
                        if line.strip() and line[0].isdigit():
                            p = line.split()
                            ts = f"20{int(p[1]):02d}/{int(p[2]):02d}/{int(p[3]):02d},{int(p[4]):02d}:{int(p[5]):02d}:00"
                            with open(L_T, "w") as tf: tf.write(ts + "\n")
                            with open(L_F, "w") as ff: ff.write(out + "\n")
                            with open(L_DL, "w") as df:
                                df.write(datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S") + "\n")
                            return out, ts
        except:
            continue
    # All downloads failed — fall back to last good cached ephemeris
    cached_path, cached_ts, cached_age = _load_cached_ephemeris()
    if cached_path and os.path.getsize(cached_path) >= MIN_EPH_SIZE:
        print(f"WARNING: Fresh download failed. Using cached ephemeris ({cached_age}): {os.path.basename(cached_path)}", file=sys.stderr)
        return cached_path, cached_ts
    return None, None


if __name__ == "__main__":
    path, ts = download_ephemeris()
    if path:
        print(f"Ephemeris ready: {os.path.basename(path)}  ({ts})")
    else:
        print("Ephemeris download failed.")
