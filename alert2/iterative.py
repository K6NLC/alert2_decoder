# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
iterative.py — Iterative decoding extension (Phase 1: erasure-RS).

ALERT2 uses concatenated CC + RS without symbol interleaving. The
spec (§3.5.1) deliberately omits the interleaver because MLS bit
scrambling is applied. This means error patterns at the RS input
cluster geographically — a fade or chunk-boundary event affects
contiguous RS symbols rather than scattered ones. Erasure-aware RS
decoding helps most when errors are spread; with clustered errors,
the gain is smaller.

============================================================================
Phase 1 strategy: when standard hard-decision RS decode FAILS on a
packet, retry using libfec's existing erasure-aware decode with byte
positions marked as erasures based on demodulator confidence.

Reliability source: the absolute value of the quantized soft samples
from the demodulator. A bit at the demodulator output that's near
zero magnitude is one where the demodulator was uncertain
(matched-filter output ~midway between mark and space). A bit near
±SOFT_IDEAL is one where the demodulator was confident. We aggregate
bit-level confidence to byte-level by taking the MINIMUM across the
8 bits in each byte (a byte is no more confident than its weakest
bit).

This bypasses a true SOVA / per-bit Viterbi-derived reliability.
That's intentional for Phase 1:
  - It's simpler — no traceback-window machinery
  - It's faster — no extra Viterbi pass
  - It tests the same core hypothesis: "can erasure-RS recover
    packets that fail standard RS?"
  - If this produces no signal, a more sophisticated SOVA likely
    won't either (the failure mode would still be cluster-of-errors
    too dense for any RS variant)

The CC stage will spread bit errors via convolutional decoding, so
the byte-level reliability after Viterbi isn't perfectly aligned
with where errors actually land. But low-confidence bits going into
Viterbi correlate strongly with bit errors coming out of it. This
is a heuristic; we'll see if it works.
============================================================================

Per-block CPU cost: zero extra for packets that succeed standard
decode (most of them). One additional erasure-RS call per failed
block. The erasure-RS call is comparable in cost to standard RS
(microseconds). Net: close to free.

