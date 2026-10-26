# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Per-burst symbol-timing measurement — OBSERVATION ONLY.

WHY THIS EXISTS
---------------
MER at both sites spreads ~2.5 dB sd (7.5 dB p05..p95) while SNR spreads
0.59 dB and capture level (alpha) is flat. Measured on 2802 bursts received
simultaneously at the QTH and the shop (same 4601 transmission, same instant):

    corr(QTH MER, SHOP MER) = +0.013
    sd(QTH) 2.47   sd(SHOP) 2.58   sd(difference) 3.55
    independence would predict sd(difference) = 3.57

The two receivers agree on nothing. The spread is therefore generated
LOCALLY in each receive chain, not by the transmitter or the link. It is
also not fading (zero time-correlation at every lag), not station-specific
(1.81 dB across 26 origin stations), and is a clean per-burst property
(within-frame sd 0.020 dB).

Symbol sampling phase is the leading suspect: with SAMPLES_PER_BIT = 9 the
residual timing error is up to +/-1/18 symbol and is independently random
per burst at each receiver — exactly the observed signature. But a
whole-stream fractional shift moved a given packet's MER by only ~1.19 dB
against a 7.5 dB population spread, so timing may explain only part of it.

THIS MODULE DOES NOT DECODE ANYTHING DIFFERENTLY. It measures, per accepted
burst, the fractional timing offset that WOULD have maximised the match to
the known 80-bit preamble, and reports it alongside MER. One day of field
data then answers directly: do low-MER bursts have large timing offsets?

  * If yes  -> per-burst timing correction is justified, and the logged
               offsets size the available gain before anything is built.
  * If no   -> timing is exonerated and the next suspect is the per-burst
               amplitude estimate feeding soft quantisation.

