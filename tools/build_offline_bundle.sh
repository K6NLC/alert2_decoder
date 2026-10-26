#!/bin/bash
# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later
#
# build_offline_bundle.sh — create a self-contained installer bundle
#
# Run this on a Pi (with internet) to produce a USB-portable bundle that
# can install the ALERT2 decoder on another Pi of the same architecture
# without internet access.
#
# Usage:
#   cd alert2_pkg
#   ./tools/build_offline_bundle.sh [output_dir]
#
# The output bundle will be in /tmp/alert2_bundle_<arch>/ unless you
# specify a different output_dir.
#
# Architecture: the bundle targets whatever architecture this script
# is run on (arm64 for Pi 4/5, armv7l for Pi 2/3, etc).
# Build the bundle ON the target architecture (or one compatible with it).

set -e

# ---------- Find ourselves ----------
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PKG_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PKG_ROOT"

# ---------- Determine architecture ----------
ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
case "$ARCH" in
    arm64|aarch64)  ARCH_LABEL="arm64" ;;
    armhf|armv7l)   ARCH_LABEL="armv7l" ;;
    amd64|x86_64)   ARCH_LABEL="amd64" ;;
    *)              ARCH_LABEL="$ARCH" ;;
esac
echo "Building offline bundle for architecture: $ARCH_LABEL"

# ---------- Decide output location ----------
OUTPUT_DIR="${1:-/tmp/alert2_bundle_${ARCH_LABEL}}"
echo "Output directory: $OUTPUT_DIR"

if [ -d "$OUTPUT_DIR" ]; then
    echo "Output directory already exists. Remove it? [y/N]"
    read -r confirm
    if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
        rm -rf "$OUTPUT_DIR"
    else
        echo "Aborting."
        exit 1
    fi
fi
mkdir -p "$OUTPUT_DIR"/{packages,python_wheels,docs}

# ---------- Record build environment for verification ----------
{
    echo "ALERT2 decoder offline bundle"
    echo "Built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "Architecture: $ARCH_LABEL"
    echo "Built on: $(uname -a)"
    if [ -f /etc/os-release ]; then
        echo "OS: $(grep PRETTY_NAME /etc/os-release | cut -d= -f2 | tr -d '\"')"
    fi
    PYV=$(python3 --version 2>&1)
    echo "Python: $PYV"
} > "$OUTPUT_DIR/build_info.txt"
echo "$ARCH_LABEL" > "$OUTPUT_DIR/arch.txt"

# ---------- Download .deb packages ----------
echo ""
echo "==> Downloading system packages (.deb)..."
cd "$OUTPUT_DIR/packages"

# rtl-sdr and its dependencies
# --recurse pulls transitive deps; filters keep it minimal
PKGS=$(apt-cache depends rtl-sdr --recurse --no-recommends --no-suggests \
       --no-conflicts --no-breaks --no-replaces --no-enhances 2>/dev/null \
       | grep "^\w" | sort -u)

if [ -z "$PKGS" ]; then
    echo "WARNING: apt-cache could not resolve rtl-sdr dependencies."
    echo "         Falling back to minimal list."
    PKGS="rtl-sdr librtlsdr0 libusb-1.0-0"
fi

# Also include numpy, scipy, and their runtime libs as system packages.
# This is more reliable than piwheels (which has had broken wheels for
# armv7l Python 3.11 scipy). The apt versions are built directly by
# Debian and Just Work.
EXTRA_PKGS="python3-numpy python3-scipy libopenblas0 libgfortran5 libfec-dev"
echo "Adding: $EXTRA_PKGS"
for p in $EXTRA_PKGS; do
    # Recurse on each to pull its deps too
    DEPS=$(apt-cache depends "$p" --recurse --no-recommends --no-suggests \
           --no-conflicts --no-breaks --no-replaces --no-enhances 2>/dev/null \
           | grep "^\w" | sort -u)
    PKGS="$PKGS"$'\n'"$DEPS"
done
PKGS=$(echo "$PKGS" | sort -u)

# Download each package; ignore failures for virtual packages
echo "$PKGS" | while read pkg; do
    apt-get download "$pkg" 2>/dev/null || echo "  (skipped virtual: $pkg)"
done

