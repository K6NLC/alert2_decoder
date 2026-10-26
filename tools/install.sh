#!/bin/bash
# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later
#
# install.sh — defensive installer for the ALERT2 decoder bundle.
#
# This script is meant to be run from inside the bundle directory:
#   cd alert2_bundle_<arch>
#   ./install.sh
#
# It will:
#   1. Check architecture, OS, Python version
#   2. Detect and confirm existing installations before changing them
#   3. Install rtl-sdr and dependencies from local .deb packages
#   4. Install numpy/scipy from local Python wheels
#   5. Install the alert2 Python package
#   6. Optionally set up udev rules for non-root RTL-SDR access
#   7. Run a self-test on a bundled WAV file

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# ---------- Helpers ----------
yes_no() {
    # Usage: yes_no "Prompt text?" default
    # Returns 0 for yes, 1 for no
    local prompt="$1"
    local default="${2:-N}"
    local hint="[y/N]"
    [ "$default" = "Y" ] && hint="[Y/n]"
    echo -n "$prompt $hint "
    read -r reply
    [ -z "$reply" ] && reply="$default"
    case "$reply" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

separator() {
    echo ""
    echo "------------------------------------------------------------"
}

# ---------- Phase 1: pre-flight checks ----------
echo "============================================================"
echo "ALERT2 Decoder — Offline Installer"
echo "============================================================"

# Check arch
if [ -f arch.txt ]; then
    BUNDLE_ARCH=$(cat arch.txt)
    HOST_ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
    case "$HOST_ARCH" in
        aarch64) HOST_ARCH=arm64 ;;
        armv7l|armhf) HOST_ARCH=armv7l ;;
        x86_64) HOST_ARCH=amd64 ;;
    esac
    if [ "$BUNDLE_ARCH" != "$HOST_ARCH" ]; then
        echo "ERROR: This bundle was built for $BUNDLE_ARCH but this Pi is $HOST_ARCH."
        echo "       Get a bundle built on the same architecture as this Pi,"
        echo "       or run build_offline_bundle.sh on a $HOST_ARCH machine."
        exit 1
    fi
    echo "Architecture check: $HOST_ARCH ✓"
fi

# Check Python
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is not installed."
    echo "       Install it first (apt-get install python3) and re-run."
    exit 1
fi
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || [ "$PY_MINOR" -lt 8 ]; then
    echo "ERROR: Python 3.8+ required (you have $PY_VERSION)."
    exit 1
fi
echo "Python version: $PY_VERSION ✓"

# Check pip
if ! python3 -m pip --version >/dev/null 2>&1; then
    echo "ERROR: python3-pip is not installed."
    echo "       Install it (apt-get install python3-pip) and re-run."
    exit 1
fi
echo "pip available: ✓"

# Check sudo (we'll need it for apt and udev)
if ! command -v sudo >/dev/null 2>&1; then
    echo "WARNING: sudo not found. Script may fail at install steps."
    echo "         Continue anyway? Press Ctrl-C to abort."
    sleep 2
fi

# ---------- Phase 2: detect existing installations ----------
separator
echo "Checking for existing installations..."

RTLSDR_INSTALLED=no
if command -v rtl_fm >/dev/null 2>&1; then
    RTL_VERSION=$(rtl_fm 2>&1 | head -3 | grep -i version || echo "unknown version")
    echo "  rtl-sdr is already installed ($RTL_VERSION)"
    RTLSDR_INSTALLED=yes
fi

ALERT2_INSTALLED=no
ALERT2_VERSION=
if python3 -c "import alert2" 2>/dev/null; then
    ALERT2_VERSION=$(python3 -c "from alert2 import __version__; print(__version__)" 2>/dev/null || echo "unknown")
    echo "  alert2 v$ALERT2_VERSION is already installed"
    ALERT2_INSTALLED=yes
fi

# ---------- Phase 3: install system packages ----------
# Two categories of .deb packages in this bundle:
#   - rtl-sdr family (rtl-sdr, librtlsdr0, libusb-1.0-0) — RF capture stack
#   - Python/decoder dependencies (python3-numpy, python3-scipy,
#     libopenblas0, libgfortran5, libfec-dev) — needed regardless
#
# We only prompt about replacing rtl-sdr if it's already installed.
# The Python/decoder deps are always installed because skipping them
# means alert2 itself won't import (which was the v0.8.2/v0.8.3 install
# trap on a Pi 2 test install).
separator
INSTALL_RTL=yes
if [ "$RTLSDR_INSTALLED" = "yes" ]; then
    if ! yes_no "rtl-sdr is already installed. Replace it with bundled version?" "N"; then
        INSTALL_RTL=no
        echo "Skipping rtl-sdr replacement (keeping existing)."
    fi
fi

if [ ! -d packages ] || [ -z "$(ls -A packages/*.deb 2>/dev/null)" ]; then
    echo "ERROR: No .deb packages found in packages/."
    echo "       This bundle may be incomplete."
    exit 1