Cost: one interpolation of ~80 symbols per accepted burst. Negligible
against a Viterbi decode, and it runs only on packets that already decoded.
"""
from typing import Optional

try:
    import numpy as np
except ImportError:
    np = None

from .constants import (SAMPLES_PER_BIT, BITSYNC_HEX, BITSYNC_BITS,
                        FRAMESYNCS, FRAMESYNC_BITS)

PREAMBLE_BITS = BITSYNC_BITS + FRAMESYNC_BITS

_PRE_CACHE = {}


def _preamble_symbols(fec_mode: int):
    """The 80 transmitted preamble bits as +/-1, bitsync then framesync."""
    key = fec_mode
    a = _PRE_CACHE.get(key)
    if a is None:
        bits = [(BITSYNC_HEX >> (BITSYNC_BITS - 1 - i)) & 1
                for i in range(BITSYNC_BITS)]
        fs = FRAMESYNCS[fec_mode]
        bits += [(fs >> (FRAMESYNC_BITS - 1 - i)) & 1
                 for i in range(FRAMESYNC_BITS)]
        a = np.where(np.asarray(bits, dtype=np.int8) != 0, 1.0, -1.0)
        _PRE_CACHE[key] = a
    return a


def measure_burst_timing(filtered, fs_end_bit: int, bit_phase: float,
                         fec_mode: int, subdiv: int = 8,
                         search: float = 4.5,
                         inverted: bool = False) -> Optional[dict]:
    """Fractional timing offset that best matches the known preamble.

    Args:
        filtered: matched-filter output at RX_SAMPLE_RATE (9 samples/symbol),
                  i.e. the stream BEFORE symbol decimation.
        fs_end_bit: bit index one past the framesync (the decoder's `fe`).
        bit_phase: the integer sampling phase demodulate() used.
        fec_mode: selects which framesync pattern is expected.
        inverted: True when the decoder is working the INVERTED-polarity
                  pass. `filtered` is always the un-inverted matched-filter
                  output, so the reference must be negated to match. Getting
                  this wrong makes every correlation negative and drives the
                  argmax to a search boundary — which is exactly what
                  happened on 2275 live bursts (100% saturated at |4.500|,
                  corr@0 = -0.914 on a burst that reads +0.914 correctly).
        subdiv: offsets tested per side; total 2*subdiv+1 points.
        search: half-width of the search in SAMPLES. Default 4.5 spans a
                FULL symbol (SAMPLES_PER_BIT = 9), i.e. every integer phase.
                A narrower +/-0.5 window was tried first and every degraded
                burst saturated at the boundary, which cannot distinguish
                "optimum is just outside" from "surface is flat and the
                argmax wandered". Searching the whole symbol separates them:
                a burst wanting a DIFFERENT INTEGER PHASE reports |offset|
                of 1..4 samples, while a genuinely flat surface reports a
                low peak `corr`.

    Returns dict with:
        'offset'      best fractional offset in samples (-search..+search)
        'offset_sym'  the same as a fraction of a symbol
        'corr'        normalised preamble correlation at that offset
        'corr_at_0'   normalised correlation at the offset actually used
        'gain'        corr - corr_at_0 (how much was left on the table)
    or None if the preamble does not lie fully inside `filtered`.

    Sign convention: `offset` is what should be ADDED to the sampling
    instant to peak the preamble match.
    """
    if np is None or filtered is None:
        return None
    start_bit = fs_end_bit - PREAMBLE_BITS
    if start_bit < 0:
        return None
    ref = _preamble_symbols(fec_mode)
    if inverted:
        ref = -ref
    n = len(filtered)
    base = bit_phase + start_bit * SAMPLES_PER_BIT
    idx = base + np.arange(PREAMBLE_BITS) * SAMPLES_PER_BIT
    if idx[0] < 1 or idx[-1] > n - 2:
        return None
    grid = np.arange(n)
    # Resolution is held at ~1/8 sample regardless of search width, so a
    # wider window costs more points rather than coarser ones.
    npts = int(round(2 * search * subdiv)) + 1
    offs = np.linspace(-search, search, npts)
    best_c, best_o = -np.inf, 0.0
    c_at_0 = None
    all_c = []
    for o in offs:
        y = np.interp(idx + o, grid, filtered)
        d = np.dot(y, y)
        if d <= 0:
            continue
        c = float(np.dot(y, ref) / np.sqrt(d * PREAMBLE_BITS))
        all_c.append(c)
        if abs(o) < 1e-9:
            c_at_0 = c
        # Strictly-greater keeps the FIRST maximum, which on a flat
        # correlation top lands at the search edge and misreports a
        # well-timed burst as badly mistimed. Ties go to the smaller
        # |offset|: if two phases match the preamble equally, the one
        # nearer the phase already in use is the honest answer.
        if c > best_c + 1e-12 or (abs(c - best_c) <= 1e-12
                                  and abs(o) < abs(best_o)):
            best_c, best_o = c, float(o)
    if c_at_0 is None:
        y = np.interp(idx, grid, filtered)
        d = np.dot(y, y)
        c_at_0 = float(np.dot(y, ref) / np.sqrt(d * PREAMBLE_BITS)) if d > 0 else 0.0
    if not np.isfinite(best_c):
        return None
    # Peak sharpness: how far the best correlation stands above the median
    # across the search. Near zero => flat surface => the argmax is noise.
    prom = best_c - float(np.median(all_c)) if all_c else 0.0
    # A strongly NEGATIVE correlation at the used phase means the reference
    # polarity is wrong, not that timing is bad. Surfaced so the failure
    # mode that produced the saturated field run is self-announcing.
    return {
        'polarity_suspect': bool(c_at_0 < -0.3),
        'prominence': prom,
        'corr_min': float(min(all_c)) if all_c else 0.0,
        'offset': best_o,
        'offset_sym': best_o / SAMPLES_PER_BIT,
        'corr': best_c,
        'corr_at_0': c_at_0,
        'gain': best_c - c_at_0,
    }
