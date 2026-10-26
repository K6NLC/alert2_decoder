# Installation Guide

This guide walks you through installing the ALERT2 decoder and its dependencies
on Linux, macOS, and Windows.

## Prerequisites

You need:
- **Python 3.8 or newer**
- **libfec** (Phil Karn's FEC library, used for Reed-Solomon decoding)
- **NumPy** and **SciPy** (signal processing)
- **(Optional)** **rtl-sdr** tools if you want to decode live SDR input
- **(Optional)** **sox** for converting audio formats

---

## Step 1: Install Python and pip

### Linux (Debian/Ubuntu)

Python 3 is usually pre-installed. Verify:

```bash
python3 --version    # Should be 3.8 or newer
pip3 --version
```

If missing:

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv
```

### Linux (Fedora/RHEL)

```bash
sudo dnf install python3 python3-pip
```

### macOS

Install Homebrew if needed (https://brew.sh), then:

```bash
brew install python3
```

### Windows

Download the installer from [python.org](https://www.python.org/downloads/).
**Important:** Check "Add Python to PATH" during installation.

Verify in PowerShell or Command Prompt:
```cmd
python --version
pip --version
```

---

## Step 2: Install libfec

This is the trickiest dependency since it's a native C library.

### Linux (Debian/Ubuntu)

```bash
sudo apt install libfec-dev
```

If that doesn't find it (some distros don't package it), build from source:

```bash
sudo apt install build-essential git
git clone https://github.com/quiet/libfec.git
cd libfec
./configure
make
sudo make install
sudo ldconfig
```

### Linux (Fedora/RHEL)

```bash
sudo dnf install fec-devel
```

If unavailable, build from source as above.

### macOS

```bash
brew install fec   # If a brew formula exists
```

If not, build from source:

```bash
brew install autoconf automake libtool
git clone https://github.com/quiet/libfec.git
cd libfec
./configure
make
sudo make install
```

You may need to add `/usr/local/lib` to your dynamic library path:
```bash
echo 'export DYLD_LIBRARY_PATH=/usr/local/lib:$DYLD_LIBRARY_PATH' >> ~/.zshrc
```

### Windows

This is the hardest part on Windows. Options in order of difficulty:

**Option A: Use WSL (recommended for Windows users)**

Install Windows Subsystem for Linux, then follow the Linux instructions inside WSL:
```powershell
wsl --install
```
After WSL is set up, open a WSL terminal and use the Debian/Ubuntu instructions above.

**Option B: Native Windows build (advanced)**

You'd need to build libfec with MSYS2 or MinGW. This is non-trivial; the WSL
route is much simpler.

### Verify libfec is installed

```bash
python3 -c "import ctypes; ctypes.CDLL('libfec.so.0'); print('OK')"
```

You should see `OK` and no error. If you get `OSError: libfec.so.0: cannot
open shared object file`, libfec is not installed or not on the library path.

---

## Step 3: Install Python dependencies

Create a virtual environment (recommended) to avoid conflicting with system
Python packages:

```bash
cd path/to/alert2_decoder
python3 -m venv venv
source venv/bin/activate    # Linux/Mac
# OR on Windows:
venv\Scripts\activate
```

Install the Python packages:

```bash
pip install numpy scipy
```

---

## Step 4: Install the ALERT2 decoder

Inside the project directory:

```bash
pip install -e .
```

The `-e` flag installs in "editable" mode so any changes you make to the source
take effect immediately.

Verify:

```bash
python -m alert2 --version
```

You should see the version number.

---

## Step 5 (optional): Install RTL-SDR tools

Only needed if you want to decode live signals from an RTL-SDR dongle.

### Linux (Debian/Ubuntu)

```bash
sudo apt install rtl-sdr sox
```

Add yourself to the `plugdev` group so you can access the dongle without root:
```bash
sudo usermod -aG plugdev $USER
```

Then **log out and back in** for the group change to take effect.

Blacklist the kernel DVB driver (otherwise the dongle gets grabbed as a TV tuner):
```bash
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf
sudo rmmod dvb_usb_rtl28xxu 2>/dev/null  # Or just reboot
```

### macOS

```bash
brew install librtlsdr sox
```

### Windows

Download `rtl_sdr.zip` from the [releases page](https://github.com/rtlsdrblog/rtl-sdr-blog/releases)
and add the extracted folder to your PATH.

You also need to install the WinUSB driver via [Zadig](https://zadig.akeo.ie):
1. Plug in the dongle
2. Run Zadig as administrator
3. Options → "List All Devices"
4. Select "Bulk-In, Interface (Interface 0)" or your RTL-SDR
5. Install WinUSB driver

### Verify RTL-SDR

```bash
rtl_test -t
```

You should see your dongle detected. If you see "usb_claim_interface error",
the kernel DVB driver is still grabbing it — see blacklist instructions above.

---

## Step 6: Test the installation

Use one of the included sample WAV files:

```bash
python -m alert2 examples/sample_capture.wav
```

You should see something like:

```
Decoded N unique packets from examples/sample_capture.wav
  [FEC0] src=775 port=0 hop=0 len=26 ...
```

If you see decoded packets, you're done!

---

## Troubleshooting

**"libfec.so.0: cannot open shared object file"**
- libfec isn't installed or isn't on the library path
- Try `sudo ldconfig` after installation
- See Step 2 above

**"No module named 'numpy'" or similar**
- Activate the virtual environment: `source venv/bin/activate`
- Or install globally: `pip install numpy scipy`

**"usb_claim_interface error -6" (RTL-SDR)**
- Kernel DVB driver is grabbing the dongle
- See "Blacklist the kernel DVB driver" in Step 5

**RTL-SDR not detected**
- Try a different USB port (avoid USB hubs)
- Check `lsusb` (Linux) or Device Manager (Windows)
- On Linux, may need to add yourself to the `plugdev` group

**Decoder finds bursts but no packets**
- Check signal level (see HARDWARE.md)
- Try `--max-fs-hd 6` for more permissive framesync matching
- Make sure you're tuned to the right frequency

**No bursts detected at all**
- Verify the frequency in `gqrx` or `SDR#`
- Check that the transmitter is actually transmitting during your capture
- Adjust SDR gain (see HARDWARE.md)
