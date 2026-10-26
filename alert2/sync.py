# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Bitsync and framesync detection.

Per AirLink spec §2.2:
  - Bit Synchronization pattern: 0xEB90B433AAAA (48 bits)
  - Frame Synchronization patterns: one per FEC mode (32 bits each)
"""
from typing import List, Iterator, Tuple, Optional

try:
    import numpy as _np
except ImportError:                      # numpy is optional for these paths
    _np = None

from .constants import (
    BITSYNC_HEX, BITSYNC_BITS, FRAMESYNCS, FRAMESYNC_BITS,
)

# Vectorised pattern search (rc26).
#
# find_framesync() walked every bit in Python maintaining a shift register.
# Measured on an 84 s QTH capture it cost 0.502 s — and MULTIPHASE RUNS IT
# ONCE PER CANDIDATE PHASE, so at 6 candidates it dominated the front end.
# The numpy form below scores every offset at once and measured 0.070 s for
# the same work: ~7x, with identical output (asserted by
# tests/test_sync_vectorised.py against the scalar reference on real bits).
#
# The scalar implementations are kept as _*_scalar and remain the
# definition of correct behaviour.

_PAT_CACHE = {}


def _pattern_bits(word: int, nbits: int):
    key = (word, nbits)
    a = _PAT_CACHE.get(key)
    if a is None:
        a = _np.array([(word >> (nbits - 1 - i)) & 1 for i in range(nbits)],
                      dtype=_np.int8)
        _PAT_CACHE[key] = a
    return a


def _hd_all_offsets(bits, word: int, nbits: int):
    """Hamming distance of `word` against every window of `bits`.

    Returns an int array `hd` where hd[i] is the distance for the window
    starting at i, i.e. ending one past index i+nbits-1.
    """
    arr = _np.asarray(bits, dtype=_np.int8)
    if arr.size < nbits:
        return _np.empty(0, dtype=_np.int32)
    win = _np.lib.stride_tricks.sliding_window_view(arr, nbits)
    pat = _pattern_bits(word, nbits)
    return (win != pat).sum(axis=1, dtype=_np.int32)


def popcount(n: int) -> int:
    """Count set bits in an integer."""
    return bin(n).count('1')


def find_bitsync(bits: List[int],
                 max_hd: int = 4) -> List[Tuple[int, int]]:
    """Find all bitsync occurrences in the bit stream.

    Returns list of (position_after_pattern, hamming_distance).
    """
    if _np is not None:
        hd = _hd_all_offsets(bits, BITSYNC_HEX, BITSYNC_BITS)
        if hd.size == 0:
            return []
        idx = _np.flatnonzero(hd <= max_hd)
        return [(int(i) + BITSYNC_BITS, int(hd[i])) for i in idx]
    return _find_bitsync_scalar(bits, max_hd)


def _find_bitsync_scalar(bits: List[int],
                         max_hd: int = 4) -> List[Tuple[int, int]]:
    """Reference implementation. Defines correct behaviour."""
    matches = []
    sr = 0
    mask = (1 << BITSYNC_BITS) - 1
    for i, b in enumerate(bits):
        sr = ((sr << 1) | (b & 1)) & mask
        if i >= BITSYNC_BITS - 1:
            hd = popcount(sr ^ BITSYNC_HEX)
            if hd <= max_hd:
                matches.append((i + 1, hd))
    return matches


def find_framesync(bits: List[int], max_hd: int = 4,
                   modes: Optional[List[int]] = None) -> List[Tuple[int, int, int]]:
    """Find all framesync occurrences in the bit stream.

    Returns list of (position_after_pattern, fec_mode, hamming_distance).

    modes: which FEC modes to search for (default: all).

    Output order matches the scalar reference: ascending position, and
    within a position, ascending mode order as given in `modes`.
    """
    if modes is None:
        modes = list(FRAMESYNCS.keys())
    if _np is not None:
        per_mode = []
        for mode in modes:
            hd = _hd_all_offsets(bits, FRAMESYNCS[mode], FRAMESYNC_BITS)
            if hd.size == 0:
                continue
            idx = _np.flatnonzero(hd <= max_hd)
            per_mode.append((mode, idx, hd))
        if not per_mode:
            return []
        # Merge by position so ties keep the scalar loop's mode ordering.
        out = []
        for mode, idx, hd in per_mode:
            out.extend((int(i) + FRAMESYNC_BITS, mode, int(hd[i]))
                       for i in idx)
        order = {m: k for k, m in enumerate(modes)}
        out.sort(key=lambda t: (t[0], order[t[1]]))
        return out
    return _find_framesync_scalar(bits, max_hd, modes)


def _find_framesync_scalar(bits: List[int], max_hd: int = 4,
                           modes: Optional[List[int]] = None
                           ) -> List[Tuple[int, int, int]]:
    """Reference implementation. Defines correct behaviour."""
    if modes is None:
        modes = list(FRAMESYNCS.keys())

    matches = []
    sr = 0
    mask = (1 << FRAMESYNC_BITS) - 1
    for i, b in enumerate(bits):
        sr = ((sr << 1) | (b & 1)) & mask
        if i >= FRAMESYNC_BITS - 1:
            for mode in modes:
                hd = popcount(sr ^ FRAMESYNCS[mode])
                if hd <= max_hd:
                    matches.append((i + 1, mode, hd))
    return matches


def find_framesync_with_bitsync_gate(
    bits: List[int],
    max_fs_hd: int = 4,
    max_bs_hd: int = 8,
    gate_window_bits: int = 96,
) -> List[Tuple[int, int, int]]:
    """Find framesyncs that are preceded by a bitsync within `gate_window_bits`.

    This greatly reduces false positives from noise: framesync should
    follow bitsync within ~32-96 bits in a real burst.

    Returns list of (position_after_framesync, fec_mode, framesync_hd).
    """
    bs_matches = find_bitsync(bits, max_bs_hd)
    if not bs_matches:
        return []

    bs_positions = sorted(set(p for p, _ in bs_matches))

    fs_matches = find_framesync(bits, max_fs_hd)
    gated = []
    for fs_pos, mode, fs_hd in fs_matches:
        # Check if any bitsync ended in [fs_pos - 32 - gate_window, fs_pos - 32]
        fs_start = fs_pos - FRAMESYNC_BITS  # Where framesync started
        for bs_pos in bs_positions:
            if 0 <= fs_start - bs_pos <= gate_window_bits:
                gated.append((fs_pos, mode, fs_hd))
                break

    return gated


# =============================================================================
# Soft-decision framesync (see docs/SOFT_FRAMESYNC.md)
#
# Hard framesync slices each soft sample to a bit then counts Hamming
# distance. That discards confidence: 4 wrong bits that were all
# near-threshold coin-flips is far more likely a real framesync than 4
# confidently-wrong bits, but hard-decision can't tell them apart.
#
# Soft framesync correlates the 32 calibrated soft samples in the window
# against the bipolar framesync pattern (a matched filter):
#     score = Σ_k  r_k * (2*p_k - 1)
# normalized by amplitude so the threshold is SNR-independent:
#     norm_score = score / (amplitude_estimate * FRAMESYNC_BITS)  ∈ ~[-1,1]
# +1 = perfect noiseless match; ~0 = noise; -1 = inverted framesync.
# =============================================================================

# Precompute bipolar (±1) pattern bit lists per FEC mode, MSB-first to
# match the shift-register bit order used by the hard path.
_FS_BIPOLAR = {
    mode: [1 if (pat >> (FRAMESYNC_BITS - 1 - k)) & 1 else -1
           for k in range(FRAMESYNC_BITS)]
    for mode, pat in FRAMESYNCS.items()
}


def soft_score_to_synthetic_hd(norm_score: float) -> int:
    """Map a soft norm_score back to an approximate integer Hamming
    distance for diag/JSON continuity.

    A perfect match (norm_score=1.0) -> HD 0. Each confidently-wrong
    bit costs 2/FRAMESYNC_BITS off the score, so:
        HD ≈ round((1 - norm_score) * FRAMESYNC_BITS / 2)
    Clamped to [0, FRAMESYNC_BITS]. This keeps existing log parsers and
    the forwarder's (unused-but-present) framesync_hd field stable; the
    richer norm_score is logged alongside it in diag text.
    """
    hd = round((1.0 - norm_score) * FRAMESYNC_BITS / 2.0)
    if hd < 0:
        hd = 0
    elif hd > FRAMESYNC_BITS:
        hd = FRAMESYNC_BITS
    return hd


def find_framesync_soft(
    soft_samples: List[float],
    amplitude_estimate: float,
    min_norm_score: float = 0.62,
    modes: Optional[List[int]] = None,
) -> List[Tuple[int, int, float]]:
    """Find framesyncs by soft matched-filter correlation.

    Args:
        soft_samples: per-bit float soft values, index-aligned with the
            hard `bits` list (same length/order) — exactly the array
            demod.py exposes as info['soft_samples'] for soft-Viterbi.
        amplitude_estimate: nominal symbol amplitude |A| (same estimate
            soft-Viterbi uses). Used to normalize the score so the
            threshold is SNR-independent.
        min_norm_score: detection threshold on the normalized score.
            Default 0.62 (zero false-positives in realistic >7dB regime;
            ~1/1000 in pathological noise-as-signal case, bounded
            further by RS parity. Calibration target — sweep per site).
        modes: which FEC modes to search (default all).

    Returns list of (position_after_framesync, fec_mode, norm_score).
    Positive norm_score = normal polarity. The decoder's existing
    inverted-polarity pass handles inverted bursts; soft-FS reports the
    magnitude-positive matches per polarity it is given (the caller
    negates soft_samples for the inverted pass, exactly as soft-Viterbi
    does), so behavior stays orthogonal and A/B-attributable.

    Defensive: if amplitude_estimate is non-finite or <= 0, returns []
    (no confidence -> no soft detection; the hard path still runs).
    """
    import math
    if (amplitude_estimate is None
            or not math.isfinite(amplitude_estimate)
            or amplitude_estimate <= 0):
        return []
    if modes is None:
        modes = list(FRAMESYNCS.keys())

    n = len(soft_samples)
    if n < FRAMESYNC_BITS:
        return []

    denom = amplitude_estimate * FRAMESYNC_BITS
    matches = []
    # Sliding window of the last FRAMESYNC_BITS soft samples.
    # Window covers samples [i-FRAMESYNC_BITS+1 .. i]; a match at the
    # end of that window means the framesync pattern ended at index i,
    # so the payload begins at i+1 — matching the hard path's
    # "position_after_pattern = i+1" convention.
    for i in range(FRAMESYNC_BITS - 1, n):
        w0 = i - FRAMESYNC_BITS + 1
        for mode in modes:
            bip = _FS_BIPOLAR[mode]
            s = 0.0
            for k in range(FRAMESYNC_BITS):
                s += soft_samples[w0 + k] * bip[k]
            norm = s / denom
            if norm >= min_norm_score:
                matches.append((i + 1, mode, norm))
    return matches


def find_framesync_soft_with_bitsync_gate(
    bits: List[int],
    soft_samples: List[float],
    amplitude_estimate: float,
    min_norm_score: float = 0.62,
    max_bs_hd: int = 8,
    gate_window_bits: int = 96,
    modes: Optional[List[int]] = None,
) -> List[Tuple[int, int, float]]:
    """Soft framesync, gated by a (hard) bitsync within the window.

    Bitsync stays hard-decision for now (48 bits, robust; soft bitsync
    is a conditional follow-up per the design doc, not a3 scope). This
    mirrors find_framesync_with_bitsync_gate but with the soft framesync
    candidate list.
    """
    bs_matches = find_bitsync(bits, max_bs_hd)
    if not bs_matches:
        return []
    bs_positions = sorted(set(p for p, _ in bs_matches))

    fs_matches = find_framesync_soft(
        soft_samples, amplitude_estimate, min_norm_score, modes)
    gated = []
    for fs_pos, mode, score in fs_matches:
        fs_start = fs_pos - FRAMESYNC_BITS
        for bs_pos in bs_positions:
            if 0 <= fs_start - bs_pos <= gate_window_bits:
                gated.append((fs_pos, mode, score))
                break
    return gated
