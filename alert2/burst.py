"""Burst-scoped decoding (rc23).
# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later


Why this exists
---------------
`demod.demodulate()` fits ONE symbol-clock phase and ONE amplitude
estimate per call, and `find_best_phase()` picks that phase from only
the first `n_test` (8000) samples — 0.185 s at 43.2 kHz. For a 6-second
chunk that is the first 3%, which at typical traffic density contains
no signal at all. Every burst in the window is then decimated at a
phase fitted to noise.

Field measurement (32 real shop bursts, 2026-07): each burst's own
optimal phase is spread across 8 of the 9 possible values, and
successive transmissions from the SAME station land on different
phases (4615: 0, 8, 3, 7, 3, 7). This is expected — each transmitter's
clock is independent of the receiver's sound card, so arrival time
modulo the symbol period is arbitrary. No window-level estimate can be
right for more than a fraction of the bursts inside it.

`--multiphase` is the existing mitigation: decode the whole chunk at
N ranked phases and hope one is close. It earns 7% of packets at the
shop and 20-27% at the QTH, which measures how wrong the single chunk
phase usually is. It costs N x a full chunk decode to sample ~N/9 of
the phase space.

This module does it exactly instead: detect the burst, extract just
that burst from the chunk audio, and demodulate the extract — so the
phase and amplitude are fitted to the burst itself. Measured on real
shop audio degraded to the failure threshold: burst-scoped with ONE
phase recovered 6/6 where chunk-scoped with FOUR multiphases recovered
4/6.

Superset safety
---------------
Burst-scoped decoding is a per-candidate re-decode. It cannot remove a
packet the chunk pass found: callers merge its output with the primary
pass rather than replacing it. Acceptance is unchanged — plain
unforced RS through the same ladder (eq -> DD -> progressive), the
same structural filters, and the same dedup.
"""

import logging
from typing import List, Optional

import numpy as np

from .fec import first_block_cc_bits_padded, followon_cc_bits_padded
from .constants import (SAMPLES_PER_BIT, RX_SAMPLE_RATE, FIRST_BLOCK_DATA,
                        FOLLOWON_BLOCK_DATA, RS_PARITY, AIRLINK_HEADER_BYTES,
                        BITSYNC_BITS, FRAMESYNC_BITS)

logger = logging.getLogger(__name__)

# Bits of audio to include before the framesync end. The preamble
# (bitsync + framesync) precedes fs_end and the equalizer trains on the
# last 80 bits of it, so the extract must contain it with slack.
PRE_BITS = BITSYNC_BITS + FRAMESYNC_BITS + 96

# Slack after the computed frame end, for filter tails and timing slop.
POST_BITS = 96

# Used when the first block cannot be read at detection time and the
# frame length is therefore unknown. 256 payload bytes is generous for
# any observed traffic while staying far below the 1023 B spec max, so
# an unknown-length extract stays cheap. A frame longer than this that
# also fails its detection-stage first-block decode simply falls back
# to the chunk pass, which is no worse than today.
UNKNOWN_LEN_BYTES = 256


def frame_bit_length(airlink_len: int, fec_mode: int) -> int:
    """Total on-air bits for a frame, from its AirLink payload length."""
    m = fec_mode if fec_mode in FIRST_BLOCK_DATA else 0
    ba = max(0, airlink_len - (FIRST_BLOCK_DATA[m] - AIRLINK_HEADER_BYTES))
    full = FOLLOWON_BLOCK_DATA[m]
    nf, fin = divmod(ba, full)
    total_rs = (FIRST_BLOCK_DATA[m] + RS_PARITY[m]) \
        + nf * (full + RS_PARITY[m]) \
        + ((fin + RS_PARITY[m]) if fin else 0)
    # Convolutional coding at the mode's rate (first block and follow-on
    # stream each carry a tail), plus a pad allowance.
    fo_rs = total_rs - (FIRST_BLOCK_DATA[m] + RS_PARITY[m])
    n = first_block_cc_bits_padded(m)
    if fo_rs > 0:
        n += followon_cc_bits_padded(fo_rs, m)
    return n + 48


def bit_to_sample(bit_idx: int, phase: int, fs: int) -> int:
    """Map a bit index in the demodulated stream to a sample index in
    the ORIGINAL-rate audio the demod consumed."""
    idx_43k = phase + bit_idx * SAMPLES_PER_BIT
    return int(idx_43k * fs / float(RX_SAMPLE_RATE))


