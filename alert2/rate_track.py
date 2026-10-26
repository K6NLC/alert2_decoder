# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Per-burst open-loop symbol-rate correction (--rate-track).

WHY
---
The decoder samples each burst at a fixed phase of a fixed 9-samples-per-
symbol grid. That is exact when the transmitter's symbol clock and the
receiver's sample clock agree, which they do on a crystal-locked radio
and sound card (SLO repeater 4601 measured -0.002%). It is NOT exact for
some SDR recordings: four captures of other networks showed ~0.1% symbol-
rate error, which walks the sampling instant 5.9 samples across one
652-symbol first block -- more than half a symbol -- so the eye closes
mid-block and the frame fails RS no matter which of the nine phases it
started on.

HOW
---
The eye-opening phase is measured in consecutive windows along the burst
(the mean |amplitude| at each of the nine phases, parabolic-interpolated
to a fraction of a sample). If the transmitter runs slow, the best phase
advances a little every window; the slope of that drift, in samples per
symbol, is the rate error. The burst is then re-sampled on the corrected
grid `start + phase0 + k * (SPB + slope)` by linear interpolation and
decoded as an ordinary soft bit stream.

This is open loop by design: one estimate per burst from the preamble and
the start of the data, no feedback from decisions. A closed loop (Gardner)
was tried earlier in this project and was destructive on marginal bursts.

WHEN IT RUNS
------------
Only when |slope| exceeds `min_slope` (default 0.002 samples/symbol =
0.022%). Below that the fixed grid is already within one sample over the
longest frame seen in the field, the correction can only add interpolation
noise, and it costs nothing because the slope measurement is a few hundred
array means per burst. The QTH capture (62 bursts) measured |slope| <=
0.012%, the SDR recordings -0.094..-0.108%, so the threshold sits between
them with a factor of two on either side. Above it the burst is decoded BOTH ways and the
usual best-wins dedup keeps one, so the rung can add packets but never
remove one.

Measured (SDR recordings of three other networks; per-burst phase-select
baseline at fs-hd 3, gate 0.40, slope -0.098% on every burst):
    1-1.wav  0 -> 5     4-1.wav  6 -> 12     5-1.wav  0 -> 4
