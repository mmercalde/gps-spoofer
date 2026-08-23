#!/bin/sh
# Reset the Waveshare 4.3" DSI touch controller (FocalTech edt-ft5506) and
# re-bind its driver. The panel overlay omits the proper touch reset, so the
# controller can come up in a bad state (intermittent touch).
#
# The touch reset is line 1 of the "7inch-touchscreen-p" GPIO chip
# (i2c 11-0045, rpi_touchscreen_attiny), active-low.

chip=$(gpiodetect 2>/dev/null | awk '/7inch-touchscreen/{print $1; exit}' | sed 's/gpiochip//')
if [ -z "$chip" ]; then
    echo "gps-touch-reset: touchscreen GPIO chip not found" >&2
    exit 1
fi

for d in /sys/bus/i2c/devices/*; do
    [ -f "$d/name" ] || continue
    if [ "$(cat "$d/name" 2>/dev/null)" = "edt-ft5506" ]; then
        id=$(basename "$d")
        # release the driver's hold on the reset line
        echo "$id" > /sys/bus/i2c/drivers/edt_ft5x06/unbind 2>/dev/null
        sleep 0.5
        # pulse reset: assert (active-low) then de-assert
        gpioset "$chip" 1=0
        sleep 0.3
        gpioset "$chip" 1=1
        sleep 0.3
        echo "$id" > /sys/bus/i2c/drivers/edt_ft5x06/bind 2>/dev/null
        echo "gps-touch-reset: reset + re-bound touch $id"
    fi
done
