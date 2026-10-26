# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reject hopeless framesync candidates before the Viterbi runs.

WHY
---
Profiling rc29 (3 phases, 30 s of QTH audio, 2.63 s total) put the cost here:

    Viterbi + RS          1.39 s   53%
    quantize_soft         0.59 s   23%
    slice_bits/soft       0.26 s   10%
    find_framesync        0.03 s    1.3%   (after vectorisation)

_try_decode_at was called 125 times to produce a handful of packets. At
--max-fs-hd 6 the framesync search returns hundreds of candidates per phase
and almost none of them are real: on a 123 s QTH capture, 356 candidates
yielded 55 real bursts and 301 spurious ones. Three quarters of the CPU is
spent decoding candidates that were never going to decode.

Framesync Hamming distance cannot separate them — the handoff records real
packets occurring at raw HD 7-11, and HD is explicitly "not a noise
discriminator". But the 80-bit PREAMBLE can. Correlating the already-
decimated soft samples against the known bitsync+framesync sequence, at the
single sampling instant already chosen, costs 80 multiply-adds:

    measured 0.5 ms for 159 candidates = 0.00001x real time

MEASURED SEPARATION (four captures, 70 real bursts / 924 spurious):

    real bursts     min 0.610   (includes one decoding at MER -1.23 dB)
    spurious        max 0.559
    gate at 0.40    rejects 98.6-98.7% of spurious, loses 0 of 70 real

The QTH capture had silent stretches removed by the operator, so its
spurious candidates are all SIGNAL-ADJACENT — partial preamble matches near
real bursts, which are the hardest case. Pure-noise candidates score lower,
so both the safety margin and the rejection rate should be better in the
field than these numbers suggest.

SAFETY
------
This gate can only REMOVE decode attempts, so unlike a recovery rung it is
not superset-safe by construction — a threshold set too high silently drops
real packets. Therefore:

  * default is OFF (threshold 0.0), opt in via --candidate-gate
  * the recommended 0.40 sits 0.21 below the lowest real burst observed
  * tests assert every real burst in the captures survives the gate
  * a candidate whose preamble cannot be scored (too near the start of the
    stream, wrong length) is ALWAYS admitted, never rejected on a
    missing measurement
"""
from typing import Optional

try:
    import numpy as np
except ImportError:
    np = None

from .constants import (BITSYNC_HEX, BITSYNC_BITS,
                        FRAMESYNCS, FRAMESYNC_BITS)

PREAMBLE_BITS = BITSYNC_BITS + FRAMESYNC_BITS

# Sits 0.21 below the lowest real burst seen across four captures (0.610)
# and 0.16 above the highest spurious candidate (0.446 on the marginal QTH
# capture, 0.559 pooled). Deliberately nearer the spurious side of the gap.
DEFAULT_THRESHOLD = 0.40

_REF = {}


def _reference(fec_mode: int):
    """The 80 transmitted preamble bits as +/-1: bitsync then framesync."""
    ref = _REF.get(fec_mode)
    if ref is None:
        bits = [(BITSYNC_HEX >> (BITSYNC_BITS - 1 - i)) & 1
                for i in range(BITSYNC_BITS)]
        fs = FRAMESYNCS[fec_mode]
        bits += [(fs >> (FRAMESYNC_BITS - 1 - i)) & 1
                 for i in range(FRAMESYNC_BITS)]
        ref = np.where(np.asarray(bits, dtype=np.int8) != 0, 1.0, -1.0)
        _REF[fec_mode] = ref
    return ref


def preamble_correlation(soft, fs_end_bit: int, fec_mode: int,
                         inverted: bool = False) -> Optional[float]:
    """Normalised match between the soft samples and the known preamble.

    Args:
        soft: per-symbol soft values ALREADY decimated at the sampling
            phase in use. No interpolation and no offset sweep — that is
            what makes this affordable as a gate (the full timing probe,
            which sweeps 73 offsets, costs 1.79x real time when run over
            every candidate).
        fs_end_bit: index one past the framesync, i.e. the decoder's `fe`.
        fec_mode: selects the framesync pattern.
        inverted: True when the bits being decoded are the inverted
            polarity, since `soft` is then the un-inverted stream.

    Returns a value in [-1, 1], or None when the preamble does not lie
    fully inside `soft`. None means "unknown", and callers must admit the
    candidate rather than reject it.
    """
    if np is None or soft is None:
        return None
    start = fs_end_bit - PREAMBLE_BITS
    if start < 0 or fs_end_bit > len(soft):
        return None
    y = np.asarray(soft[start:fs_end_bit], dtype=np.float64)
    if y.size != PREAMBLE_BITS:
        return None
    denom = float(np.dot(y, y))
    if denom <= 0.0 or not np.isfinite(denom):
        return None
    ref = _reference(fec_mode)
    if inverted:
        ref = -ref
    c = float(np.dot(y, ref) / np.sqrt(denom * PREAMBLE_BITS))
    return c if np.isfinite(c) else None


def admit(soft, fs_end_bit: int, fec_mode: int, threshold: float,
          inverted: bool = False) -> bool:
    """Should this candidate be handed to the decode ladder?

    Uses the MAGNITUDE of the correlation, so the caller's polarity
    bookkeeping cannot silently disable the gate.

    Why magnitude rather than the signed value: callers do not agree on
    whose job it is to invert. The multiphase pass pre-inverts its soft
    samples AND passes soft_inverted=True, so decode_bits inverts a
    second time and `src` comes back at the ORIGINAL polarity while
    `bits` are inverted. That cancels harmlessly for the Viterbi — which
    only ever compares quantised soft against those same bits — but a
    signed preamble correlation reads it as a large NEGATIVE score and
    rejects every real candidate. Measured: with a signed gate, all
    mf-phase* decodes vanished (19 of 20 packets fell back to the burst
    path) and the marginal MER floor dropped 6.24 -> 5.56 dB.

    A wrong-polarity real burst still correlates strongly, just with the
    opposite sign; noise correlates weakly either way (measured max
    |corr| 0.36 over 200 pure-noise windows). Taking |corr| therefore
    keeps all of the discrimination and none of the polarity fragility.

    Fails OPEN: a threshold of zero or below disables the gate, and an
    unscoreable candidate is admitted. The only way to reject is a
    successfully measured correlation that falls short.
    """
    if not threshold or threshold <= 0.0:
        return True
    c = preamble_correlation(soft, fs_end_bit, fec_mode, inverted)
    if c is None:
        return True
    return abs(c) >= threshold