DEB_COUNT=$(ls -1 *.deb 2>/dev/null | wc -l)
echo "Downloaded $DEB_COUNT .deb packages."

# ---------- Python dependencies ----------
# We no longer download numpy/scipy as pip wheels — instead they're
# included in the .deb packages section above (python3-numpy, python3-scipy).
# This is more reliable than piwheels (broken armv7l/Py3.11 scipy 1.17.1)
# and the apt versions bring all their runtime deps automatically.
#
# The python_wheels/ directory is kept (empty) for backward compatibility
# with older install.sh scripts that look for it.
cd "$OUTPUT_DIR/python_wheels"
touch .placeholder

# ---------- Copy the decoder source ----------
echo ""
echo "==> Bundling the decoder source..."
cd "$PKG_ROOT"
# Get version
# Get version by parsing __init__.py directly (avoids importing the package,
# which fails when its dependencies like scipy aren't installed yet)
VERSION=$(grep -E "^__version__\s*=" alert2/__init__.py | head -1 | sed -E "s/.*=\s*['\"]([^'\"]+)['\"].*/\1/")
if [ -z "$VERSION" ]; then
    echo "ERROR: Could not parse __version__ from alert2/__init__.py"
    exit 1
fi
echo "Bundling decoder source for v$VERSION..."
ZIPNAME="alert2_decoder_v${VERSION}.zip"
(cd .. && zip -rq "$OUTPUT_DIR/$ZIPNAME" "$(basename $PKG_ROOT)" \
    -x "*/__pycache__/*" "*.pyc" "*/tools/build_offline_bundle.sh")
echo "Wrote $ZIPNAME"

# ---------- Include the install script ----------
cp "$SCRIPT_DIR/install.sh" "$OUTPUT_DIR/install.sh"
chmod +x "$OUTPUT_DIR/install.sh"

# Optionally include uninstall.sh if present
if [ -f "$SCRIPT_DIR/uninstall.sh" ]; then
    cp "$SCRIPT_DIR/uninstall.sh" "$OUTPUT_DIR/uninstall.sh"
    chmod +x "$OUTPUT_DIR/uninstall.sh"
fi

# ---------- Include docs ----------
if [ -d "$PKG_ROOT/docs" ]; then
    cp "$PKG_ROOT/docs/"*.md "$OUTPUT_DIR/docs/" 2>/dev/null || true
fi

# Top-level README in the bundle (different from the package's README)
cat > "$OUTPUT_DIR/README.md" << READMEEOF
# ALERT2 Decoder Offline Bundle (v${VERSION})

Architecture: **${ARCH_LABEL}**

This bundle was built on $(date -u +%Y-%m-%dT%H:%M:%SZ).

## To install

\`\`\`bash
cd $(basename "$OUTPUT_DIR")
./install.sh
\`\`\`

The installer will prompt before changing anything on your system.

See \`docs/OFFLINE_INSTALL.md\` for full documentation.

## What's in this bundle

- \`packages/\` — system packages (.deb files for rtl-sdr and deps)
- \`python_wheels/\` — Python packages (numpy, scipy)
- \`${ZIPNAME}\` — the decoder source
- \`install.sh\` — defensive installer (prompts before changes)
- \`docs/\` — additional documentation

## Compatibility

Built for architecture: **$ARCH_LABEL**

This bundle will work on:
- Same architecture Pi (Pi 4 → Pi 4, Pi 2 → Pi 2/3)
- Pi OS Bullseye (Python 3.9) or Bookworm (Python 3.11)

It will NOT work on:
- Different architecture (e.g. amd64 desktop)
- Pi Zero (which is armv6 — older than this bundle's target)
READMEEOF

# ---------- Done ----------
TOTAL_SIZE=$(du -sh "$OUTPUT_DIR" | cut -f1)
echo ""
echo "============================================================"
echo "Bundle built successfully."
echo "  Output: $OUTPUT_DIR"
echo "  Size:   $TOTAL_SIZE"
echo "  Arch:   $ARCH_LABEL"
echo ""
echo "Copy the entire directory to a USB drive:"
echo "  cp -r $OUTPUT_DIR /media/usb/  # or wherever your USB is mounted"
echo ""
echo "To install from the USB on a target Pi:"
echo "  cd /path/to/bundle"
echo "  ./install.sh"
echo "============================================================"