QTH capture (no drift): 82 -> 82, no burst crosses the threshold.
"""
from typing import List, Optional, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from .constants import SAMPLES_PER_BIT as SPB
from .fec import first_block_cc_bits_padded

# bitsync (48) + framesync (32) bits precede fs_end
PREAMBLE_BITS = 80
DEFAULT_MIN_SLOPE = 0.002        # samples / symbol (0.022%)
DEFAULT_WINDOW_SYMBOLS = 60
DEFAULT_MAX_RESIDUAL = 1.0       # samples, RMS about the linear fit
MAX_RESAMPLE_SYMBOLS = 26000     # spec max frame (1023 B) + margin


def eye_phase(seg: 'np.ndarray') -> Tuple[float, float]:
    """Best sampling phase of `seg` as a fractional sample, plus the eye
    contrast (max - mean) / mean of the per-phase |amplitude|. Contrast is
    reported for diagnostics only; it was measured NOT to mark the end of
    a burst reliably on discriminator audio."""
    n = (len(seg) // SPB) * SPB
    if n < SPB * 8:
        return 0.0, 0.0
    sc = np.abs(seg[:n]).reshape(-1, SPB).mean(axis=0)
    i = int(np.argmax(sc))
    a, b, c = sc[(i - 1) % SPB], sc[i], sc[(i + 1) % SPB]
    den = a - 2 * b + c
    frac = 0.5 * (a - c) / den if abs(den) > 1e-12 else 0.0
    mean = float(sc.mean())
    contrast = (float(b) - mean) / mean if mean > 0 else 0.0
    return (i + frac) % SPB, contrast


def estimate_slope(filtered: 'np.ndarray', start: int, fec_mode: int = 0,
                   window_symbols: int = DEFAULT_WINDOW_SYMBOLS,
                   max_residual: float = DEFAULT_MAX_RESIDUAL
                   ) -> Tuple[float, float, int]:
    """Symbol-rate error of the burst beginning at sample `start`.

    Returns (slope_samples_per_symbol, phase0, n_windows).

    The measurement spans the preamble plus the first block -- the part
    of every frame whose length is known before anything is decoded (736
    symbols in mode 0, 12 windows of 60). Running on past that would need
    the AirLink length, and letting an "end of burst" heuristic decide was
    measured to fail: on a discriminator, noise and back-to-back TDMA
    traffic keep the eye contrast near 0.1 after a burst ends, so the fit
    would absorb random phases. Extrapolating a 12-window fit over the
    longest field frame (~4000 symbols) is worth about 0.4 sample of
    error, well inside the eye.

    A fit whose residual RMS exceeds `max_residual` samples is not a clean
    linear drift (weak burst, collision) and returns slope 0 so the
    caller leaves the fixed grid alone.
    """
    W = window_symbols * SPB
    n_win = (PREAMBLE_BITS + first_block_cc_bits_padded(fec_mode)) \
        // window_symbols
    phases = []
    for k in range(n_win):
        a = start + k * W
        seg = filtered[a:a + W + SPB]
        if len(seg) < W:
            break
        phases.append(eye_phase(seg)[0])
    if len(phases) < 4:
        return 0.0, (phases[0] if phases else 0.0), len(phases)
    u = np.unwrap(np.asarray(phases) * (2 * np.pi / SPB)) * (SPB / (2 * np.pi))
    x = np.arange(len(u)) * window_symbols + window_symbols / 2.0
    slope, icpt = np.polyfit(x, u, 1)
    resid = u - (slope * x + icpt)
    if float(np.sqrt(np.mean(resid ** 2))) > max_residual:
        return 0.0, float(icpt % SPB), len(phases)
    return float(slope), float(icpt % SPB), len(phases)


def resample_burst(filtered: 'np.ndarray', start: int, phase0: float,
                   slope: float, n_symbols: int = MAX_RESAMPLE_SYMBOLS
                   ) -> 'np.ndarray':
    """Soft symbol stream on the corrected grid start+phase0+k*(SPB+slope)."""
    idx = start + phase0 + np.arange(n_symbols) * (SPB + slope)
    idx = idx[(idx >= 0) & (idx < len(filtered) - 1)]
    return np.interp(idx, np.arange(len(filtered)), filtered)


def corrected_stream(filtered: 'np.ndarray', abs_sample: int,
                     fec_mode: int = 0,
                     min_slope: float = DEFAULT_MIN_SLOPE,
                     max_symbols: int = MAX_RESAMPLE_SYMBOLS
                     ) -> Optional[Tuple['np.ndarray', float]]:
    """For a burst whose framesync ends at `abs_sample` (absolute sample
    index into `filtered`), return (soft_symbols, slope) on the rate-
    corrected grid, or None when the measured drift is below `min_slope`.
    The returned stream begins at the burst's preamble, so its framesync
    lands near bit PREAMBLE_BITS."""
    if np is None:
        return None
    start = int(abs_sample) - PREAMBLE_BITS * SPB
    if start < 0:
        start = 0
    slope, phase0, n = estimate_slope(filtered, start, fec_mode)
    if n < 4 or abs(slope) < min_slope:
        return None
    return resample_burst(filtered, start, phase0, slope, max_symbols), slope


def decode_rate_corrected(decoder, filtered: 'np.ndarray', bursts,
                          min_slope: float = DEFAULT_MIN_SLOPE,
                          label: str = 'rate-track') -> List:
    """Decode every burst in `bursts` (BurstCandidate list from
    burst_detect.detect_bursts) whose measured drift exceeds `min_slope`,
    on its corrected grid. Returns the packets; the caller merges them
    with the fixed-grid results through the normal best-wins dedup."""
    from .demod import slice_bits
    from .fec import estimate_amplitude
    out = []
    for bc in bursts:
        r = corrected_stream(filtered, bc.abs_sample, bc.fec_mode, min_slope)
        if r is None:
            continue
        soft, slope = r
        bits = slice_bits(soft)
        sl = [float(v) for v in soft]
        if bc.inverted:
            bits = [1 - b for b in bits]
            sl = [-v for v in sl]
        for pkt in decoder.decode_bits(
                bits, soft_samples=sl,
                amplitude_estimate=estimate_amplitude(sl),
                soft_inverted=bc.inverted, presync_search=False):
            pkt.decode_path = label
            pkt.rate_ppm = slope / SPB * 1e6
            out.append(pkt)
    return out