Phase 2 (NOT in this module): true iterative feedback — re-run
Viterbi with RS-corrected positions as constraints. Build only if
Phase 1 shows real gains.
"""

import ctypes
from typing import List, Optional, Tuple

from .constants import (
    FIRST_BLOCK_DATA, FOLLOWON_BLOCK_DATA, RS_PARITY,
)
from .fec import (
    SoftViterbi, SOFT_IDEAL, bits_to_bytes, descramble_block,
    _load_libfec, _get_rs_handle, rs_decode, depuncture_soft,
)


# Reliability threshold below which a soft bit is "uncertain".
# Soft samples are quantized to roughly [-SOFT_IDEAL .. +SOFT_IDEAL].
# A bit with |soft| < threshold is flagged. SOFT_IDEAL // 2 means
# "samples whose magnitude is less than half of fully-confident".
DEFAULT_RELIABILITY_THRESHOLD = SOFT_IDEAL // 2


def _bit_reliabilities_from_soft(soft_code_values: List[int],
                                   n_input_bits: int) -> List[int]:
    """Map per-coded-bit confidence (= |soft value|) back to per-input-bit
    confidence after Viterbi decoding.

    For rate-1/2 CC, each input bit affects 2 output coded bits (plus
    influence from convolutional memory). We approximate per-input-bit
    confidence as the MIN of the two coded-bit confidences at that input
    position. (For modes 1/2 with puncturing this would differ — Phase 1
    is mode 0 only, matching soft-V scope.)

    Args:
        soft_code_values: 2*N coded-bit soft values (signed; we use |·|)
        n_input_bits: number of input bits (excluding CC tail)
    Returns:
        list of length n_input_bits, each a non-negative confidence
    """
    out = []
    n_pairs = len(soft_code_values) // 2
    limit = min(n_input_bits, n_pairs)
    for i in range(limit):
        c0 = abs(soft_code_values[2 * i])
        c1 = abs(soft_code_values[2 * i + 1])
        out.append(min(c0, c1))
    # Pad if needed
    while len(out) < n_input_bits:
        out.append(0)
    return out


def _byte_reliabilities(bit_reliabilities: List[int]) -> List[int]:
    """Reduce per-bit reliability to per-byte reliability by MIN across
    the 8 bits of each byte. A byte is no more confident than its
    weakest bit.
    """
    out = []
    n_bits = len(bit_reliabilities)
    n_bytes = (n_bits + 7) // 8
    for byte_idx in range(n_bytes):
        s = byte_idx * 8
        e = min(s + 8, n_bits)
        out.append(min(bit_reliabilities[s:e]) if e > s else 0)
    return out


def _erasure_positions(byte_reliabilities: List[int],
                        threshold: int,
                        max_erasures: int) -> List[int]:
    """Pick byte positions to mark as erasures: lowest-confidence first,
    capped at max_erasures (=parity_len, the RS code's erasure capacity).
    """
    candidates = [(rel, pos) for pos, rel in enumerate(byte_reliabilities)
                  if rel < threshold]
    candidates.sort()
    return sorted([pos for _, pos in candidates[:max_erasures]])


def _rs_decode_with_erasures(codeword: bytes,
                              data_len: int,
                              parity_len: int,
                              erasure_positions: List[int]
                              ) -> Tuple[Optional[bytes], int]:
    """Wrapper around libfec's decode_rs_int with erasure positions."""
    libfec = _load_libfec()
    rs = _get_rs_handle(data_len, parity_len)
    arr = (ctypes.c_int * (data_len + parity_len))()
    for i, b in enumerate(codeword):
        arr[i] = b
    n_eras = len(erasure_positions)
    eras_pos = (ctypes.c_int * max(n_eras, parity_len))()
    for i, p in enumerate(erasure_positions):
        eras_pos[i] = p
    n = libfec.decode_rs_int(rs, arr, eras_pos, n_eras)
    if n < 0:
        return None, -1
    return bytes(arr[i] & 0xFF for i in range(data_len)), n


def decode_first_block_iterative(soft_cc_values,
                                  fec_mode: int = 0,
                                  reliability_threshold: Optional[int] = None
                                  ) -> Tuple[Optional[bytes], int, dict]:
    """Decode the first block: standard soft-V first; on RS failure,
    retry with erasures derived from demodulator confidence.

    Returns (descrambled_data, num_corrections, stats_dict).
    stats_dict.method: 'standard' | 'erasure' | 'failed'
    stats_dict.n_erasures: number of erasure positions used
    stats_dict.n_low_rel_bytes: byte positions below threshold
    """

    if reliability_threshold is None:
        reliability_threshold = DEFAULT_RELIABILITY_THRESHOLD

    # Run Viterbi (same as soft-V path)
    expected_total = FIRST_BLOCK_DATA[fec_mode] + RS_PARITY[fec_mode]
    v = SoftViterbi(fec_mode)
    soft_cc_values = depuncture_soft(soft_cc_values, fec_mode,
                                     expected_total * 8 + (v.k - 1))
    decoded_bits, _ = v.decode_soft(soft_cc_values, expect_tail=True)
    rs_codeword = bits_to_bytes(decoded_bits)

    if len(rs_codeword) < expected_total:
        return None, -1, {'method': 'failed', 'n_erasures': 0,
                          'n_low_rel_bytes': 0}
    rs_codeword = rs_codeword[:expected_total]

    # Compute byte reliabilities from demod-level confidence
    # Number of input bits to Viterbi = expected_total * 8 (we feed
    # input bits then 6 tail bits get appended internally)
    n_input_bits = expected_total * 8
    bit_rels = _bit_reliabilities_from_soft(soft_cc_values, n_input_bits)
    byte_rels = _byte_reliabilities(bit_rels)
    byte_rels = byte_rels[:expected_total]
    n_low_rel = sum(1 for r in byte_rels if r < reliability_threshold)

    # Try standard RS first (errors-only)
    rs_data, n_corr = rs_decode(rs_codeword, FIRST_BLOCK_DATA[fec_mode],
                                 RS_PARITY[fec_mode])

    if rs_data is not None:
        return (descramble_block(rs_data), n_corr,
                {'method': 'standard', 'n_erasures': 0,
                 'n_low_rel_bytes': n_low_rel})

    # Standard failed — try erasure-aware
    max_erasures = RS_PARITY[fec_mode]
    erasures = _erasure_positions(byte_rels, reliability_threshold,
                                   max_erasures)
    if not erasures:
        return None, -1, {'method': 'failed', 'n_erasures': 0,
                          'n_low_rel_bytes': n_low_rel}

    rs_data, n_corr = _rs_decode_with_erasures(
        rs_codeword, FIRST_BLOCK_DATA[fec_mode],
        RS_PARITY[fec_mode], erasures)

    if rs_data is None:
        return None, -1, {'method': 'failed',
                          'n_erasures': len(erasures),
                          'n_low_rel_bytes': n_low_rel}

    return (descramble_block(rs_data), n_corr,
            {'method': 'erasure', 'n_erasures': len(erasures),
             'n_low_rel_bytes': n_low_rel})


def decode_followon_stream_iterative(soft_cc_values,
                                      fec_mode: int,
                                      total_data_bytes: int,
                                      reliability_threshold: Optional[int] = None
                                      ) -> Tuple[Optional[bytes], int, dict]:
    """Follow-on stream: same per-block strategy as the first block."""
    if total_data_bytes <= 0:
        return b'', 0, {'method': 'standard', 'n_erasures': 0,
                        'n_low_rel_bytes': 0}

    if reliability_threshold is None:
        reliability_threshold = DEFAULT_RELIABILITY_THRESHOLD

    full_block_data = FOLLOWON_BLOCK_DATA[fec_mode]
    parity = RS_PARITY[fec_mode]

    n_full_blocks = total_data_bytes // full_block_data
    final_block_size = total_data_bytes % full_block_data
    if final_block_size == 0:
        block_sizes = [full_block_data] * n_full_blocks
    else:
        block_sizes = [full_block_data] * n_full_blocks + [final_block_size]

    total_rs_bytes = sum(b + parity for b in block_sizes)

    v = SoftViterbi(fec_mode)
    soft_cc_values = depuncture_soft(soft_cc_values, fec_mode,
                                     total_rs_bytes * 8 + (v.k - 1))
    decoded_bits, _ = v.decode_soft(soft_cc_values, expect_tail=True)
    rs_stream = bits_to_bytes(decoded_bits)
    if len(rs_stream) < total_rs_bytes:
        return None, -1, {'method': 'failed', 'n_erasures': 0,
                          'n_low_rel_bytes': 0}
    rs_stream = rs_stream[:total_rs_bytes]

    n_input_bits = total_rs_bytes * 8
    bit_rels = _bit_reliabilities_from_soft(soft_cc_values, n_input_bits)
    byte_rels_full = _byte_reliabilities(bit_rels)

    out_data = bytearray()
    total_corr = 0
    offset = 0
    stats = {'method': 'standard', 'n_erasures': 0, 'n_low_rel_bytes': 0}
    for block_size in block_sizes:
        codeword_size = block_size + parity
        codeword = rs_stream[offset:offset + codeword_size]
        block_byte_rels = byte_rels_full[offset:offset + codeword_size]
        stats['n_low_rel_bytes'] += sum(1 for r in block_byte_rels
                                         if r < reliability_threshold)
        offset += codeword_size

        rs_data, n_corr = rs_decode(codeword, block_size, parity)
        if rs_data is None:
            erasures = _erasure_positions(block_byte_rels,
                                            reliability_threshold,
                                            parity)
            if not erasures:
                return None, -1, {'method': 'failed',
                                   'n_erasures': stats['n_erasures'],
                                   'n_low_rel_bytes': stats['n_low_rel_bytes']}
            rs_data, n_corr = _rs_decode_with_erasures(
                codeword, block_size, parity, erasures)
            if rs_data is None:
                return None, -1, {'method': 'failed',
                                   'n_erasures': stats['n_erasures'] + len(erasures),
                                   'n_low_rel_bytes': stats['n_low_rel_bytes']}
            stats['method'] = 'erasure'
            stats['n_erasures'] += len(erasures)

        out_data.extend(descramble_block(rs_data))
        total_corr += n_corr

    return bytes(out_data), total_corr, stats
