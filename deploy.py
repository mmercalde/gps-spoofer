#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deploy.py - sync the gps-spoofer runtime files from the repo to every machine.

Runs on Ser8 (the repo is the source of truth).  For each target it backs up the
existing files (timestamped), copies the new ones, and verifies an md5 checksum
so a partial or failed copy can never go unnoticed.

Files synced: gps_spoofer_web.py, gps_spoofer_gui.py, gps_spoofer_core.py,
              gpsdata.py

Usage (on Ser8):
  # preview only - no writes
  ./deploy.py --dry-run

  # deploy to field + router + Ser8 runtime, pulling the repo first
  ./deploy.py --pull --targets field router ser8-runtime --restart
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime

FILES = [
    "gps_spoofer_web.py",
    "gps_spoofer_gui.py",
    "gps_spoofer_core.py",
    "gpsdata.py",
]

REMOTE_TARGETS = {
    "field":  ("michael@192.168.1.75", "~/gps_spoofer/"),
    "router": ("michael@192.168.3.10", "~/gps_spoofer/"),
}

LOCAL_TARGETS = {
    "ser8-runtime": os.path.expanduser("~/gps_spoofer"),
    "staging":      os.path.expanduser("~/gps-spoofer-ui"),
}

SSH_OPTS = [
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "StrictHostKeyChecking=no",
    "-o", "IdentitiesOnly=yes",
]


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sh(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print("  !! command failed:", " ".join(cmd))
        print("     " + (r.stderr or r.stdout).strip()[:400])
    return r


def remote_md5s(host, remote_dir):
    out = sh(["ssh", *SSH_OPTS, host, "cd %s && md5sum %s 2>/dev/null" % (remote_dir, " ".join(FILES))], check=False)
    result = {}
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            result[os.path.basename(parts[1])] = parts[0]
    return result


def main():
    all_targets = sorted(list(REMOTE_TARGETS) + list(LOCAL_TARGETS))
    ap = argparse.ArgumentParser(description="Sync gps-spoofer runtime files to all machines")
    ap.add_argument("--repo", default=os.path.expanduser("~/gps-spoofer-repo"),
                    help="source repo dir (default: ~/gps-spoofer-repo)")
    ap.add_argument("--targets", nargs="*", default=["field", "router", "ser8-runtime"],
                    help="targets to deploy to (default: field router ser8-runtime). choices: " + ", ".join(all_targets))
    ap.add_argument("--pull", action="store_true", help="git pull in the repo before deploying")
    ap.add_argument("--restart", action="store_true", help="restart the field web UI after deploy")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen; write nothing")
    args = ap.parse_args()

    repo = os.path.abspath(os.path.expanduser(args.repo))
    if not os.path.isdir(repo):
        sys.exit("ERROR: repo not found: " + repo)

    targets = []
    for t in args.targets:
        for name in t.replace(",", " ").split():
            if name not in REMOTE_TARGETS and name not in LOCAL_TARGETS:
                sys.exit("ERROR: unknown target '%s'. choices: %s" % (name, ", ".join(all_targets)))
            targets.append(name)

    if args.pull and not args.dry_run:
        print("=== git pull ===")
        sh(["git", "-C", repo, "pull"])

    src = {f: os.path.join(repo, f) for f in FILES}
    missing = [f for f, p in src.items() if not os.path.exists(p)]
    if missing:
        sys.exit("ERROR: missing source files in repo: " + ", ".join(missing))
    src_md5 = {f: md5(p) for f, p in src.items()}

    if not args.dry_run:
        st = sh(["git", "-C", repo, "status", "--short", "gps_spoofer_core.py"], check=False)
        if st.stdout.strip():
            print("WARNING: gps_spoofer_core.py has uncommitted changes (4-core work) - those WILL be deployed.")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []

    for name in targets:
        print("=== " + name + " ===")
        if args.dry_run:
            print("  [DRY-RUN] would back up + copy " + str(len(FILES)) + " files")
            results.append((name, "dry-run"))
            continue

        if name in LOCAL_TARGETS:
            dest = LOCAL_TARGETS[name]
            os.makedirs(dest, exist_ok=True)
            for f in FILES:
                dst = os.path.join(dest, f)
                if os.path.exists(dst):
                    shutil.copy2(dst, dst + ".bak_deploy_" + ts)
                shutil.copy2(src[f], dst)
            ok = all(md5(os.path.join(dest, f)) == src_md5[f] for f in FILES)
        else:
            host, remote_dir = REMOTE_TARGETS[name]
            backup_cmd = "cd %s && for f in %s; do [ -f \"$f\" ] && cp -a \"$f\" \"$f.bak_deploy_%s\"; done; true" % (remote_dir, " ".join(FILES), ts)
            sh(["ssh", *SSH_OPTS, host, backup_cmd], check=False)
            sh(["scp", *SSH_OPTS, *[src[f] for f in FILES], host + ":" + remote_dir])
            rm = remote_md5s(host, remote_dir)
            ok = all(rm.get(f) == src_md5[f] for f in FILES)
            if not ok:
                for f in FILES:
                    if rm.get(f) != src_md5[f]:
                        print("  MISMATCH %s: local=%s remote=%s" % (f, src_md5[f], rm.get(f)))

        print("  " + ("OK" if ok else "MISMATCH"))
        results.append((name, "OK" if ok else "MISMATCH"))

    if args.restart and not args.dry_run:
        print("=== restart field web UI ===")
        host, _ = REMOTE_TARGETS["field"]
        sh(["ssh", *SSH_OPTS, host,
            "pkill -f gps_spoofer_web.py; sleep 1; cd ~/gps_spoofer && nohup python3 gps_spoofer_web.py >/tmp/gps_web.log 2>&1 </dev/null &"],
           check=False)
        print("  restarted (note: the touchscreen GUI must be restarted separately)")

    print("")
    print("=== summary ===")
    for name, status in results:
        print("  %-14s %s" % (name, status))
    bad = [n for n, s in results if s not in ("OK", "dry-run")]
    if bad:
        sys.exit("FAILED: " + ", ".join(bad))
    print("Done.")


if __name__ == "__main__":
    main()
