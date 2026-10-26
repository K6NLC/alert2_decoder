# RTL-SDR Setup for ALERT2 Decoding

Quick reference for getting your RTL-SDR working on a Pi for ALERT2.

## Hardware

Tested with NooElec NESDR (R820T2 tuner). Other RTL-SDR dongles using
the R820T or R820T2 tuner should work the same way.

## Frequency

Hydrologic ALERT2 networks typically operate at one of these
frequencies:

- **171.850 MHz** — common in California (where K6NLC operates)
- Other regions may use different frequencies; check with your
  network operator

`rtl_fm` doesn't tune well exactly on-frequency due to DC spike,
so we tune offset and let the demodulator find the actual signal:

```bash
rtl_fm -f 171.850M
```

Recent versions handle DC offset internally with `-E dc`, which is
what the example commands use.

## Gain

The R820T2 has discrete gain steps. Useful values:

| Setting | Effect |
|---------|--------|
| `-g 30` | Conservative; works in moderate-noise environments |
| `-g 40` | Default recommendation; balances signal/noise |
| `-g 43` | Slight boost; may help in weak-signal locations |
| `-g 46+` | Risk of intermodulation distortion; test carefully |

Set with `-g <value>` (rtl_fm rounds to nearest available step).
Tune empirically — what works at one location may not at another.

## Sample rate

`-s 48000` is what the decoder expects. The decoder will resample
internally if needed, but 48k matches its preferred input format
and minimizes resampling.

## Modulation

`-M fm` (NBFM) is correct for ALERT2. The signal is 4800-baud FSK
modulated onto an FM carrier.

## Putting it together

```bash
sudo rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 40 - 2>/dev/null | \
  python3 -m alert2 --stdin --log-file alert2.log --web-ui
```

- `sudo` may not be needed if you've installed udev rules and joined plugdev
- Output is piped as raw int16 PCM to the decoder
- `--log-file` writes decoded packets to a file
- `--web-ui` starts the browser UI at `http://localhost:8080`

## Permissions

By default, rtl_fm requires root access to the USB device. Two ways
to fix that:

### Option A: udev rules (recommended)

```bash
sudo tee /etc/udev/rules.d/20-rtlsdr.rules > /dev/null << EOF
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG plugdev $USER
# log out and back in
```

Then `rtl_fm` works without sudo.

### Option B: stay with sudo

Less clean but works. Just keep using `sudo rtl_fm ...`.

## Kernel driver conflict

The mainline kernel has a DVB driver (`dvb_usb_rtl28xxu`) that
claims the RTL-SDR before `rtl_fm` can use it. You'll see errors like:

```
Failed to open rtlsdr device #0
```

Fix: blacklist the kernel driver:

```bash
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf
sudo rmmod dvb_usb_rtl28xxu  # if currently loaded
```

Survives reboots once the blacklist file is in place.

The bundled `install.sh` offers to do all of this for you.

## Antenna

ALERT2 at 171.85 MHz is in the lower VHF band. Wavelength ~1.74 m,
so quarter-wave is ~43 cm.

- **Indoor whip**: works for nearby strong sources (a few km)
- **External vertical** (J-pole, ground plane): much better
- **Yagi/directional**: best for distant or weak sources, but requires
  pointing at a known transmitter

Higher is better. Even a few meters of height can dramatically
improve SNR over an indoor antenna.

## Diagnostics

To see RTL-SDR signal quality without the decoder:

```bash
sudo apt install rtl-sdr-tools
rtl_test -t        # tests the device
rtl_power -f 171.840M:171.860M:1k -g 40 -i 60 noise.csv  # noise floor sweep
```

`rtl_power` is particularly useful for finding interference: it'll
show you the noise floor across the frequency range over time.
