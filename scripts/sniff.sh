#!/usr/bin/env bash
# Phase 0: bring up can0 and capture what the GO9 polls.
# Usage: ./scripts/sniff.sh [bitrate]   (default 500000; retry 250000 if silent)
set -euo pipefail
BITRATE="${1:-500000}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$(cd "$HERE/.." && pwd)/docs/captures"
mkdir -p "$DIR"
LOG="$DIR/go9_candump_$(date +%Y%m%d_%H%M%S)_${BITRATE}.log"
"$HERE/can_up.sh" "$BITRATE" can0
echo "Capturing to $LOG  (Ctrl-C to stop)."
echo "Watch for 0x7DF/0x7E0 requests and a mode-09 (VIN) request."
candump -tz can0 | tee "$LOG"
