# Signal Quality (SNR) Measurement

The decoder includes a signal-to-noise ratio (SNR) measurement for each
decoded packet. This is useful for monitoring link quality, comparing sites,
and diagnosing reception problems.

## How it works

When using an SDR with FM mode (e.g. `rtl_fm -M fm`), the audio output has a
distinctive property called **FM quieting**:

- **Idle (no signal)**: The FM discriminator outputs full-amplitude noise.
  Audio envelope is high.
- **Signal present**: The FM discriminator locks onto the carrier, and the
  output envelope drops dramatically.

The decoder takes advantage of this by measuring two windows for each packet:

1. **Noise window** — ~80 ms of audio just *before* the burst, where the
   discriminator is unlocked
2. **Signal window** — ~80 ms of audio *during* the burst, after framesync
   has been detected

The SNR is reported as:
```
SNR_dB = 20 * log10(noise_rms / signal_rms)
```

A higher value means deeper FM quieting, which means a stronger or cleaner
signal. Typical values:

| SNR | Reception quality |
|-----|-------------------|
| < 2 dB | Marginal — RS may not always recover |
| 3-6 dB | Acceptable — most packets decode |
| 7-15 dB | Good |
| > 15 dB | Strong, very clean |

## What this is NOT

**This is not calibrated dBm.** True received signal level (RSL) in dBm
requires a calibrated reference signal source. The SNR reported here is
relative — useful for comparison and trends, not for absolute power
reporting.

For applications where calibrated dBm is needed:

- Use `rtl_power` to tap pre-discriminator IQ samples
- Calibrate against a known reference signal
- See the GNURadio ecosystem for power-meter applications

For most diagnostic purposes (which is most cases), the relative SNR shown
here is what you actually want.

## Output formats

### Stdout / log file

```
[FEC0] src=769 path=[4602, 4601] port=0 hop=0 len=47 RS_corr=1 FS_hd=1 SNR=4.0dB SRP[ts=15081]: GSR(...)
```

### CSV

Three new columns:
- `snr_db` — signal-to-noise ratio in dB (e.g., `4.05`)
- `noise_rms` — pre-burst RMS amplitude (e.g., `0.044`)
- `signal_rms` — in-burst RMS amplitude (e.g., `0.028`)

### JSON Lines

```json
{
  "rx_time": {...},
  "fec_mode": 0,
  "signal": {
    "snr_db": 4.03,
    "noise_rms": 0.0447,
    "signal_rms": 0.0281
  },
  ...
}
```

### Stats summary

```
Signal quality (SNR):   min 3.5dB, avg 4.0dB, max 4.1dB (6 pkts)
Avg SNR by source:
     773: 4.1 dB
     719: 4.1 dB
     781: 4.1 dB
     769: 3.9 dB
```

This is great for:
- **Identifying weak sites** — sources with consistently low SNR
- **Detecting deterioration** — comparing today's stats to yesterday's
- **Antenna A/B testing** — moving the antenna and re-running

## Cross-reference with errors

The most useful diagnostic combination is comparing SNR with RS corrections.
Run with both `--stats` and the JSON output, then look for packets with low
SNR + high RS_corrections:

```bash
python -m alert2 capture.wav --jsonl-file - --quiet | \
  jq 'select(.rs_corrections > 0) | {src: .mant.source_addr, snr: .signal.snr_db, rs: .rs_corrections}'
```

If RS corrections cluster at low SNR, that's expected and confirms the
measurement is meaningful. If you see high RS corrections at high SNR,
something else is wrong (clock drift, interference, etc).

## Programmatic access

```python
from alert2 import decode_wav

packets = decode_wav('capture.wav')
for pkt in packets:
    if pkt.snr_db is not None:
        print(f"src={pkt.mant.source_addr}: "
              f"SNR={pkt.snr_db:.1f}dB "
              f"(noise={pkt.noise_rms:.4f} signal={pkt.signal_rms:.4f})")
```

## Live decoding

The streaming decoder also computes SNR for each packet seen on a live SDR
feed:

```bash
rtl_fm -f 171.850M -s 48000 -M fm -E dc -g 30 - 2>/dev/null | \
  python -m alert2 --stdin --log-file alert2.log --csv-file alert2.csv \
  --rotate-daily --stats
```

The log file will show per-packet SNR alongside everything else, and the
stats summary at the end will include per-source averages.

## Tuning the measurement windows

If you find the default windows aren't well-suited to your environment
(e.g., very short bursts, unusual FEC modes), you can adjust the constants
in `alert2/quality.py`:

- `NOISE_WINDOW_MS` (default 80) — how much pre-burst audio to use
- `SIGNAL_WINDOW_MS` (default 80) — how much in-burst audio to use
- `NOISE_GAP_MS` (default 50) — gap between noise window end and burst
  start (avoids including the AGC ramp-up)
