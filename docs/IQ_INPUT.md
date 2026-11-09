# I/Q input — decoding raw SDR captures

```bash
alert2-decode gqrx_20261029_052740_171850000_240000_fc.raw \
    --multiphase --phase-candidates 9 --rate-track --max-fs-hd 4 \
    --eq-retry --presync-eq
```

A GQRX I/Q recorder file needs no other options: the filename carries the
centre frequency and sample rate. Other sources:

| source | format | options |
|---|---|---|
| GQRX I/Q recorder (`..._fc.raw`) | complex float32 | none |
| `rtl_sdr -f F -s R out.cu8` | unsigned 8-bit I/Q | `--iq-rate R --iq-center F` |
| SDR++ / SDRangel baseband WAV | stereo 16-bit, I=L Q=R | `--iq-format wav --iq-center F` |
| anything else | `--iq-format {fc32,cu8,cs8,cs16,wav}` | `--iq-rate`, `--iq-center` |

`--iq` forces I/Q interpretation for a filename the decoder would not
recognise. `--iq-center` is only needed to make `carrier=` absolute; the
decode itself works without it.

## Why

The audio path depends on the SDR software having centred the carrier
before it demodulated. Three recordings made with the carrier near the
edge of the channel filter (QTH, 2026-10-28) came out with a −0.4 FS DC
step and no FM quieting; the signal was ~10 dB under the noise and
nothing could be recovered from the audio afterwards. With I/Q the
demodulation happens here, after the carrier offset has been measured.

## What it does

1. **Survey.** Short-time spectrum of the whole capture, 200 Hz bins. A
   bin is "bursty" when its 99.5th-percentile power stands ≥ 11 dB above
   its median (noise reaches ~9.4 dB over a 30 s file). Bursty bins
   within ±10 kHz are one channel; its carrier is their weighted
   centroid. Up to six channels, strongest first. `--freq 170.2625M`
   skips the survey and decodes that frequency only.
2. **Channelize.** Mix the channel to 0 Hz, low-pass to ±4.5 kHz
   (`--iq-bw`), decimate to 48 kHz.
3. **Discriminate.** `arg(y[n]·conj(y[n−1]))/π` — the same baseband a
   radio's discriminator pin gives.
4. **Decode** through the unchanged file-mode chain (multiphase,
   phase-select, rate-track, …).
5. **Per-burst refinement.** Each burst the chain detects is re-mixed on
   its own carrier — the mean discriminator value over its preamble and
   first block is its frequency offset — with the narrow filter, and
   decoded again (`decode_path=iq-refined`). Best-wins keeps the better
   copy. This is what handles a channel shared by sites with different
   crystals; a synthetic test with two sites 2.4 kHz apart in one
   channel recovers both and reports each carrier within 400 Hz.

Every packet carries `carrier=` in the diag line: absolute when the
centre frequency is known, else the offset from the tuned centre.

## Channel filter width

Measured on the first QTH I/Q capture (4 bursts):

| `--iq-bw` | packets |
|---|---|
| 3500 | 4/4 |
| 4500 | 4/4 (default) |
| 5500 | 4/4 |
| 7000 | 2/4 |
| 9000 | 0/4 |

The signal occupies about ±4 kHz; a wider filter only admits noise, and
at threshold that noise is what costs the quieting.

## First capture, and what it says about gain

`gqrx_20261029_052740_171850000_240000_fc.raw`, 33 s, RTL-SDR at the QTH:
survey found one channel at −27 Hz (score 14.9 dB), carrier
171.849902 MHz — the dongle is within 0.6 ppm — and both frames in the
file decoded complete (src 735 via 4604/4601, src 720 via 4601), at MER
2–4 dB. The second file gave both of its frames too (799 via 4603 and
4604).

MER that low is the capture, not the path: the I samples take only
**four distinct values** — the ADC is toggling two bits, 0.5 LSB rms.
The RF gain was effectively off. For a usable capture the noise floor
should sit at 10–20 LSB rms, i.e. about 0.05–0.1 in GQRX's float units,
with bursts well clear of ±1.0. Expect MER to rise by 10 dB or more
once the gain is up.

## Recording with GQRX

- Input rate 240–250 ksps (Configure I/O devices). The recorder writes
  the full input rate; 2.4 Msps is 20 MB/s of nothing useful.
- Park the hardware tune a few kHz off the channel so the DC spike is
  not on the signal. The survey finds the channel wherever it is.
- Gain as above.
- Keep the filename GQRX writes.
