# Waveshare 4.3" DSI touchscreen — intermittent touch fix (RESOLVED)

## Symptom
The touchscreen (Waveshare 4.3" 800x480 MIPI DSI, capacitive) worked on SOME boots
and not others. On "bad" boots the controller registered (I2C fine) but produced no
touch events at all (confirmed via evtest while tapping: zero events).

## Root cause (confirmed)
The overlay vc4-kms-dsi-waveshare-800x480 declares the touch reset as:

    reset-gpio = <&reg_display 1 1>;      // SINGULAR - wrong

but the kernel driver edt-ft5x06 reads the property "reset-gpios" (PLURAL) via
devm_gpiod_get_optional(dev, "reset", ...). So the driver NEVER reset the FocalTech
FT5506 controller, and its power-on init was a coin flip per boot.

## Fix
Rebuilt the overlay from kernel source with the typo corrected
(reset-gpio -> reset-gpios). The driver now gets the reset GPIO and does a proper
hardware reset (assert -> 5-6ms -> deassert -> 300ms) at every probe.

Installed on the field:
    /boot/firmware/overlays/vc4-kms-dsi-waveshare-800x480.dtbo        (rebuilt)
    /boot/firmware/overlays/vc4-kms-dsi-waveshare-800x480.dtbo.bak_orig  (original)

Verify after boot:
    ls /sys/bus/i2c/devices/11-0038/of_node/reset-gpios   # should now exist
    gpioinfo 16    # line 1 "reset" active-low [used] by the driver

## Rebuild (after a kernel/firmware update overwrites the .dtbo)
    mkdir -p /tmp/build && cd /tmp/build
    curl -sL https://raw.githubusercontent.com/raspberrypi/linux/rpi-6.6.y/arch/arm/boot/dts/overlays/vc4-kms-dsi-waveshare-800x480-overlay.dts -o overlay.dts
    curl -sL https://raw.githubusercontent.com/raspberrypi/linux/rpi-6.6.y/arch/arm/boot/dts/overlays/edt-ft5406.dtsi -o edt-ft5406.dtsi
    sed -i "s/reset-gpio/reset-gpios/" overlay.dts
    cpp -nostdinc -I . -undef -x assembler-with-cpp overlay.dts -o overlay.cpp.dts
    dtc -@ -I dts -O dtb overlay.cpp.dts -o vc4-kms-dsi-waveshare-800x480.dtbo
    sudo cp /boot/firmware/overlays/vc4-kms-dsi-waveshare-800x480.dtbo /boot/firmware/overlays/vc4-kms-dsi-waveshare-800x480.dtbo.bak
    sudo cp vc4-kms-dsi-waveshare-800x480.dtbo /boot/firmware/overlays/
    sudo reboot

The one-line source change is captured in config/waveshare-touch-reset-fix.patch.

Note: the earlier gps-touch-rebind.sh / udev / systemd approach (pulsing the reset
line from userspace) was a band-aid and is NOT required now that the driver resets
the controller itself. It can be left in place or removed.
