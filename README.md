# ALERT2 Decoder

A pure-Python decoder for the **National Hydrologic Warning Council ALERT2** radio
protocol used by hydrologic sensor networks.

Built and tested against:
- ALERT2 AirLink Layer Specification v1.2 (January 2025)
- ALERT2 MANT Specification v1.2 (January 2025)
- ALERT2 Application Layer Specification v1.3

## Status

**Working:**
- AirLink Layer **FEC modes 0, 1 and 2** — full demod through MANT parsing.
  Mode 0 (k=7, rate 1/2) is validated on two sites' worth of field traffic;
  mode 2 (k=9, rate 3/4) is validated byte-exact on a recording of another
  network; mode 1 (k=9, rate 2/3) is validated by round trip only, no
  real-world mode-1 capture has been seen yet
- MANT Layer with Add Path Service (showing repeater route) and EERDS PDU ID
- **Application-layer SRP decoding** — sensor readings shown by name and value
- Live decoding from a soundcard or `rtl_fm` SDR pipeline
- WAV file decoding
- Both first-block-only and first+follow-on packets
- **Transmit side** — frame generation, WAV export, PTT keying (see below)
- **Signal quality metrics** — MER, MER_eq, MER_act, alpha, per-burst timing
- **Per-burst phase selection** — decode each burst at its own best symbol
  phase rather than at every phase
- **Per-burst symbol-rate correction** (`--rate-track`) — recovers frames
  from capture chains whose clock is ~0.1% off (some SDR recordings)

Measured in the field (two sites, 2026-08-22..29, versus a commercial
reference receiver): **100.000% recall** of relayed traffic on six of eight
consecutive days, 25,272 receptions with 2 missed, at `decode_rt` 0.058.

**Not yet implemented:**
- ALERT Concentration Protocol (Port 1 — legacy ALERT messages)
- Encrypted MANT PDUs

## Quick start

After [installation](docs/INSTALL.md):

```bash
# Decode a WAV file:
python -m alert2 capture.wav

# Decode live SDR audio:
rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 30 - | python -m alert2 --stdin

# Save to log file, CSV, JSON Lines, with daily rotation and stats:
python -m alert2 --stdin \
    --log-file logs/alert2.log \
    --csv-file logs/alert2.csv \
    --jsonl-file logs/alert2.jsonl \
    --rotate-daily \
    --stats

# Use as a library:
python3 -c "from alert2 import decode_wav; print(decode_wav('capture.wav'))"
```

> **Experimental (v0.9.0aN):** `--soft-viterbi` enables soft-decision
> Viterbi for FEC mode 0. It is OFF by default and not yet validated
> against real captures — the hard-decision decoder remains the
> production path. See [docs/SOFT_VITERBI.md](docs/SOFT_VITERBI.md)
> for the design, status, and the field A/B procedure that gates a
> default switch.

Example output:
```
[FEC0] src=769 path=[4602, 4601] port=0 hop=0 len=47 FS_hd=1
       SRP[ts=15081]: GSR(sensor_10=0, sensor_11=62.2433, Stage=7.2,
                          Battery Voltage=12.67, sensor_77=7.38)
```

The decoder names recommended sensor IDs (Stage, Battery Voltage, etc.) and
preserves administrator-assigned IDs as `sensor_NN` so you see the data even
without a hard-coded mapping.

## Recommended flags

The configuration these numbers came from:

```bash
alert2-decode --stdin --sample-rate 48000 \
    --multiphase --phase-candidates 9 --phase-select \
    --phase-block-scan --eq-retry --presync-eq \
    --candidate-gate 0.40 --max-fs-hd 3 \
    --chunk-seconds 6 --overlap-seconds 3 \
    --mer-mode full --timing-probe \
    --diag-mode --diag-file site.diag --rotate-daily
```

Notes on the less obvious ones:

