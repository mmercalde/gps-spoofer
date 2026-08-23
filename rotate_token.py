#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rotate_token.py - fetch a fresh NASA Earthdata Login token and update gpsdata.py.

Logs in to Earthdata Login via the User Tokens API (no headless browser needed),
validates the returned JWT, and - with --apply - patches the TOKEN constant in
gpsdata.py (timestamped backup first).  Optionally scp's the updated file to the
other field machines.

Credentials come from EDL_USERNAME / EDL_PASSWORD env vars; if missing you are
prompted (password hidden).  Never hardcode them in this file.

Examples:
  # dry-run (no writes): just get a token and show what WOULD change
  EDL_USERNAME=bajacali EDL_PASSWORD=... python3 rotate_token.py

  # apply locally, then push the updated file to the field + router
  EDL_USERNAME=bajacali EDL_PASSWORD=... python3 rotate_token.py --apply \
      --file gpsdata.py \
      --targets michael@192.168.1.75:~/gps_spoofer/ michael@192.168.3.10:~/gps_spoofer/
"""

import argparse
import base64
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import requests

URS_TOKEN_URL = "https://urs.earthdata.nasa.gov/api/users/find_or_create_token"
DEFAULT_UID = "bajacali"   # the Earthdata username the token should belong to


def get_credentials():
    username = os.environ.get("EDL_USERNAME")
    password = os.environ.get("EDL_PASSWORD")
    if not username:
        username = input("Earthdata username: ").strip()
    if not password:
        password = getpass.getpass("Earthdata password: ")
    if not username or not password:
        sys.exit("ERROR: missing Earthdata credentials.")
    return username, password


def fetch_token(username, password):
    resp = requests.post(
        URS_TOKEN_URL,
        auth=(username, password),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if not resp.ok:
        sys.exit(f"Earthdata Login failed (HTTP {resp.status_code}): {resp.text.strip()[:300]}")
    try:
        data = resp.json()
    except ValueError:
        sys.exit(f"Earthdata Login returned non-JSON: {resp.text.strip()[:200]}")
    token = data.get("access_token")
    if not token:
        sys.exit(f"No access_token in response: {json.dumps(data)[:200]}")
    return token, data.get("expiration_date")


def decode_jwt_payload(token):
    """Base64url-decode the JWT payload (no signature verification - NASA does that)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as e:
        sys.exit(f"ERROR: token is not a well-formed JWT: {e}")


def main():
    ap = argparse.ArgumentParser(description="Rotate the NASA Earthdata token in gpsdata.py")
    ap.add_argument("--file", default="gpsdata.py", help="path to gpsdata.py (default: ./gpsdata.py)")
    ap.add_argument("--apply", action="store_true", help="write the new token (default: dry-run, no writes)")
    ap.add_argument("--uid", default=DEFAULT_UID, help="expected Earthdata uid (default: %(default)s)")
    ap.add_argument("--targets", nargs="*", default=[],
                    help="scp destinations, e.g. michael@192.168.1.75:~/gps_spoofer/")
    args = ap.parse_args()

    username, password = get_credentials()
    token, expiration = fetch_token(username, password)
    payload = decode_jwt_payload(token)

    exp = payload.get("exp")
    uid = payload.get("uid")
    now = int(datetime.now(timezone.utc).timestamp())

    print(f"Got token: uid={uid}  exp={exp}")
    if exp and exp <= now:
        sys.exit("ERROR: token is already expired!")
    if exp:
        dt = datetime.fromtimestamp(exp, tz=timezone.utc)
        print(f"  expires: {dt.isoformat()}  ({max(0, exp - now) // 86400} days from now)")
    if uid and uid != args.uid:
        print(f"  WARNING: token uid '{uid}' != expected '{args.uid}' - verify before applying.")

    if not os.path.exists(args.file):
        sys.exit(f"ERROR: file not found: {args.file}")
    with open(args.file) as f:
        content = f.read()

    new_content, n = re.subn(r'^TOKEN\s*=\s*"[^"]*"', f'TOKEN = "{token}"', content, count=1, flags=re.M)
    if n != 1:
        sys.exit('ERROR: could not find a TOKEN = "..." line to replace.')
    if new_content == content:
        print("Token is already up to date (no change needed).")
        return

    if not args.apply:
        print(f"[DRY-RUN] would patch {args.file} (backup + write skipped).")
        print(f"[DRY-RUN] would scp to: {', '.join(args.targets) or '(none)'}")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{args.file}.bak_tokrot_{ts}"
    shutil.copy2(args.file, backup)
    with open(args.file, "w") as f:
        f.write(new_content)
    print(f"Updated {args.file}  (backup: {backup})")

    for target in args.targets:
        print(f"scp -> {target}")
        try:
            subprocess.run(["scp", args.file, target], check=True)
            print(f"  ok: {target}")
        except subprocess.CalledProcessError as e:
            print(f"  FAILED: {target}  ({e})")

    print("\nDone. REMINDER: restart any long-running web/GUI process so it drops the")
    print("old in-memory token (the Python module caches gpsdata.py at first import).")


if __name__ == "__main__":
    main()
