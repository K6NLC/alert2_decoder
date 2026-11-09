# `--rate-track` — per-burst symbol-rate correction

## The problem it solves

The demodulator resamples audio to 43200 Hz (9 samples per 4800-baud
symbol) and takes one sample per symbol at a fixed phase. That assumes the
transmitter's symbol clock and the receiver's sample clock agree. On a
crystal-locked radio + sound card they do: the SLO repeater measured
-0.002%, and the QTH capture's 62 bursts all measured |drift| <= 0.012%.

Four SDR recordings of other networks did not. Every burst measured about
-0.098% (the SDR's clock, since all sources in a file agree). At 0.1% the
sampling instant walks 5.9 samples across one 652-symbol first block —
more than half a symbol — so the eye closes mid-block and the follow-on
RS fails on every phase. Symptom: every frame truncated, MER 2-3 dB low,
and `--multiphase` cannot help because no single phase is right for the
whole block.

## What it does

For each burst found by the per-burst detector (`burst_detect.py`):

1. Measure the eye-opening phase in 12 consecutive 60-symbol windows
   spanning the preamble and the first block: mean |amplitude| at each of
   the nine phases, parabolic-interpolated to a fraction of a sample.
2. Unwrap and fit a line. Its slope, in samples per symbol, is the rate
   error. The fit is accepted only when it is unmistakably a drift: RMS
   residual <= 0.5 sample AND total drift over the span >= 8x the
   residual. Real SDR drift fits at residual 0.07-0.18 with ratio >= 15;
   the QTH's noise-only bursts reach ratio 4.3 at most.
3. If |slope| >= `--rate-track-min-slope` (default 0.002 samples/symbol,
   0.022%), re-sample the burst on the grid `start + phase0 + k*(9+slope)`
   by linear interpolation and decode that stream as well. The packet is
   labelled `rate-track` and carries `rate=±Nppm` in the diag line.

The fixed-grid decode still runs. Best-wins dedup keeps one copy, so the
rung can add packets but never remove one.

## Why it is open loop, and why 12 windows

A decision-directed timing loop (Gardner) was tried earlier in this
project and was destructive on marginal bursts. One estimate per burst
from the strongest part of the frame (the preamble is a known
high-transition pattern) avoids that.

The fit stops at the end of the first block because that is the part of
every frame whose length is known before anything is decoded. An "end of
burst" heuristic from eye contrast was tried and measured to fail: on
discriminator audio, noise and back-to-back TDMA traffic keep the contrast
near 0.1 after a burst ends, so the fit absorbed random phases.
Extrapolating a 12-window fit over the longest field frame (~4000
symbols) costs about 0.4 sample, well inside the eye.

## Measured

Per-burst phase-select baseline, fs-hd 3, gate 0.40, 9 phases:

| recording | drift | fixed grid | + rate-track |
|---|---|---|---|
| 1-1.wav (SDR) | -0.098% | 1 (truncated) | 5 |
| 4-1.wav (SDR) | -0.098% | 7 | 12 |
| 5-1.wav (SDR) | -0.099% | 1 (truncated) | 4 |
| QTH 2.1 min (TK790 + USB) | <= 0.012% | 82 | 82, rung never fired |

Synthetic check (`tests/test_rate_track.py`): generator audio stretched by
0.08-0.15% recovers the injected slope within 15%; a 150-byte frame at
0.10% truncates on the fixed grid and decodes complete through the rung;
with no drift the rung emits nothing.

## Cost

The slope measurement is twelve small array means per burst — not
measurable against the decode. The re-sample + decode only happens for
bursts over the threshold, so on a clean chain the cost is zero. Requires
`--multiphase` (it needs the matched-filter stream); works with or without
`--phase-select`.

## Field note, first day (QTH 2026-10-26, rc38)

3438 packets, the rung fired once: a weak burst (MER 8.0 dB, the day's
minimum) measured a fictitious -738 ppm, and its rate-corrected first
block was emitted as a TRUNCATED copy of a frame the fixed grid had
already decoded complete 34 ms earlier. Two fixes in rc39:

- the acceptance test above (rc38 only checked residual <= 1.0 sample);
- `_drop_shadowed_truncations`: a truncated packet whose bytes are a
  prefix of a complete packet in the same batch is dropped, whatever
  path produced it. Byte-level, so it needs no SRP timestamp.

Neither changed the SDR results (5 / 12 / 4) or the QTH capture (82).