- `--fec-modes 0` (the default) searches only the mode-0 framesync word.
  Add `--fec-modes 0,1,2` for a network that transmits in mode 1 or 2 — the
  three framesync words are 21-22 bits apart, so a real frame never
  triggers the wrong mode's decoder, but each extra word adds noise-hit
  attempts (about +20% CPU on a 9-phase file decode) and the k=9 trellis
  is 4x the states of k=7. Nothing else about the decode depends on the
  network; there are no site-specific filters.

- `--phase-candidates 9` is complete coverage. The winning symbol phase is
  uniformly random per burst with no memory between bursts, so there is
  nothing to predict and partial coverage is structurally lossy.
- `--phase-select` decodes each burst once at its best phase instead of at
  every phase — same packets, roughly 3x less CPU.
- `--overlap-seconds` MUST exceed the longest frame's airtime. Measure it:
  a 162-byte frame is 0.88 s, the spec maximum (1023 B) is 5.19 s. The
  default 0.6 s is too short for real traffic and silently truncates frames.
- `--candidate-gate 0.40` rejects candidates whose preamble correlation is
  too low, before the Viterbi runs. This is the one rung that can REMOVE
  packets, so it is opt-in; 0.40 sits well below the weakest real burst
  observed (0.610).
- `--rate-track` corrects per-burst symbol-rate error. A crystal-locked
  radio + sound card chain does not need it (the QTH measures <= 0.012%
  and the rung never fires), but some SDR recordings run ~0.1% off, which
  walks the sampling instant more than half a symbol across one block and
  truncates every frame no matter which phase it starts on. The rung
  measures each burst's drift from the eye-opening phase slope over the
  preamble + first block, and only when it exceeds the threshold
  re-samples the burst on the corrected grid and decodes it as well; the
  fixed-grid copy is still decoded, so it can add packets but never
  remove one. Three SDR recordings of other networks: 1 -> 5, 7 -> 12,
  1 -> 4 packets. Requires `--multiphase`. Safe to leave on.
- `--burst-mode` is **not** recommended alongside 9-phase selection. It
  contributes no unique packets there and emits partial copies that inflate
  the truncation count.

## Output formats

The decoder supports several output formats simultaneously:

- **Stdout** (default): human-readable one-line-per-packet
- **`--log-file <path>`**: timestamped human-readable log file
- **`--csv-file <path>`**: structured CSV with all packet fields
- **`--jsonl-file <path>`**: JSON Lines (use `-` for stdout)
- **`--diag-mode`** / **`--diag-file <path>`**: per-packet diagnostic log
  with RS corrections, framesync HD, byte counts, hex payloads (useful
  for analyzing marginal/truncated packets)
- **`--udp-emit HOST:PORT`**: send each packet as a JSON datagram to a
  UDP endpoint (used by the [alert2_forwarder](../alert2_forwarder/) companion
  program for Campbell ASCII IP forwarding)
- **`--stats`** / **`--stats-file <path>`**: session statistics
  (counts, distributions, source frequency, SNR per source, etc.)
- **`--web-ui`**: in-browser dashboard at http://localhost:8080

Add `--rotate-daily` for any file output to start a new file each day at
local midnight (filenames get a `-YYYY-MM-DD` suffix).

Each decoded packet includes a per-packet **SNR estimate** based on FM
quieting (uncalibrated dB, useful for trends and per-source comparison).

See [docs/OUTPUTS.md](docs/OUTPUTS.md) and
[docs/SIGNAL_QUALITY.md](docs/SIGNAL_QUALITY.md) for full details.

### Forwarding to a Campbell-compatible receiver

Use `--udp-emit` together with the separate `alert2_forwarder` program:

```bash
# Terminal 1: start the forwarder
python3 -m alert2_forwarder --dest 192.0.2.10:60015

# Terminal 2: run the decoder
sudo rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 40 - 2>/dev/null | \
  python3 -m alert2 --stdin --log-file alert2.log \
    --udp-emit 127.0.0.1:9999
```

The decoder emits decoded packets as JSON UDP; the forwarder converts
them to Campbell SCI ASCII format and maintains a persistent TCP
connection to the destination. See the `alert2_forwarder/` package for
details.

