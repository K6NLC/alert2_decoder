# Hardware Setup

This guide covers connecting an RTL-SDR (recommended) for ALERT2 reception.

## Equipment

- **An RTL-SDR dongle** — NESDR Smart, RTL-SDR Blog v3, or similar.
  Avoid the cheapest no-name dongles which lack TCXO and drift heavily.
- **An antenna** appropriate for your transmitter frequency. For VHF
  (typically 159–174 MHz), even a simple wire cut to ~17 inches works
  for nearby transmitters. For weak/distant signals, a proper dipole
  or J-pole antenna helps.
- **A USB cable** — short, good quality. Long cheap USB cables can introduce
  noise that ruins reception.

## Initial setup

### 1. Install RTL-SDR tools

See [INSTALL.md Step 5](INSTALL.md).

### 2. Verify the dongle is detected

```bash
rtl_test -t
```

You should see something like:
```
Found 1 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000001
Using device 0: Generic RTL2832U OEM
Found Rafael Micro R820T tuner
Supported gain values (29): ...
```

If you see "usb_claim_interface error", the kernel DVB driver is grabbing
your dongle. Blacklist it (see INSTALL.md).

## Tuning to your ALERT2 frequency

ALERT2 networks operate on assigned VHF frequencies, typically in the
159-174 MHz range. **You need to know your transmitter's frequency** —
this would have been configured when the network was set up.

### Step 1: See the signal in a waterfall

Use a graphical SDR tool first to confirm the signal exists at your
expected frequency. Recommendations:
- **Linux/Mac:** `gqrx`
- **Windows:** SDR# (SDRSharp)

Configure:
- Device: RTL-SDR
- Tune to your transmitter frequency (e.g., 171.850 MHz)
- Mode: NFM (narrowband FM)
- Filter width: 12.5 kHz (or "Narrow")
- Squelch: off
- Gain: start at ~30 dB

When the transmitter keys up, you should see a brief vertical stripe in the
waterfall — about 200–300 ms wide and roughly 10 kHz wide (FSK occupies more
bandwidth than a single carrier).

### Step 2: Check signal level

You want the signal **20+ dB above the noise floor** when transmitting. If
it's marginal:
- Try a better antenna or better antenna placement
- Get closer to the transmitter
- Check for nearby noise sources (computer monitors, switching power supplies)

If the signal is too strong (more than ~50 dB above noise), the dongle may
saturate. Reduce gain.

### Step 3: Note any frequency offset

RTL-SDR dongles drift. The signal might appear 1-3 kHz off from where you
tuned. Note this offset; you can correct for it with the `-p <ppm>` option
to `rtl_fm`.

For example, if your transmitter is at 171.850 MHz but it appears at 171.853 MHz
in gqrx, that's a +3 kHz error → ppm = (3000 / 171850000) × 1e6 ≈ 17.5.

For most cases, you don't need precise PPM correction — just tune slightly
off-center if needed.

## Capturing audio with rtl_fm

Once you can see the signal in gqrx, capture it from the command line:

```bash
rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 30 - 2>rtl_fm.log | \
  sox -t raw -r 48000 -e signed -b 16 -c 1 - sdr_capture.wav
```

Replace `171.850M` with your frequency.

Let it run for 60-120 seconds (long enough to capture multiple transmissions),
then Ctrl-C.

**Important `rtl_fm` flags explained:**
- `-f 171.850M` — center frequency
- `-s 48000` — sample rate (matches what the decoder expects)
- `-M fm` — FM demodulation mode
- `-E dc` — enable DC blocking (gives a cleaner baseline)
- `-g 30` — tuner gain in dB (0-49 typically; 30 is a good starting point)

## Verifying capture quality

Quick check:
```bash
sox sdr_capture.wav -n stat
```

Look for:
- **Maximum amplitude:** 0.30 to 0.70 (not 0.99 = clipping, not 0.05 = too quiet)
- **No clipping:** "Volume adjustment" should be < 1.0

If amplitude is too low, increase `-g` (try 40, then 45).
If clipping, decrease (try 25, then 20).

## Live decoding

Pipe `rtl_fm` directly into the decoder:

```bash
rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 30 - 2>/dev/null | \
  python -m alert2 --stdin
```

You'll see decoded packets print as they arrive:

```
[FEC0] src=775 port=0 hop=0 len=26 FS_hd=0 payload=...
[FEC0] src=797 port=0 hop=1 len=32 FS_hd=0 payload=...
```

To stop, press Ctrl-C.

## Common gotchas

**Signal looks like high-amplitude noise with brief low-amplitude packets**

This is normal! When `rtl_fm` is in FM mode and there's no signal, it outputs
noise at full amplitude. When a signal arrives, the FM discriminator locks
onto it and the output goes "quiet" (because the signal is now a clean tone
instead of noise). The packets are the *quiet* parts.

**Antenna placement**

Higher is better. Outdoor is better than indoor. Away from electronics is
better than near them. For testing in the same building as the transmitter,
even a basic indoor antenna works fine.

**USB cable length and quality**

Long or cheap USB cables can introduce noise. Plug the dongle directly into
the computer if possible. If using a USB extension, use a quality shielded
cable.

**Heat affects frequency**

Let the dongle warm up for 5-10 minutes before capturing — the oscillator
drifts as it heats. The NESDR Smart has a TCXO so it's relatively stable.

**Strong nearby transmitters can cause intermod**

If your Ritron's antenna is right next to the SDR's antenna and it's
transmitting at high power on a different frequency, the SDR may see "phantom"
signals at other frequencies. Reduce SDR gain or move the antennas apart.
