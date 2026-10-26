# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
iterative_sova.py — Canonical-SOVA-based erasure-RS decoding.

Parallel to alert2.iterative (Phase 1), but uses canonical SOVA
per-bit reliability instead of raw demodulator-level confidence. The
difference: SOVA's reliability reflects the Viterbi trellis's view of
how close the competing paths got — so it captures cases where the
demodulator was confident about a sample but the underlying bit
decision was actually marginal (e.g., a strong-but-wrong sample at a
critical position that flipped the survivor away from the true path).

API matches alert2.iterative exactly: decode_first_block_iterative_sova
and decode_followon_stream_iterative_sova return (data, n_corr, stats).
"""

import ctypes
from typing import List, Optional, Tuple

from .constants import (
    FIRST_BLOCK_DATA, FOLLOWON_BLOCK_DATA, RS_PARITY,
)
from .fec import (
    SOFT_IDEAL, bits_to_bytes, descramble_block,
    _load_libfec, _get_rs_handle, rs_decode,
)
from .sova import canonical_sova


# Reliability threshold: SOVA produces values in a different range than
# raw soft-sample magnitude. From the validation runs, clean signals
# give ~120 and heavy noise gives ~20. A threshold of SOFT_IDEAL*8 = 48
# is a sensible starting point — "any byte whose worst bit had a SOVA
# reliability gap less than ~8 fully-confident-symbol-distances is
# erasure-candidate".
#
# This is a Phase-1.5 starting point. The actually-optimal threshold
# is properly chosen by paired A/B. We expose it as a parameter.
DEFAULT_SOVA_THRESHOLD = SOFT_IDEAL * 8  # = 48 at SOFT_IDEAL=6


def _byte_reliabilities_from_bits(bit_reliabilities: List[int]) -> List[int]:
    """Per-byte reliability = MIN over its 8 bits."""
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
    """Pick byte positions to mark as erasures."""
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


def decode_first_block_sova(soft_cc_values,
                             fec_mode: int = 0,
                             reliability_threshold: Optional[int] = None,
                             max_erasures: Optional[int] = None
                             ) -> Tuple[Optional[bytes], int, dict]:
    """SOVA-based iterative decode for the first block.

    max_erasures bounds how many low-reliability bytes may be declared as
    erasures for the RS retry. None = full parity budget (RS_PARITY), the
    least-safe setting. Lower values (e.g. 16-32 for mode 0) sharply reduce
    fabrication rate at the cost of fewer recoveries — see field analysis
    2026-06-21. This is the direct analog of --max-fs-hd for the erasure
    stage: it turns SOVA "down" rather than off.
    """
    if reliability_threshold is None:
        reliability_threshold = DEFAULT_SOVA_THRESHOLD

    sova = canonical_sova(soft_cc_values, fec_mode=fec_mode, expect_tail=True)
    rs_codeword = bits_to_bytes(sova.bits)

    expected_total = FIRST_BLOCK_DATA[fec_mode] + RS_PARITY[fec_mode]
    if len(rs_codeword) < expected_total:
        return None, -1, {'method': 'failed', 'n_erasures': 0,
                          'n_low_rel_bytes': 0}
    rs_codeword = rs_codeword[:expected_total]

    byte_rels = _byte_reliabilities_from_bits(sova.bit_reliability)
    byte_rels = byte_rels[:expected_total]
    n_low_rel = sum(1 for r in byte_rels if r < reliability_threshold)

    rs_data, n_corr = rs_decode(rs_codeword, FIRST_BLOCK_DATA[fec_mode],
                                 RS_PARITY[fec_mode])

    if rs_data is not None:
        return (descramble_block(rs_data), n_corr,
                {'method': 'standard', 'n_erasures': 0,
                 'n_low_rel_bytes': n_low_rel,
                 'max_block_corr': (n_corr if n_corr and n_corr > 0 else 0)})

    eras_cap = RS_PARITY[fec_mode] if max_erasures is None else \
        min(max_erasures, RS_PARITY[fec_mode])
    erasures = _erasure_positions(byte_rels, reliability_threshold,
                                   eras_cap)
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
             'n_low_rel_bytes': n_low_rel,
             'max_block_corr': (n_corr if n_corr and n_corr > 0 else 0)})


def decode_followon_stream_sova(soft_cc_values,
                                 fec_mode: int,
                                 total_data_bytes: int,
                                 reliability_threshold: Optional[int] = None,
                                 max_erasures: Optional[int] = None
                                 ) -> Tuple[Optional[bytes], int, dict]:
    """SOVA-based iterative decode for the follow-on stream.

    max_erasures: per-block cap on declared erasures (None = full parity).
    See decode_first_block_sova for rationale.
    """
    if total_data_bytes <= 0:
        return b'', 0, {'method': 'standard', 'n_erasures': 0,
                        'n_low_rel_bytes': 0}
    if reliability_threshold is None:
        reliability_threshold = DEFAULT_SOVA_THRESHOLD

    full_block_data = FOLLOWON_BLOCK_DATA[fec_mode]
    parity = RS_PARITY[fec_mode]

    n_full_blocks = total_data_bytes // full_block_data
    final_block_size = total_data_bytes % full_block_data
    if final_block_size == 0:
        block_sizes = [full_block_data] * n_full_blocks
    else:
        block_sizes = [full_block_data] * n_full_blocks + [final_block_size]

    total_rs_bytes = sum(b + parity for b in block_sizes)

    sova = canonical_sova(soft_cc_values, fec_mode=fec_mode, expect_tail=True)
    rs_stream = bits_to_bytes(sova.bits)
    if len(rs_stream) < total_rs_bytes:
        return None, -1, {'method': 'failed', 'n_erasures': 0,
                          'n_low_rel_bytes': 0}
    rs_stream = rs_stream[:total_rs_bytes]

    byte_rels_full = _byte_reliabilities_from_bits(sova.bit_reliability)

    out_data = bytearray()
    total_corr = 0
    offset = 0
    stats = {'method': 'standard', 'n_erasures': 0, 'n_low_rel_bytes': 0,
             'max_block_corr': 0}
    for block_size in block_sizes:
        codeword_size = block_size + parity
        codeword = rs_stream[offset:offset + codeword_size]
        block_byte_rels = byte_rels_full[offset:offset + codeword_size]
        stats['n_low_rel_bytes'] += sum(1 for r in block_byte_rels
                                         if r < reliability_threshold)
        offset += codeword_size

        rs_data, n_corr = rs_decode(codeword, block_size, parity)
        block_corr = n_corr if n_corr is not None and n_corr >= 0 else 0
        if rs_data is None:
            eras_cap = parity if max_erasures is None else \
                min(max_erasures, parity)
            erasures = _erasure_positions(block_byte_rels,
                                            reliability_threshold,
                                            eras_cap)
            if not erasures:
                return None, -1, {'method': 'failed',
                                   'n_erasures': stats['n_erasures'],
                                   'n_low_rel_bytes': stats['n_low_rel_bytes'],
                                   'max_block_corr': stats['max_block_corr']}
            rs_data, n_corr = _rs_decode_with_erasures(
                codeword, block_size, parity, erasures)
            if rs_data is None:
                return None, -1, {'method': 'failed',
                                   'n_erasures': stats['n_erasures'] + len(erasures),
                                   'n_low_rel_bytes': stats['n_low_rel_bytes'],
                                   'max_block_corr': stats['max_block_corr']}
            stats['method'] = 'erasure'
            stats['n_erasures'] += len(erasures)
            # libfec's decode_rs_int return value ALREADY includes the
            # erasure count (it returns total symbols corrected = errors +
            # erasures), so do NOT add len(erasures) again.
            block_corr = n_corr if n_corr is not None and n_corr >= 0 else 0

        if block_corr > stats['max_block_corr']:
            stats['max_block_corr'] = block_corr

        out_data.extend(descramble_block(rs_data))
        total_corr += n_corr

    return bytes(out_data), total_corr, stats
