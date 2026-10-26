# Troubleshooting

## Decoder issues

### "Decoded 0 unique packets"

The decoder didn't find anything decodable in the input. Check in this order:

**1. Is there actually a signal in the file?**

Look at the file in a waveform viewer (Audacity, etc.) or via:
```bash
sox capture.wav -n stat
```

If the maximum amplitude is < 0.05, your signal is too weak.
If there are no obvious bursts, no transmissions occurred during your capture.

**2. Was the signal captured cleanly?**

Open the file in a spectrogram viewer (Audacity → Analyze → Plot Spectrum, or
gqrx → File source). During a transmission burst, you should see:
- A short "AGC tone" at ~2400 Hz at the start of the burst (about 30 ms)
- Then varying frequency content from ~1000-3000 Hz during data

If the AGC tone is missing or the frequencies look very different, the audio
path may be too narrowband (e.g., speaker output of a normal voice radio).
Use SDR or a discriminator output instead.

**3. Is it the right protocol?**

ALERT2 isn't the only protocol on VHF data frequencies. Make sure you're
listening to an actual ALERT2 transmitter and not, say, a different vendor's
hydrologic system or a paging signal.

**4. Try more permissive matching**

```bash
python -m alert2 capture.wav --max-fs-hd 7
```

This allows up to 7 bit errors in the framesync (default is 5). If still no
decode, the signal is likely too marginal.

### "Decoded N packets, but they don't match the log"

If you have a reference decoder (Campbell AL205 or similar) producing a log,
and the packets we decode don't match:

**Check time alignment:** the log might be from a different period than your
capture. Are timestamps consistent?

**Check decoder fields:** our decoder outputs `src`, `port`, `hop`, `len`, etc.
A mismatch in any of these vs. the reference suggests a parsing issue we should
fix.

### Decoder crashes or hangs

If `python -m alert2` hangs (no output for many seconds on a small file):
- Likely a bug in our decoder. Open an issue with:
  - The exact command you ran
  - A small WAV file that triggers the hang
  - Python version (`python --version`)
  - OS

If it crashes with a stack trace:
- Capture the trace and the input file
- Try with `--verbose` for more detail

## SDR issues

### "usb_claim_interface error -6"

The kernel DVB driver is using the dongle. Blacklist it:

```bash
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf
sudo rmmod dvb_usb_rtl28xxu
```

If `rmmod` fails, just reboot.

### Signal appears at wrong frequency

RTL-SDR dongles drift. Use `rtl_fm -p <ppm>` to correct.

To measure PPM precisely, use `kal`:
```bash
kal -s GSM900   # Or another scan target
```
But for ALERT2 work, eyeballing the offset in `gqrx` is usually fine.

### "rtl_fm: not found"

You haven't installed the rtl-sdr package. See [INSTALL.md Step 5](INSTALL.md).

### Decoder finds bursts but RS always fails

Common causes:

**Gain too high:** the SDR is saturating. Try lower gain (`-g 20`).

**Gain too low:** the signal is too weak. Try higher gain (`-g 40` or higher).

**Wrong frequency:** the signal is partially out of the FM filter bandwidth.
Verify the signal is centered in `gqrx`.

**Antenna issue:** weak signal isn't getting through. Try a better antenna or
move closer to the transmitter.

## libfec issues

### "OSError: libfec.so.0: cannot open shared object file"

libfec isn't installed or isn't on the library path.

**Check if it's installed:**
```bash
ldconfig -p | grep libfec
```

If it shows nothing, install it (see [INSTALL.md Step 2](INSTALL.md)).

If it's listed but Python still can't find it:
```bash
sudo ldconfig
```

If you built it from source and it's in `/usr/local/lib`:
```bash
echo '/usr/local/lib' | sudo tee /etc/ld.so.conf.d/local.conf
sudo ldconfig
```

### libfec on macOS

macOS uses `.dylib` not `.so`. The decoder tries both. If it can't find the
library, set `DYLD_LIBRARY_PATH`:

```bash
export DYLD_LIBRARY_PATH=/usr/local/lib:$DYLD_LIBRARY_PATH
```

Add this to your `~/.zshrc` to make it permanent.

## Performance issues

### Decoder is slow

The pure-Python Viterbi decoder runs at maybe 100 packets/sec on a typical
laptop, which is more than fast enough for live decoding (ALERT2 networks
typically have one packet every few seconds at most).

If you're trying to batch-process many WAV files, you can parallelize:

```bash
ls *.wav | parallel python -m alert2 {} > {.}.decoded.txt
```

### Live mode falls behind

If `rtl_fm | python -m alert2 --stdin` falls behind real-time, the decoder
is too slow for your CPU. Try:
- Reducing the chunk size in stream.py
- Raising the framesync HD threshold (more permissive = more candidate decodes)

For most modern computers, real-time decoding is not an issue.

## Offline bundle install problems

### "This bundle was built for X but this Pi is Y"

You built the bundle on a Pi with a different architecture. Pi 4/5 use
arm64; Pi 2/3 use armv7l. Build a bundle on a Pi matching your target.

### "python3-pip is not installed" during install

The target Pi needs pip itself before we can install Python packages.
Either:
- Get internet on the target temporarily: `sudo apt install python3-pip`
- Or download `python3-pip*.deb` from a working Pi:
  `apt-get download python3-pip` and copy it to the target.

### "Some packages failed to install"

The dpkg dependency chain is incomplete. Try:
```bash
sudo apt-get install -f --no-download
sudo dpkg -i packages/*.deb
```

If still failing, build a fresh bundle on a Pi running the same OS
version as the target.

### "rtl_fm: command not found" after install

The `rtl-sdr` package didn't install correctly. Re-run:
```bash
sudo dpkg -i packages/rtl-sdr_*.deb
```

