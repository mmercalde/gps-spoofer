#!/bin/sh
# Re-initialize the Waveshare 4.3" DSI touch controller (FocalTech edt-ft5506).
# The touch can come up in a bad state (intermittent); unbind/rebind the I2C
# driver to force a clean re-init. Idempotent and safe to run any time.
for d in /sys/bus/i2c/devices/*; do
    [ -f "$d/name" ] || continue
    if [ "$(cat "$d/name" 2>/dev/null)" = "edt-ft5506" ]; then
        id=$(basename "$d")
        echo "$id" > /sys/bus/i2c/drivers/edt_ft5x06/unbind 2>/dev/null
        sleep 1
        echo "$id" > /sys/bus/i2c/drivers/edt_ft5x06/bind 2>/dev/null
        echo "gps-touch-rebind: re-initialized $id"
    fi
done