def extract_burst(audio: np.ndarray, fs: int, fs_end_bit: int,
                  phase: int, frame_bits: int):
    """Slice the audio region occupied by one burst.

    Returns (segment, ok). `ok` is False when the burst is not fully
    contained in this audio (it will be caught by the neighbouring
    chunk, since overlap guarantees containment somewhere).
    """
    start_bit = fs_end_bit - PRE_BITS
    end_bit = fs_end_bit + frame_bits + POST_BITS
    if start_bit < 0:
        return None, False
    lo = bit_to_sample(start_bit, phase, fs)
    hi = bit_to_sample(end_bit, phase, fs)
    if lo < 0 or hi > len(audio):
        return None, False
    if hi - lo < SAMPLES_PER_BIT * 64:
        return None, False
    return audio[lo:hi], True


def decode_bursts(audio: np.ndarray, fs: int, decoder, candidates,
                  phase: int, quantized_by_pol: dict,
                  demodulate_fn) -> List:
    """Re-decode each detected burst with per-burst demodulation.

    Args:
        audio: the chunk's audio at original sample rate.
        fs: original sample rate.
        decoder: an Alert2Decoder (its flags/ladder are reused as-is).
        candidates: iterable of (fs_end_bit, fec_mode, soft_inverted,
            airlink_len_or_None) or the 5-tuple form with a trailing
            per-candidate phase. The 5-tuple is used by the all-phase
            collector (stage 2): candidates then come from DIFFERENT
            phases, so each carries the phase its fs_end_bit is relative
            to. Without it, bit->sample conversion would use the wrong
            origin and the extract would be cut in the wrong place.
        phase: the detection demod's symbol phase, used as the default
            when a candidate does not carry its own.
        quantized_by_pol: unused placeholder for future reuse of the
            detection-stage soft values; kept for call-site stability.
        demodulate_fn: injected demodulate (keeps this module testable
            without importing the whole demod stack at call time).

    Returns a list of packets. Each carries burst_scoped=True.
    """
    out = []
    seen_positions = set()
    for cand in candidates:
        if len(cand) == 5:
            fs_end, fec_mode, inverted, airlink_len, cand_phase = cand
        else:
            fs_end, fec_mode, inverted, airlink_len = cand
            cand_phase = phase
        # Collapse candidates that refer to the same burst: the same
        # transmission is routinely reported at slightly different
        # offsets by different polarity/threshold paths.
        # Absolute-sample key: fs_end is relative to cand_phase, so two
        # candidates from different phases describing the SAME burst have
        # different fs_end values. Keying on the phase-relative bit index
        # would treat them as distinct (and re-decode the burst N times)
        # while also risking merging genuinely different bursts.
        pos_key = (((cand_phase + fs_end * SAMPLES_PER_BIT)
                    // (SAMPLES_PER_BIT * 16)), fec_mode, inverted)
        if pos_key in seen_positions:
            continue
        seen_positions.add(pos_key)

        n_bits = frame_bit_length(
            airlink_len if airlink_len else UNKNOWN_LEN_BYTES, fec_mode)
        seg, ok = extract_burst(audio, fs, fs_end, cand_phase, n_bits)
        if not ok:
            continue
        try:
            b_seg, info_seg = demodulate_fn(seg, fs)
        except Exception as e:
            logger.debug(f"burst demod failed at bit {fs_end}: {e}")
            continue

        soft = info_seg.get('soft_samples')
        amp = info_seg.get('amplitude_estimate')
        bits_use = [1 - x for x in b_seg] if inverted else b_seg
        try:
            # The burst pass re-demodulates the extracted segment, so its
            # matched-filter output and phase belong to THIS burst. Handing
            # them to the timing probe measures how well the burst-scoped
            # phase estimate actually landed — the number needed to decide
            # whether per-burst timing should replace the phase brute force.
            pkts = decoder.decode_bits(
                bits_use, soft_samples=soft, amplitude_estimate=amp,
                filtered_43k=(info_seg.get('filtered_43k')
                              if getattr(decoder, 'timing_probe', False)
                              else None),
                bit_phase=info_seg.get('best_phase', 0),
                soft_inverted=inverted, presync_search=False)
        except Exception as e:
            logger.debug(f"burst decode failed at bit {fs_end}: {e}")
            continue
        for p in pkts:
            try:
                p.burst_scoped = True
            except Exception:
                pass
            out.append(p)
    return out