fi

if [ "$INSTALL_RTL" = "yes" ]; then
    echo "Installing all system packages from bundle..."
    sudo dpkg -i packages/*.deb || {
        echo ""
        echo "Some packages failed to install. Trying to fix dependencies..."
        sudo apt-get install -f --no-download || true
        sudo dpkg -i packages/*.deb
    }
else
    echo "Installing Python/decoder dependencies only (skipping rtl-sdr)..."
    # Install everything EXCEPT the rtl-sdr family
    TO_INSTALL=$(ls packages/*.deb 2>/dev/null | \
        grep -v -E '/(rtl-sdr|librtlsdr|libusb-1\.0)_' || true)
    if [ -n "$TO_INSTALL" ]; then
        sudo dpkg -i $TO_INSTALL || {
            sudo apt-get install -f --no-download || true
            sudo dpkg -i $TO_INSTALL
        }
    fi
fi
echo "System packages installed."

# ---------- Phase 4: verify Python dependencies ----------
separator
echo "Checking Python dependencies (numpy, scipy)..."

# Check for stale pip-installed numpy/scipy that might shadow the
# apt-installed versions. This broke an early test install:
# leftover piwheels scipy 1.17.1 (broken) was found by Python before
# the apt scipy 1.10.x. Two locations to check:
#   ~/.local/lib/python*/site-packages/  (user-site)
#   /usr/local/lib/python*/dist-packages/  (pip install --user as root)
STALE_FOUND=no
for loc in "$HOME/.local/lib"/python*/site-packages \
           /usr/local/lib/python*/dist-packages; do
    for pkg in numpy scipy; do
        if [ -d "$loc/$pkg" ] || [ -d "$loc/${pkg}-"*".dist-info" ]; then
            echo "  WARNING: stale pip-installed $pkg detected in $loc"
            STALE_FOUND=yes
        fi
    done
done

if [ "$STALE_FOUND" = "yes" ]; then
    echo ""
    if yes_no "Remove these stale pip installs (recommended)?" "Y"; then
        pip3 uninstall numpy scipy -y --break-system-packages 2>/dev/null || true
        sudo pip3 uninstall numpy scipy -y --break-system-packages 2>/dev/null || true
        # Manually scrub leftover dirs that pip uninstall sometimes misses
        for loc in "$HOME/.local/lib"/python*/site-packages \
                   /usr/local/lib/python*/dist-packages; do
            for pkg in numpy scipy; do
                if [ -d "$loc/$pkg" ]; then
                    echo "  Removing $loc/$pkg"
                    sudo rm -rf "$loc/$pkg"*
                fi
            done
        done
        echo "Cleanup done."
    fi
fi

# In v0.8.3+, numpy and scipy are installed via python3-numpy and
# python3-scipy .deb packages that were already installed in Phase 3.
# This is more reliable than piwheels.
if python3 -c "import numpy, scipy" 2>/dev/null; then
    NUMPY_V=$(python3 -c "import numpy; print(numpy.__version__)")
    SCIPY_V=$(python3 -c "import scipy; print(scipy.__version__)")
    echo "  numpy $NUMPY_V — ok"
    echo "  scipy $SCIPY_V — ok"
else
    echo "ERROR: numpy or scipy not importable after install."
    echo "       Try: sudo apt install python3-numpy python3-scipy libopenblas0 libgfortran5"
    echo "       Or check the install logs above for which .deb failed."
    exit 1
fi

# Verify libfec is installed (Reed-Solomon decoder needs it)
if ! ldconfig -p | grep -q libfec; then
    echo "ERROR: libfec not found. Installing now from bundle..."
    sudo dpkg -i packages/libfec-dev_*.deb 2>/dev/null || \
        sudo apt install -y libfec-dev
    if ! ldconfig -p | grep -q libfec; then
        echo "ERROR: libfec install failed. Try: sudo apt install libfec-dev"
        exit 1
    fi
fi
echo "  libfec — ok"

# ---------- Phase 5: install alert2 package ----------
separator
ZIPFILE=$(ls alert2_decoder_v*.zip 2>/dev/null | head -1)
if [ -z "$ZIPFILE" ]; then
    echo "ERROR: No alert2_decoder_v*.zip found in bundle."
    exit 1
fi

NEW_VERSION=$(echo "$ZIPFILE" | sed 's/alert2_decoder_v\(.*\)\.zip/\1/')
echo "Bundle contains alert2 v$NEW_VERSION"

if [ "$ALERT2_INSTALLED" = "yes" ]; then
    if ! yes_no "alert2 v$ALERT2_VERSION is currently installed. Replace with v$NEW_VERSION?" "Y"; then
        echo "Skipping alert2 installation."
        SKIP_ALERT2=yes
    fi
fi