## What you need

1. **An FM receiver tuned to your ALERT2 network frequency** — typically in the
   159–174 MHz VHF band. Either:
   - An **RTL-SDR dongle** (recommended — ~$30, gives the cleanest signal)
   - A **commercial FM radio with discriminator output** (sometimes labeled
     "9600 baud data")
   - **NOT recommended:** a regular voice radio's speaker output, since
     audio-band filtering destroys parts of the FSK signal

2. **A computer** with Python 3.8+, `numpy`, `scipy`, and `libfec`.

3. **(For SDR mode):** `rtl-sdr` package providing the `rtl_fm` tool.

## Architecture

```
alert2/
├── __init__.py        # Public API
├── constants.py       # Spec values (RRC taps, sync words, etc.)
├── types.py           # Packet/header dataclasses
├── demod.py           # Audio → bit stream
├── sync.py            # Bitsync/framesync detection (vectorised)
├── fec.py             # Scrambler, RS, conv code, Viterbi
├── decoder.py         # Main pipeline (bits → packets)
├── stream.py          # Streaming decoder for live input
├── burst.py           # Burst-scoped extraction and re-demod
├── burst_detect.py    # Cross-phase burst detection and ranking
├── candidate_gate.py  # Preamble-correlation rejection before Viterbi
├── timing_probe.py    # Per-burst symbol-timing measurement
├── quality.py         # SNR, MER, MER_eq (signal quality metrics)
├── srp.py             # Application-layer Self-Reporting Protocol
├── devices.py         # ALSA capture device discovery
├── output.py          # Log/diag/CSV/JSONL/UDP emitters
├── web.py             # Read-only monitoring web UI
├── generator.py       # TRANSMIT side: frame building + PTT keying
├── txcli.py           # Transmit command line
├── txconsole.py       # Transmit web console
├── iterative.py       # Iterative decoding (experimental)
├── sova.py            # Soft-output Viterbi (disabled in field)
├── cli.py             # Command-line interface
└── __main__.py        # `python -m alert2` entry
```

The decoder pipeline matches AirLink Spec Figure 1-2:
```
audio → DC-remove → resample to 43.2 kHz → RRC matched filter
     → symbol-clock recovery → soft samples → adaptive slice → bits
     → framesync detection
     → Viterbi decode (k=7, polys 0x6D & 0x4F)
     → Reed-Solomon decode (CCSDS Blue Book §3.2)
     → MLS descramble (x^17 + x^3 + 1, preload 0x00001)
     → AirLink header parse → MANT header parse → payload
```

## Transmitting

The package includes a **transmit side** (`alert2/generator.py`) that builds
spec-compliant frames through the same `constants.py` and `fec.py` the decoder
uses, so the two cannot drift apart — `tests/test_generator_roundtrip.py`
asserts a generated frame decodes byte-exact.

It is useful for generating reference signals of exactly known content (for
receiver testing and attenuator sweeps), for querying repeater parameters, and
for bench-testing a site's configuration.

### Web console

```bash
python3 -m alert2.txconsole --app-uid bench01 --enable-tx
# then open http://127.0.0.1:8788
```

Four zones: **Status** (send an MSR Type-5 as if from a site, or query a
repeater), **Pretend** (freeform sensor value + site ID; TBRG/MSR/GSR),
**Get Explorer** (single or bundled IND parameter queries), and
**Config Change** (Set IND parameters + NV-save).

### Command line

```bash
# Build a reference WAV — no radio involved
python3 -m alert2.txcli sensor --sa 799 --reading 5=12.34 --wav /tmp/ref.wav
# ...which writes 48000 Hz by default; --tx-rate 43200 for direct decoding
# --fec-mode 1 or 2 selects the punctured k=9 codes (default 0)

# Transmit live via serial RTS keying
python3 -m alert2.txcli sensor --sa 799 --reading 5=12.34 \
    --ptt serial:/dev/ttyUSB0:rts --device 2

# Query a repeater's IND parameters
python3 -m alert2.txcli query --sa 799 --da 4601 --param 0x52 --wav /tmp/q.wav

# Standalone test tone
python3 -m alert2.txcli tone --seconds 2 --freq 2400 --wav /tmp/tone.wav
```

