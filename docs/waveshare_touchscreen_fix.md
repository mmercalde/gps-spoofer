# Waveshare 4.3" DSI touchscreen — intermittent touch fix

## Symptom
The Pi5 field unit's touchscreen (Waveshare 4.3" 800x480 MIPI DSI, capacitive)
sometimes works and sometimes doesn't — taps are ignored until a reboot.

## Hardware / driver
- Controller: FocalTech edt-ft5506 (FT5x06) on the DSI panel I2C bus
  (i2c-11, address 0x38), driven by the kernel edt_ft5x06 module.
- Device: /sys/bus/i2c/devices/11-0038 (name = "edt-ft5506").
- Input: "11-0038 generic ft5x06 (79)" -> /dev/input/eventN.
- DSI overlay: dtoverlay=vc4-kms-dsi-waveshare-800x480 (defines ts@38 and a
  "7inch-touchscreen-p" regulator GPIO chip at i2c-11/0x45).

## Root cause
The Waveshare overlay's ts@38 node is incomplete:
- it uses "reset-gpio" (singular) instead of "reset-gpios", so the edt_ft5x06
  driver does not reset the controller, and
- it defines no interrupt, so the driver polls.

The FT5506 therefore comes up in a bad state on some boots (registers but does
not report touches). A hardware reset fixes it.

The touch reset line is GPIO line 1 of the "7inch-touchscreen-p" chip
(i2c-11/0x45, rpi_touchscreen_attiny), active-low. On the official 7" panel the
driver drives it; here it does not, so we pulse it manually.

## Fix (installed on the field, committed here)
- scripts/gps-touch-rebind.sh — unbind driver, pulse the reset line, re-bind.
- config/gps-touch-rebind.service — systemd oneshot at boot (after lightdm).
- config/99-waveshare-touch.rules — udev rule to run the script on touch hotplug.

## Install on a Pi5
    sudo install -m 0755 scripts/gps-touch-rebind.sh /usr/local/bin/
    sudo install -m 0644 config/gps-touch-rebind.service /etc/systemd/system/
    sudo install -m 0644 config/99-waveshare-touch.rules /etc/udev/rules.d/
    sudo systemctl daemon-reload
    sudo systemctl enable --now gps-touch-rebind.service
    sudo udevadm control --reload-rules

## Manual revive (no reboot)
    sudo /usr/local/bin/gps-touch-rebind.sh

## Verify
    systemctl status gps-touch-rebind.service
    grep -Ei "ft5|edt" /proc/bus/input/devices
    gpioinfo 16    # line 1 = "reset", active-low
