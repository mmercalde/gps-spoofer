#!/usr/bin/env bash
# Bring up a CAN iface at a given bitrate. Default: can0 @ 500000.
set -euo pipefail
BITRATE="${1:-500000}"
IFACE="${2:-can0}"
sudo ip link set "$IFACE" down 2>/dev/null || true
sudo ip link set "$IFACE" up type can bitrate "$BITRATE"
echo "$IFACE up @ ${BITRATE} bps"
ip -details -statistics link show "$IFACE"
