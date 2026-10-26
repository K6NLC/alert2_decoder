# Offline Installation Guide

How to install the ALERT2 decoder on a Raspberry Pi without internet
access. The bundle is built once on a Pi with internet, then carried
to other Pis via USB drive.

## Quick overview

```
Pi WITH internet                   Pi WITHOUT internet
+------------------+                +------------------+
| Build bundle on  |   USB drive    | Run install.sh   |
| this Pi (one     |==============> | from the bundle  |
| time only)       |   ~80 MB       | on this Pi       |
+------------------+                +------------------+
```

## Architecture matters

ARM processors come in two main flavors that matter here:

| Pi model | Architecture | Bundle label |
|----------|-------------|--------------|
| Pi 4, Pi 5, Pi 400 | arm64 (aarch64) | `arm64` |
| Pi 2, Pi 3, Pi 3 B+ | armv7l (armhf) | `armv7l` |
| Pi Zero, Pi 1 | armv6 (older) | not supported |

**A bundle built on a Pi 4 will not install on a Pi 2 and vice versa.**
You need to either:

- Build separate bundles on each architecture, or
- Build on a Pi that matches your target

## Step 1: Build the bundle (on a Pi with internet)

On any Pi that has internet AND has the alert2 source extracted:

```bash
cd alert2_pkg
./tools/build_offline_bundle.sh
```

Output goes to `/tmp/alert2_bundle_<arch>/` by default. You can pick
a different location:

```bash
./tools/build_offline_bundle.sh /home/pi/my_bundle
```

The script will:

1. Detect this Pi's architecture (arm64, armv7l, etc.)
2. Download rtl-sdr and its .deb dependencies
3. Download numpy/scipy Python wheels (for the Python version on this Pi)
4. Bundle the decoder source as a zip
5. Include the installer script
6. Print the output directory and size

Typical bundle size: 50-100 MB.

### Building for both Bullseye (Python 3.9) and Bookworm (Python 3.11)

The bundle downloads Python wheels for **whichever Python version this
Pi has** by default. If you want to support both Pi OS Bullseye and
Bookworm with one bundle, install both Python versions on your build
Pi first:

```bash
sudo apt install python3.9 python3.11 python3.9-distutils
```

Then re-run the bundle builder. It will detect both and include
wheels for each.

## Step 2: Copy to USB

```bash
cp -r /tmp/alert2_bundle_arm64 /media/usb/
```

Or `rsync`, or `tar | dd`, or whatever USB transfer method you prefer.
The bundle is just a directory.

## Step 3: Install on the target Pi

Plug the USB into the target Pi, then:

```bash
cd /media/usb/alert2_bundle_arm64
./install.sh
```

The installer will:

1. **Check** that this Pi's architecture matches the bundle's
2. **Check** Python version (≥ 3.8 required)
3. **Detect** any existing rtl-sdr or alert2 installations
4. **Prompt** before replacing anything that's already installed
5. **Install** system packages from `packages/*.deb`
6. **Install** Python packages from `python_wheels/*.whl`
7. **Install** the alert2 package to `~/alert2_install/`
8. **Offer** to set up udev rules, plugdev group membership, and
   blacklist the kernel DVB driver (all prompted, all optional)
9. **Verify** the install works
10. **Self-test** with a bundled WAV file (if included)

You'll see prompts like:

```
rtl-sdr is already installed. Replace it with bundled version? [y/N]
```

The defaults (in capital letters) are usually the safe choice.

## Step 4: Verify

After install:

```bash
python3 -m alert2 --version
# Should print: alert2 0.8.2

rtl_fm 2>&1 | head -1
# Should print rtl_fm version info
```

## Step 5: Start decoding

```bash
sudo rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 30 - 2>/dev/null | \
  python3 -m alert2 --stdin --log-file alert2.log --web-ui
```

Then open `http://localhost:8080` in a browser on the same network.

## Troubleshooting

**"This bundle was built for X but this Pi is Y"**

You built the bundle on a different architecture than this Pi.
Rebuild on a matching Pi, or use a different bundle.

**"python3-pip is not installed"**

`sudo apt install python3-pip` on the target Pi. Yes, that requires
internet on the target — pip is a bootstrap dependency we can't easily
ship in the bundle. If your target Pi truly has zero internet ever,
you may need to also bundle `python3-pip*.deb` manually.

**"Some packages failed to install"**

The dpkg dependency chain might be incomplete. Try:

```bash
sudo apt-get install -f --no-download
sudo dpkg -i packages/*.deb
```

If that still fails, the bundle is missing a required system library
that's specific to your OS version. Build a fresh bundle on a Pi
running the same OS version as the target.

**"rtl_fm: command not found" after install**

The `rtl-sdr` package didn't install correctly. Check:

```bash
ls /usr/bin/rtl_*
dpkg -l | grep rtl
```

If `rtl-sdr` doesn't appear in the dpkg list, re-run:

```bash
sudo dpkg -i packages/rtl-sdr_*.deb
```

**rtl_fm fails with "Failed to open rtlsdr device"**

The kernel's DVB driver has claimed the device. Run the install.sh
again and answer "Y" to blacklist the kernel driver, then reboot.
Or do it manually:

```bash
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf
sudo rmmod dvb_usb_rtl28xxu
```

**Web UI is unreachable**

By default the web UI binds to `127.0.0.1` (only reachable from the Pi
itself). To make it reachable from your laptop on the LAN:

```bash
python3 -m alert2 --stdin --web-ui --web-ui-bind 0.0.0.0
```

## Uninstalling

If `uninstall.sh` is included in the bundle:

```bash
./uninstall.sh
```

Or manually:

```bash
python3 -m pip uninstall alert2 --break-system-packages
sudo apt-get remove rtl-sdr librtlsdr0
sudo rm /etc/udev/rules.d/20-rtlsdr.rules
sudo rm /etc/modprobe.d/blacklist-rtl.conf
rm -rf ~/alert2_install
```