if [ "${SKIP_ALERT2:-no}" = "no" ]; then
    INSTALL_DIR="$HOME/alert2_install"
    if [ -d "$INSTALL_DIR" ]; then
        echo "Removing previous extraction at $INSTALL_DIR"
        rm -rf "$INSTALL_DIR"
    fi
    mkdir -p "$INSTALL_DIR"
    echo "Extracting $ZIPFILE to $INSTALL_DIR..."
    unzip -q "$ZIPFILE" -d "$INSTALL_DIR"
    cd "$INSTALL_DIR/alert2_pkg"

    # numpy/scipy already satisfied via apt (python3-numpy/python3-scipy debs).
    # We pass --no-deps to avoid pip trying to reach the internet for them.
    PIP_FLAGS="--no-deps"
    if python3 -m pip install --help 2>&1 | grep -q "break-system-packages"; then
        PIP_FLAGS="$PIP_FLAGS --break-system-packages"
    fi
    python3 -m pip install $PIP_FLAGS -e .
    cd "$SCRIPT_DIR"
    echo "alert2 v$NEW_VERSION installed."
fi

# ---------- Phase 6: post-install setup (optional, prompted) ----------
separator
echo "Post-install setup (all optional):"
echo ""

# udev rules for RTL-SDR
if [ ! -f /etc/udev/rules.d/20-rtlsdr.rules ] && [ ! -f /lib/udev/rules.d/20-rtlsdr.rules ]; then
    if yes_no "Install udev rules so rtl_fm works without sudo?" "Y"; then
        sudo tee /etc/udev/rules.d/20-rtlsdr.rules > /dev/null << 'UDEVEOF'
# RTL-SDR
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666", SYMLINK+="rtl_sdr"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666", SYMLINK+="rtl_sdr"
UDEVEOF
        sudo udevadm control --reload-rules
        sudo udevadm trigger
        echo "udev rules installed."
    fi
else
    echo "udev rules for RTL-SDR already present — skipping."
fi

# plugdev group
if ! groups | grep -q plugdev; then
    if yes_no "Add user '$USER' to plugdev group (lets you use rtl_fm without sudo)?" "Y"; then
        sudo usermod -aG plugdev "$USER"
        echo "Added to plugdev group."
        echo "NOTE: log out and back in for this to take effect."
    fi
else
    echo "User '$USER' already in plugdev group."
fi

# Blacklist kernel DVB driver (otherwise it claims the RTL-SDR)
BLACKLIST_FILE=/etc/modprobe.d/blacklist-rtl.conf
if [ ! -f "$BLACKLIST_FILE" ]; then
    if yes_no "Blacklist kernel DVB driver (recommended — otherwise rtl_fm may fail)?" "Y"; then
        sudo tee "$BLACKLIST_FILE" > /dev/null << 'BLACKEOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
BLACKEOF
        echo "Kernel DVB driver blacklisted. Unload now? (else takes effect on reboot)"
        if yes_no "Unload now?" "N"; then
            sudo rmmod dvb_usb_rtl28xxu 2>/dev/null || true
            sudo rmmod rtl2832 2>/dev/null || true
            sudo rmmod rtl2830 2>/dev/null || true
        fi
    fi
else
    echo "DVB driver already blacklisted — skipping."
fi

# ---------- Phase 7: verify ----------
separator
echo "Verifying installation..."

if python3 -c "from alert2 import __version__; print(f'Module import OK: alert2 v{__version__}')"; then
    INSTALL_OK=yes
else
    echo "ERROR: alert2 module fails to import."
    INSTALL_OK=no
fi

if python3 -m alert2 --version 2>/dev/null; then
    echo "CLI command OK"
else
    echo "WARNING: 'python3 -m alert2 --version' failed."
    INSTALL_OK=no
fi

if command -v rtl_fm >/dev/null 2>&1; then
    echo "rtl_fm binary OK: $(which rtl_fm)"
else
    echo "WARNING: rtl_fm not in PATH. RTL-SDR streaming will not work."
fi

# Self-test if WAV file is bundled
SELFTEST_WAV="$SCRIPT_DIR/sample_capture.wav"
if [ -f "$SELFTEST_WAV" ]; then
    echo ""
    echo "Running self-test on bundled WAV..."
    if python3 -m alert2 "$SELFTEST_WAV" --no-stats --quiet 2>&1 | head -5; then
        echo "Self-test ran without errors."
    else
        echo "WARNING: Self-test failed."
    fi
fi

separator
if [ "$INSTALL_OK" = "yes" ]; then
    cat << 'OKEOF'
Installation complete!

Quick start:
  # Decode from RTL-SDR (streaming):
  sudo rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 30 - 2>/dev/null | \
    python3 -m alert2 --stdin --log-file alert2.log --web-ui

  # Then open http://localhost:8080 in a browser.

  # Decode a WAV file:
  python3 -m alert2 some_capture.wav

For full docs, see:
  ~/alert2_install/alert2_pkg/README.md
  ~/alert2_install/alert2_pkg/docs/WEB_UI.md
OKEOF
else
    echo "Installation had problems. Check the messages above."
    exit 1
fi
