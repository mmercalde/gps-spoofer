# Waveshare 4.3" DSI touchscreen — intermittent touch fix

## Symptom
The Pi5 field unit's touchscreen (Waveshare 4.3" 800x480 MIPI DSI, capacitive)
sometimes works and sometimes doesn't — taps are ignored until a reboot.

## Hardware / driver
- Controller: FocalTech edt-ft5506 (FT5x06 family) on the panel's I2C bus
  (i2c-11, address 0x38), driven by the kernel edt_ft5x06 module.
- Device: /sys/bus/i2c/devices/11-0038 (name = "edt-ft5506").
- Input: "11-0038 generic ft5x06 (79)" -> /dev/input/eventN.
- DSI overlay: dtoverlay=vc4-kms-dsi-waveshare-800x480 (defines the ts@38 node).

## Root cause
The FT5506 controller occasionally comes up (or gets stuck) in a state where it
stops reporting touches. Unbinding and re-binding the I2C driver forces a clean
re-initialisation and brings it back (dmesg shows the input device re-registering).

## The previous fix was broken
/etc/udev/rules.d/99-waveshare-touch.rules existed on the field but:
1. matched ATTR{name}=="*GT911*" (Goodix) while the panel is FocalTech FT5506, and
2. wrote "echo 1" instead of the device name to unbind/bind, so it never fired.

## Fix (installed on the field, committed here)
- scripts/gps-touch-rebind.sh   — find edt-ft5506, unbind, sleep, rebind.
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