### Sample rate — important

Audio is **synthesised at 43200 Hz** (9 samples per symbol, which makes the
pulse shaping exact), but almost no soundcard plays 43200 natively. Output is
therefore **resampled to 48000 Hz by default** for both WAV export and live
playback.

Playing a 43200 Hz file on a 48000 Hz device without resampling stretches
every symbol by 1.111x. The result is subtle and expensive to debug: the
preamble matches for about 15 bits and then diverges, so the frame goes out
sounding strong (preamble correlation ~0.83) but never decodes (framesync
Hamming distance 4-7 instead of 0-1).

Use `--tx-rate 43200` only for a file you will decode directly rather than
play. `txcli` prints a warning if you pick a rate a soundcard is unlikely to
accept, and the web console shows one in the UI.

Subcommands: `sensor`, `query`, `raw` (`--payload` hex), `tone`.
Timing flags `--warmup/--co/--agc/--tail` expose `TxTiming`; defaults are the
field-measured AL205 values (750 ms PTT lead, 10 ms carrier-only, 55 ms AGC
tone, 5 ms tail).

### Driving a radio

The generated audio is a baseband data waveform (RRC-shaped 2-FSK at
4800 baud, energy out to about 3 kHz), not voice. It has to reach the
modulator **flat**: feed a direct/data modulation input, not the microphone
chain. A mic path adds pre-emphasis, a voice band-pass and often a limiter;
any of those will put a signal on the air that looks strong, correlates
well on the preamble and still never decodes (framesync Hamming distance
4-7). The symptom is identical to the sample-rate mistake above, so check
the input path first when a transmission refuses to decode. Deviation and
input level are secondary: set them with the `tone` subcommand against a
receiver you trust. The radio-specific detail (which input, which jumper or
menu) belongs with your radio's service notes, not here.

### Safety and authorization

Transmitting puts a signal on a shared flood-warning frequency. **You
need authorization to transmit on the frequency you use**: a license
covering that frequency and permission from the network's operator.
Use the transmit tools on a dummy load, in a shielded or cabled bench
setup, or on a network you are authorized to operate on. Never key up
on a live operational hydrologic network without the owner's consent.
A false or garbled report can feed a flood warning decision.

The tools default to being harmless:

- `txconsole` will not transmit without `--enable-tx`
- it binds to `127.0.0.1` unless told otherwise
- WAV export is the default emit; live PTT needs an explicit device
- every emit is logged with the app UID

**The Config Change zone is UNVERIFIED over the air on the AL205R.**
Bench-test it against a radio you control before using it on a live site.

## Documentation

- [Installation guide](docs/INSTALL.md)
- [Hardware setup](docs/HARDWARE.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Protocol notes](docs/PROTOCOL.md)
- [Signal quality metrics](docs/SIGNAL_QUALITY.md) — SNR, MER, MER_eq, alpha
- [Rate tracking](docs/RATE_TRACK.md) — `--rate-track`, symbol-rate error on SDR captures
- [Web UI](docs/WEB_UI.md)
- [Outputs](docs/OUTPUTS.md) — log, diag, CSV, JSONL, UDP formats

## License

Copyright (C) 2026 Nicholas Collins (K6NLC)

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the
Free Software Foundation, either version 3 of the License, or (at your
option) any later version. It is distributed WITHOUT ANY WARRANTY; see
[LICENSE](LICENSE) for details.

Every source file carries an SPDX header, and the repository passes
[`reuse lint`](https://reuse.software/). Runtime dependencies and their
licenses are listed in [NOTICE.md](NOTICE.md).

This is an independent implementation of the ALERT2 protocol, which is
specified by the National Hydrologic Warning Council. The specifications
are not included here; see https://hydrologicwarning.org/ for the
normative documents. The project is not affiliated with or endorsed by
the NHWC or any equipment vendor.
