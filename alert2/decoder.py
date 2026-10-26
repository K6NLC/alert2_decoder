# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Main ALERT2 decoder pipeline.

Combines demod, sync detection, FEC, and packet parsing.
"""
import time
import logging
from typing import List, Optional, Iterator
import numpy as np
import scipy.io.wavfile as wav

from .candidate_gate import admit as _gate_admit
from .constants import (
    FIRST_BLOCK_DATA, FOLLOWON_BLOCK_DATA, RS_PARITY,
    AIRLINK_HEADER_BYTES, SAMPLES_PER_BIT,
    BITSYNC_HEX, BITSYNC_BITS, FRAMESYNCS, FRAMESYNC_BITS,
)
from .demod import demodulate, demodulate_simple
from .sync import (
    find_framesync, find_framesync_with_bitsync_gate,
    find_framesync_soft, find_framesync_soft_with_bitsync_gate,
    soft_score_to_synthetic_hd,
)
from .fec import (
    decode_first_block, decode_followon_block, Viterbi,
    decode_first_block_soft, decode_followon_stream_soft, quantize_soft,
    decode_followon_stream_soft_blocks,
    scramble_block, rs_encode, conv_encode,
    first_block_cc_bits, first_block_cc_bits_padded,
    followon_cc_bits, followon_cc_bits_padded,
)
from .iterative import (
    decode_first_block_iterative, decode_followon_stream_iterative,
)
from .iterative_sova import (
    decode_first_block_sova, decode_followon_stream_sova,
)
from .types import Alert2Packet, AirLinkHeader, MantHeader

logger = logging.getLogger('alert2')


# ---------------------------------------------------------------------------
# Preamble-trained equalizer (--eq-retry), added rc15.
#
# Every ALERT2 burst begins with 80 KNOWN bits (48-bit bitsync 0xEB90B433AAAA
# + 32-bit framesync word, MSB-first — convention verified at hd=0 against
# live bursts). That is a free per-burst training sequence: fit a short FIR
# by least squares mapping the received soft symbols onto the known +/-1
# targets, then apply it across the packet and retry the STANDARD soft
# decode. Acceptance is by plain unforced RS, so an equalized recovery is
# exactly as trustworthy as any off-mode decode — zero fabrication risk by
# construction. Field prototype (2026-07-01, QTH TK790 captures): rescued
# src=797/762 follow-ons on 6-13 (the LONGEST packets — the chain's
# length-dependent truncation victims) and the src=735 first block +
# follow-on on 6-14; zero regressions in fallback use; recoveries disjoint
# from gated-SOVA's (the two stack).
# ---------------------------------------------------------------------------

_EQ_PREAMBLE_TARGETS = {}


def _eq_preamble_targets(fec_mode: int):
    t = _EQ_PREAMBLE_TARGETS.get(fec_mode)
    if t is None:
        def wb(w, n):
            return [(w >> (n - 1 - i)) & 1 for i in range(n)]
        known = (wb(BITSYNC_HEX, BITSYNC_BITS)
                 + wb(FRAMESYNCS[fec_mode], FRAMESYNC_BITS))
        t = np.array([1.0 if b else -1.0 for b in known])
        _EQ_PREAMBLE_TARGETS[fec_mode] = t
    return t


def _train_preamble_eq(raw_soft, fs_end: int, fec_mode: int, taps: int = 5):
    """Least-squares FIR fit on the 80 known preamble symbols ending at
    fs_end. Returns tap weights, or None if not trainable."""
    n = BITSYNC_BITS + FRAMESYNC_BITS
    if fs_end < n or raw_soft is None:
        return None
    y = np.asarray(raw_soft[fs_end - n:fs_end], dtype=float)
    if len(y) < n or not np.any(y):
        return None
    h = taps // 2
    yp = np.pad(y, (h, h), mode='edge')
    X = np.array([yp[i:i + taps] for i in range(n)])
    try:
        w, _, _, _ = np.linalg.lstsq(X, _eq_preamble_targets(fec_mode),
                                     rcond=None)
    except np.linalg.LinAlgError:
        return None
    return w


def _apply_preamble_eq(raw_soft, w, start: int, length: int):
    """Apply trained FIR to raw_soft[start:start+length] (edge-padded).
    Output is scaled to +/-1 targets, so quantize with amplitude 1.0."""
    taps = len(w)
    h = taps // 2
    lo = max(0, start - h)
    seg = np.asarray(raw_soft[lo:start + length + h], dtype=float)
    pre = h - (start - lo)
    post = max(0, (start + length + h) - len(raw_soft))
    if pre > 0 or post > 0:
        seg = np.pad(seg, (max(pre, 0), post), mode='edge')
    out = np.convolve(seg, w[::-1], mode='valid')
    return out[:length].tolist()


def _train_dd_eq(raw_soft, fs_end: int, first_data: bytes, fec_mode: int,
                 taps: int = 5):
    """Decision-directed equalizer training (rc17).

    Once the first block has decoded, its bytes are KNOWN — re-encode
    them through the exact transmit path (scramble -> RS -> CC) to
    recover the ~652 transmitted symbols occupying the burst region
    immediately BEFORE the follow-on, and LS-fit the FIR on those.
    This captures channel state much closer to the follow-on than the
    80-bit preamble does; field case: the 6-14 src=4615 follow-on that
    preamble-eq could not recover decoded with RS_corr=0 under the
    decision-directed filter (content cross-validated against the same
    gauge's July readings: Stage 8.1179 vs 8.1635 a month later).
    Acceptance downstream remains plain unforced RS.
    """
    data_len = FIRST_BLOCK_DATA[fec_mode]
    if first_data is None or len(first_data) < data_len:
        return None
    try:
        cw = rs_encode(scramble_block(bytes(first_data[:data_len])),
                       RS_PARITY[fec_mode])
    except Exception:
        return None
    rb = []
    for byte in cw:
        for i in range(7, -1, -1):
            rb.append((byte >> i) & 1)
    kb = conv_encode(rb, fec_mode=fec_mode, append_tail=True)
    n = len(kb)
    if raw_soft is None or fs_end + n > len(raw_soft):
        return None
    y = np.asarray(raw_soft[fs_end:fs_end + n], dtype=float)
    if not np.any(y):
        return None
    t = np.array([1.0 if b else -1.0 for b in kb])
    h = taps // 2
    yp = np.pad(y, (h, h), mode='edge')
    X = np.array([yp[i:i + taps] for i in range(n)])
    try:
        w, _, _, _ = np.linalg.lstsq(X, t, rcond=None)
    except np.linalg.LinAlgError:
        return None
    return w


def _reencode_blocks(data: bytes, block_sizes, parity: int):
    """Re-encode decoded follow-on data blocks back to their transmitted
    RS-codeword bit stream (scramble -> RS parity), MSB-first."""
    bits = []
    off = 0
    for bs in block_sizes:
        blk = data[off:off + bs]
        off += bs
        cw = rs_encode(scramble_block(bytes(blk)), parity)
        for byte in cw:
            for i in range(7, -1, -1):
                bits.append((byte >> i) & 1)
    return bits


def _mer_reference_segments(first_data: bytes, followon_data, fec_mode: int,
                            fs_end: int):
    """Rebuild the TRANSMITTED symbol stream from decoded bytes, for MER.

    Runs decoded data back through the exact transmit path
    (scramble -> RS -> CC), the same reconstruction _train_dd_eq() uses to
    train the decision-directed equalizer. The result is the ground-truth
    symbol sequence the transmitter emitted, so the MER residual contains
    only channel impairment — never decision noise.

    Returns (segments, coverage) where segments is a list of
    (start_index_in_raw_soft, bits) and coverage is:
        'full'        — first block and follow-on both reconstructed
        'first-block' — only the first block is known (truncated packets,
                        or a frame with no follow-on)
    Returns ([], None) when the first block is unavailable, because there
    is then no honest reference at all.

    The few pad symbols between the first block's CC tail and the start of
    the follow-on stream are deliberately NOT included: their transmitted
    value is not known (the gap is 4 symbols at fec_mode 0 and 0 at
    fec_mode 1), and inventing one would be charged to the error term.
    """
    data_len = FIRST_BLOCK_DATA[fec_mode]
    if first_data is None or len(first_data) < data_len:
        return [], None
    parity = RS_PARITY[fec_mode]
    try:
        cw = rs_encode(scramble_block(bytes(first_data[:data_len])), parity)
    except Exception:
        return [], None
    rb = []
    for byte in cw:
        for i in range(7, -1, -1):
            rb.append((byte >> i) & 1)
    # First block is CC-encoded with its own tail (spec §3.5.1).
    first_bits = list(conv_encode(rb, fec_mode=fec_mode, append_tail=True))
    segments = [(fs_end, first_bits)]
    coverage = 'first-block'

    if followon_data:
        # Follow-on blocks form ONE CC stream with a single trailing tail
        # (spec §3.5.2). Block sizes must match how the stream was cut or
        # the reference desynchronises and MER collapses to noise.
        full = FOLLOWON_BLOCK_DATA[fec_mode]
        n = len(followon_data)
        sizes = [full] * (n // full)
        if n % full:
            sizes.append(n % full)
        try:
            fo_rb = _reencode_blocks(bytes(followon_data), sizes, parity)
            fo_bits = list(conv_encode(fo_rb, fec_mode=fec_mode,
                                       append_tail=True))
        except Exception:
            return segments, coverage      # keep the honest first-block ref
        # Same offset the follow-on decoder uses, so the reference lands on
        # the symbols the follow-on was actually read from.
        first_block_total = data_len + parity
        offset_in_bits = first_block_cc_bits_padded(fec_mode)
        segments.append((fs_end + offset_in_bits, fo_bits))
        coverage = 'full'
    return segments, coverage


def _mer_after_ideal_eq(raw_soft, segments, taps: int = 5):
    """MER of the same symbols after an ideal linear equalizer.

    Decomposes the MER residual. Plain MER is measured on raw_soft, i.e.
    the channel AS RECEIVED, before any equalizer rung runs. This variant
    LS-fits a `taps`-tap FIR against the known transmitted symbols — the
    best a linear equalizer of that length could possibly do — and
    remeasures. The difference (eq_gain_db) is the part of the residual
    that is linear ISI and therefore removable; what remains is noise and
    non-linear distortion, which no FIR will recover.

    This is deliberately NOT "the MER the ladder achieved". The ladder's
    rungs equalize different regions with differently-trained filters, and
    a rung that never fired would report nothing. Measuring the ideal
    bound instead gives one number that is always defined whenever MER is,
    and answers the useful question: was this packet hurt by ISI (which
    --eq-retry can address) or by noise (which it cannot)?

    The filter is trained on the FIRST segment only and applied to all of
    them, so on multi-block frames most of the measurement is out-of-sample.
    On single-block frames it is in-sample; with 5 taps against >=652
    symbols the optimistic bias is small (measured ~0.1 dB) but it is a
    bias, so eq_gain_db should be read as a slight upper bound.

    Returns the same dict shape as measure_symbol_mer, or None.
    """
    from .quality import measure_symbol_mer
    if raw_soft is None or not segments:
        return None
    start0, bits0 = segments[0]
    w = _train_eq_on_symbols(raw_soft, start0, bits0, taps=taps,
                             last_n=len(bits0))
    if w is None:
        return None
    eq_segments = []
    offset = 0
    flat = []
    for start, bits in segments:
        if bits is None:
            continue
        n = len(bits)
        if start < 0 or start + n > len(raw_soft):
            continue
        try:
            eq = _apply_preamble_eq(raw_soft, w, start, n)
        except Exception:
            return None
        flat.extend(eq)
        eq_segments.append((offset, bits))
        offset += n
    if not eq_segments:
        return None
    return measure_symbol_mer(flat, eq_segments)


def _train_eq_on_symbols(raw_soft, start: int, known_bits, taps: int = 5,
                         last_n: int = 400):
    """LS-fit a FIR on a run of known transmitted symbols."""
    n = len(known_bits)
    if raw_soft is None or start + n > len(raw_soft) or n < taps * 4:
        return None
    y = np.asarray(raw_soft[start:start + n], dtype=float)
    t = np.array([1.0 if b else -1.0 for b in known_bits])
    if n > last_n:
        y = y[-last_n:]
        t = t[-last_n:]
    if not np.any(y):
        return None
    h = taps // 2
    yp = np.pad(y, (h, h), mode='edge')
    X = np.array([yp[i:i + taps] for i in range(len(y))])
    try:
        w, _, _, _ = np.linalg.lstsq(X, t, rcond=None)
    except np.linalg.LinAlgError:
        return None
    return w


def _dd_eq_progressive(raw_soft, fs_end: int, first_data: bytes,
                       fec_mode: int, bytes_after_first: int,
                       st: int, ln: int, max_iters: int = 6):
    """Progressive decision-directed equalizer (rc21).

    Generalizes rc17's DD-eq for long multi-block frames: the channel
    drifts over the burst, so a filter trained at block 1 is stale by
    block 4-5 (field case: 798/734/739 concentrated 130B frames running
    RS_corr~8). Iteration: equalize the remaining region with the
    current filter, decode as far as RS allows, re-encode the decoded
    prefix (true transmitted symbols), retrain the filter on the tail
    of that prefix — the freshest channel state available — and retry
    the remainder. Every block still passes plain unforced RS; the
    honest-partial guarantee is unchanged (no progress -> caller keeps
    its truncated packet).

    Returns (data, per_block_corr, blocks_ok, n_blocks) — data is full
    on success, partial prefix otherwise.
    """
    parity = RS_PARITY[fec_mode]
    full = FOLLOWON_BLOCK_DATA[fec_mode]
    nf, fin = divmod(bytes_after_first, full)
    block_sizes = [full] * nf + ([fin] if fin else [])
    n_blocks = len(block_sizes)
    if n_blocks == 0 or raw_soft is None or st + ln > len(raw_soft):
        return None, [], 0, n_blocks, 0

    w = _train_dd_eq(raw_soft, fs_end, first_data, fec_mode)
    if w is None:
        return None, [], 0, n_blocks, 0

    # piecewise-equalized soft buffer for the follow-on region
    eq_soft = list(_apply_preamble_eq(raw_soft, w, st, ln))
    best = (b'', [], 0)
    iters = 0
    for _ in range(max_iters):
        iters += 1
        q = quantize_soft(eq_soft, 1.0)
        data, per_block, blocks_ok = decode_followon_stream_soft_blocks(
            q, fec_mode, bytes_after_first)
        if blocks_ok >= n_blocks:
            return data, per_block, blocks_ok, n_blocks, iters
        if blocks_ok <= best[2]:
            break  # no progress — stop honestly
        best = (data, per_block, blocks_ok)
        # retrain on the freshest decoded prefix and re-equalize the rest
        prefix_sizes = block_sizes[:blocks_ok]
        known = _reencode_blocks(data, prefix_sizes, parity)
        prefix_syms = len(known) * 2  # CC rate 1/2, no tail mid-stream
        w2 = _train_eq_on_symbols(
            raw_soft, st, conv_encode(known, fec_mode=fec_mode,
                                      append_tail=False))
        if w2 is None:
            break
        tail_len = ln - prefix_syms
        if tail_len <= 0:
            break
        eq_soft[prefix_syms:] = _apply_preamble_eq(
            raw_soft, w2, st + prefix_syms, tail_len)
    d, pb, ok = best
    return (d if d else None), pb, ok, n_blocks, iters


class Alert2Decoder:
    """Main decoder. Process a bit stream, produce packets."""

    def __init__(self, max_framesync_hd: int = 4,
                 require_bitsync: bool = False,
                 fec_modes: Optional[List[int]] = None,
                 soft_viterbi: bool = False,
                 soft_framesync: bool = False,
                 soft_framesync_threshold: float = 0.62,
                 iterative='off',
                 max_erasures: Optional[int] = None,
                 max_rs_corr: Optional[int] = None,
                 sova_max_fs_hd: Optional[int] = None,
                 sova_max_rs_corr: Optional[int] = None,
                 sova_max_rs_corr_per_block: Optional[int] = None,
                 eq_retry: bool = False,
                 presync_eq: bool = False,
                 presync_min_bursts: int = 20,
                 presync_max_fs_hd: Optional[int] = None,
                 mer_mode: str = 'full',
                 mer_sample: int = 1,
                 timing_probe: bool = False,
                 candidate_gate: float = 0.0,
                 output=None):
        # MER cost control. Measured on a 3-frame mix: MER alone is +6.2%
        # end-to-end, MER + the ideal-equalizer variant is +37.2% (the LS
        # fit and FIR apply dominate). On a Pi already at decode_rt 0.898x
        # the full mode lands at ~1.23x, i.e. an unbounded backlog, so it
        # has to be tunable rather than always-on.
        #   mer_mode: 'off' | 'basic' (MER only) | 'full' (adds MER_eq)
        #   mer_sample: measure 1 frame in N. Statistics over a day need
        #     only a sample; 1-in-10 costs ~0.6% / ~3.7% respectively.
        if mer_mode not in ('off', 'basic', 'full'):
            raise ValueError(f"mer_mode must be off|basic|full, got {mer_mode!r}")
        self.mer_mode = mer_mode
        self.mer_sample = max(1, int(mer_sample))
        self._mer_frame_counter = 0
        # Per-burst timing probe (see timing_probe.py). Observation only:
        # it never alters detection, decoding, or acceptance. Enabling it
        # costs one ~80-symbol interpolation per decoded burst.
        self.timing_probe = bool(timing_probe)
        # Preamble-correlation gate on framesync candidates. 0.0 = off.
        # See candidate_gate.py: this can only REMOVE decode attempts, so
        # it is off by default and fails open on any unscoreable candidate.
        self.candidate_gate = float(candidate_gate or 0.0)
        self._gate_stats = {'seen': 0, 'rejected': 0}
        self._filtered_43k = None
        self._bit_phase = 0
        self._timing_probe = False

        # NOTE on defaults (v0.9.0rc1): soft_viterbi defaults to False
        # *here* deliberately. The production default-ON policy for
        # soft-Viterbi lives in the CLI layer (cli.py
        # _SOFT_VITERBI_DEFAULT), NOT in this constructor. Rationale:
        # keep the library API stable/backward-compatible and explicit
        # (a programmatic caller opts in), while letting the deployed
        # application (python -m alert2) apply the field-proven policy.
        # If you drive Alert2Decoder directly, pass soft_viterbi=True
        # to match CLI behavior.
        """
        Args:
            max_framesync_hd: max Hamming distance for framesync match (0-8)
            require_bitsync: only accept framesyncs preceded by bitsync (more strict)
            fec_modes: which FEC modes to look for (default: only mode 0)
            soft_viterbi: if True, use soft-decision Viterbi (requires
                          soft_samples to be passed to decode_bits;
                          falls back to hard if they're absent). FEC
                          mode 0 only — see docs/SOFT_VITERBI.md.
            soft_framesync: if True, detect framesync by soft matched-
                          filter correlation instead of Hamming distance
                          (requires soft_samples; falls back to hard if
                          absent). Independent of soft_viterbi — the two
                          compose. See docs/SOFT_FRAMESYNC.md.
            soft_framesync_threshold: min normalized correlation score
                          for a soft framesync match (default 0.62).
            output: optional PacketOutput (or MultiOutput) for stats hooks
                    — note_attempt() will be called for each framesync candidate
        """
        self.max_fs_hd = max_framesync_hd
        self.max_erasures = max_erasures
        self.max_rs_corr = max_rs_corr
        self.sova_max_fs_hd = sova_max_fs_hd
        self.sova_max_rs_corr = sova_max_rs_corr
        self.sova_max_rs_corr_per_block = sova_max_rs_corr_per_block
        self.eq_retry = eq_retry
        self.presync_eq = presync_eq
        self.presync_min_bursts = presync_min_bursts
        self.presync_max_fs_hd = presync_max_fs_hd
        # Per-polarity packet counters (primary pass) for presync
        # polarity gating: don't burn a second framesync search on a
        # polarity that has never produced a packet.
        self._pol_pkts = {False: 0, True: 0}
        # Presync-eq self-calibration state (running sum + count of
        # per-burst preamble-trained FIR weights)
        self._presync_w_sum = None
        self._presync_w_n = 0
        self.require_bitsync = require_bitsync
        self.fec_modes = fec_modes if fec_modes is not None else [0]
        self.soft_viterbi = soft_viterbi
        self.soft_framesync = soft_framesync
        self.soft_framesync_threshold = soft_framesync_threshold
        # Normalize iterative: bool True -> 'demod' (back-compat),
        # bool False -> 'off', or pass through 'off'/'demod'/'sova'.
        if iterative is True:
            self.iterative = 'demod'
        elif iterative is False:
            self.iterative = 'off'
        elif iterative in ('off', 'demod', 'sova'):
            self.iterative = iterative
        else:
            raise ValueError(
                f"iterative must be 'off', 'demod', 'sova', or bool; "
                f"got {iterative!r}")
        self.output = output  # For stats collection

    def _exceeds_correction_gates(self, pkt) -> bool:
        """Return True if this packet should be rejected by the tunable
        RS-correction / framesync-HD gates (instance-level, since these
        thresholds are configurable per run; the static
        _is_likely_false_positive holds the portable spec-conformance
        patterns).

        Three independent, additive gates (all default off / None):

        --max-rs-corr (self.max_rs_corr): a GENERAL cap on total RS
            corrections for ANY decode (standard or erasure). Coarse
            backstop against pathological over-correction. Field data
            (2026-06-15..22, ~13k packets) shows real STANDARD decodes
            never exceeded RS_corr=19, so a value ~32+ rejects no real
            standard traffic while catching gross anomalies.

        --sova-max-fs-hd (self.sova_max_fs_hd) and
        --sova-max-rs-corr (self.sova_max_rs_corr): the validated
            COMBINED gate for ERASURE recoveries specifically. On
            2026-06-21 and 2026-06-22 (63 fabrications, 37 real
            recoveries across two days), requiring framesync hd <= 5
            AND RS_corr <= 32 for an erasure recovery gave ZERO
            fabrications while keeping ~90% of real recoveries, with a
            clean margin (real maxed at RS=32, fabrications started at
            RS=63 after the HD gate). Fabrications fail BOTH conditions
            (noise framesync at high HD + heavy RS correction); real
            recoveries pass both. The conjunction is the discriminator —
            neither lever alone separates cleanly.
        """
        # rs_corrections is a flat integer total (first_corr + followon_corr),
        # not a per-block list. Handle both defensively.
        try:
            rc = pkt.rs_corrections
            if rc is None:
                rs_total = 0
            elif isinstance(rc, int):
                rs_total = rc
            else:
                rs_total = sum(rc)
        except (AttributeError, TypeError):
            rs_total = 0
        fs_hd = getattr(pkt, 'framesync_hd', None)
        is_erasure = getattr(pkt, 'iter_method', '') == 'erasure'

        # General RS cap (all decodes)
        if self.max_rs_corr is not None and rs_total > self.max_rs_corr:
            return True

        # SOVA-specific combined gate (erasure recoveries only).
        # Validated 2026-06-21/22 across 63 fabrications + ~330 real
        # packets: the zero-fabrication gate is FS_hd <= 5 AND total
        # RS_corr <= 32. Empirically, real ALERT2 packets need very
        # little RS correction (overwhelmingly 0 or single digits; max
        # observed real = 63 total, almost all <= 31), while
        # fabrications are noise forced through RS and cluster at high
        # correction (>= 32, mostly 64-144). Heavy RS correction is the
        # strongest single fabrication signature; the HD gate is the
        # complementary co-gate that catches the rare low-RS / high-HD
        # fabrication. Both gates are content/quality-based (no station
        # IDs), hence portable across ALERT2 systems.
        if is_erasure:
            if self.sova_max_fs_hd is not None and fs_hd is not None \
                    and fs_hd > self.sova_max_fs_hd:
                return True
            # Flat-total RS cap (the validated discriminator).
            if self.sova_max_rs_corr is not None \
                    and rs_total > self.sova_max_rs_corr:
                return True
            # Optional per-block RS cap (portable, size-invariant
            # co-gate; off unless set). A per-block value bounds the
            # worst single block's correction load regardless of how
            # many blocks the packet spans — useful on deployments with
            # larger packets where a flat total scales with block count.
            if self.sova_max_rs_corr_per_block is not None:
                mbc = getattr(pkt, 'iter_max_block_corr', 0) or 0
                if mbc > self.sova_max_rs_corr_per_block:
                    return True

        return False

    @staticmethod
    def _mant_walk_consistent(payload: bytes) -> bool:
        """Dry-run walk of stacked MANT PDUs over an AirLink payload.

        Returns False if the walk hits a structural inconsistency: an
        unparseable MANT header, a bad version, or a PDU claiming more
        bytes than remain. Used to validate SOVA erasure-recovered
        follow-on bytes BEFORE committing to them: an erasure "success"
        whose bytes produce an inconsistent MANT structure is
        untrustworthy (recovered garbage), and the caller falls back to
        the honest truncated partial instead. A clean/empty payload
        walks consistently.
        """
        offset = 0
        while offset < len(payload):
            remaining = payload[offset:]
            if all(b == 0x55 for b in remaining):
                break
            try:
                m = MantHeader.parse(remaining)
            except ValueError:
                return False
            if m.version > 2:
                return False
            size = m.header_bytes + m.payload_length
            if size > len(remaining):
                return False
            if size <= 0:
                return False
            offset += size
        return True

    @staticmethod
    def _is_likely_false_positive(pkt) -> bool:
        """Return True if this packet looks like a false-positive
        erasure-RS decode that should not be emitted.

        Four known false-positive patterns from field data, all
        conditional on iter_method='erasure' (standard-RS decodes
        are never filtered):

        Pattern A (qth10 pattern, 2026-05-26):
          iter=erasure AND truncated=True
          Mechanism: erasure-RS recovered "some" RS-consistent codeword
          but the follow-on block also failed; valid AirLink+MANT
          headers but garbage source IDs and measurement bytes. All 6
          known false positives in qth10 paired A/B fit this pattern.

        Pattern B (shop pattern, 2026-05-27):
          iter=erasure AND mant.port=0 AND srp_pdu=None
          Mechanism: erasure-RS produced valid AirLink+MANT headers
          on Port 0 (SRP), but the application bytes failed to parse
          as a valid SRP report. Among 205 legitimate erasure decodes,
          204 (99.5%) had valid SRP parses; among 19 unknown-source
          erasure decodes, 14 (74%) failed SRP parse.

        Pattern D (junk-with-known-ID pattern, 2026-05-30):
          iter=erasure AND srp_pdu contains an UnknownReport
          OR iter=erasure AND srp_pdu.test_flag is True
          Mechanism: erasure-RS produced output that parsed as SRP
          but contained unrecognized report types (e.g. Type65,
          Type174 — values that don't exist in the spec) or had
          the TEST flag set (test traffic, not normal operation).
          Among 5 days of shop data: catches ~57/77 junk decodes
          (74%) with known source IDs; zero legitimate decodes lost
          (real ALERT2 traffic uses only TBRG, GSR, MSR-*).

        Pattern E (extreme correction ratio, 2026-05-30):
          iter=erasure AND RS_corr/airlink_len > 1.0
          A very extreme correction effort relative to packet size.
          Catches 2 extreme junk cases that escape Pattern D, with
          zero legitimate cost in 5 days of data.

        Future-proofing note: Pattern D will need updating if the
        ALERT2 spec adds new report types (e.g. Type 6 or higher).
        Standard-RS decodes with unknown types are NOT filtered, on
        the assumption that those are likely real (new firmware
        features) rather than noise.
        """
        # Pattern K (2026-07-12, src=62181 field case): the MANT header
        # claims more payload bytes than the AirLink header's own length
        # field could ever contain. A genuine TRUNCATED packet claims
        # more than *received* (bytes lost to RS failure) but never more
        # than the airlink promised (735's partial: claim 41B within a
        # 46B airlink). claim > airlink.length is structurally
        # impossible, so this applies to ALL decode types — it caught a
        # STANDARD-decode false positive (hd=6, RS=5, claim 671B vs
        # 210B airlink) that the erasure-gated patterns cannot touch.
        try:
            if (pkt.mant is not None and pkt.airlink is not None
                    and pkt.mant.header_bytes + pkt.mant.payload_length
                        > pkt.airlink.length):
                return True
        except (AttributeError, TypeError):
            pass

        # Standard-RS decodes are never filtered — they're high-confidence
        if pkt.iter_method != 'erasure':
            return False

        # Pattern A: erasure + truncated
        if pkt.truncated:
            return True

        # Pattern B: erasure on SRP port (0) without parseable SRP payload
        if pkt.mant is not None and pkt.mant.port == 0 and pkt.srp_pdu is None:
            return True

        # Pattern D: erasure with parsed-but-suspect SRP content
        if pkt.srp_pdu is not None:
            # D.1: any unrecognized report type
            # (UnknownReport from alert2.srp captures all Type<N> with
            # N not in the known set per Application spec §2.4)
            try:
                from .srp import UnknownReport
                for report in pkt.srp_pdu.reports:
                    if isinstance(report, UnknownReport):
                        return True
            except ImportError:
                pass
            # D.2: TEST flag set on an erasure-decoded packet
            # (real network traffic is not in TEST mode)
            if getattr(pkt.srp_pdu, 'test_flag', False):
                return True

            # Pattern F (2026-06-02): unparseable sensor readings inside
            # an otherwise-recognized GSR/MSR report.
            #
            # Mechanism: erasure-RS fills a corrupted block with bytes
            # that parse as a valid report TYPE (GSR/MSR — caught neither
            # by Pattern B nor D) but contain sensor readings with
            # unrecognized format codes. The SRP value decoder falls
            # through to its "unknown/reserved" branch and tags the
            # reading value_type 'fmt_0x<n>_len_<m>' (optionally with a
            # unit suffix for MSR), or 'truncated'/'utf8-error'. These
            # render as hex blobs (e.g. sensor_151=e2f46e7e2fb9eb8e) and
            # impossible values (Stage=140194697101).
            #
            # Field evidence (tkr_audio TK790 capture, 2026-06-02):
            # this pattern caught 8/8 garbage erasure decodes in
            # iterative mode and 8/8 in SOVA mode, while leaving every
            # legitimate erasure recovery (all-clean readings, confirmed
            # against independent clean-path decodes) untouched.
            #
            # Limitation: does NOT catch garbage where the format code
            # happens to be valid but the value is out of physical range
            # (e.g. Battery Voltage=1.5e9 with a recognized format). That
            # would require per-sensor range tables — domain logic that
            # risks rejecting genuine sensor glitches. Pattern E catches
            # some of those via the RS-ratio; the rest are accepted.
            for report in pkt.srp_pdu.reports:
                for reading in getattr(report, 'readings', []):
                    vt = getattr(reading, 'value_type', '') or ''
                    if ('fmt_0x' in vt or 'truncated' in vt
                            or 'utf8-error' in vt):
                        return True

        # Pattern E: extreme RS correction ratio
        # (correction work disproportionate to packet size — only
        # the most extreme noise-derived codewords hit this)
        try:
            airlink_len = pkt.airlink.length if pkt.airlink else 0
            rs_total = sum(pkt.rs_corrections) if pkt.rs_corrections else 0
            if airlink_len > 0 and rs_total / airlink_len > 1.0:
                return True
        except (AttributeError, TypeError):
            pass

        # Pattern G (SRP timestamp out of range, 2026-06-21):
        #   iter=erasure AND srp has a timestamp outside [0, 43199].
        #   Spec basis: the SRP timestamp is "seconds since most recent
        #   12:00" (Application Layer); 12 hours = 43200 seconds, so a
        #   valid value is 0..43199 inclusive. A value >= 43200 is
        #   impossible per spec and indicates a fabricated/corrupted
        #   timestamp field. Spec-intrinsic (no deployment knowledge),
        #   so portable across ALERT2 systems. Independently flags the
        #   757/port-10 garbage case observed in the 6-13 TK790 capture.
        #   NOTE: this only rejects IMPOSSIBLE values; a fabrication whose
        #   ts happens to land in-range (~66% of random 16-bit values)
        #   still passes G — it is a backstop, not a primary gate.
        if pkt.srp_pdu is not None and getattr(pkt.srp_pdu, 'has_timestamp', False):
            ts = getattr(pkt.srp_pdu, 'timestamp', None)
            if ts is not None and not (0 <= ts <= 43199):
                return True

        # Pattern H (SRP APDU/PDU id out of normal range, 2026-06-21):
        #   iter=erasure AND srp pdu_id outside the 0..6 reporting cycle.
        #   Spec/observed basis: the Application PDU ID "typically
        #   increments from 0 to 6 then returns to 0" (A2Control manual);
        #   the field is 3 bits (structural max 7). We reject only
        #   pdu_id == 7 (or any value >7, which cannot occur in 3 bits),
        #   i.e. the structurally-possible-but-unused value, since real
        #   traffic cycles 0..6. Conservative: does not reject 0..6.
        #   Spec-intrinsic, portable. Weak individually (a fabrication has
        #   ~6/8 chance of landing in 0..6), so a backstop, not a gate.
        # (No APDU-id range pattern: the App PDU ID is a 3-bit field, so
        #  every value 0..7 is structurally valid. The manual notes it
        #  "typically" cycles 0..6, but 7 is not illegal and appears as a
        #  default/edge value in legitimate packets, so an id-range reject
        #  would cause false rejections without reliably catching
        #  fabrications. Evaluated 2026-06-21 and intentionally omitted.)

        # Pattern I (invalid MANT port, 2026-06-21):
        #   iter=erasure AND mant.port is not a spec-defined data port.
        #   Spec basis (MANT §2.2.6): the 4-bit Port field has only
        #   known data values 0x0 (SRP / ALERT2 data) and 0x1
        #   (Concentration / legacy ALERT); the IND Config & Control
        #   protocol uses 0x80 (not representable in 4 bits). Any other
        #   4-bit port value is undefined by the spec.
        #   Mechanism: erasure-RS fabrications can land valid AirLink+MANT
        #   framing but on an undefined port (e.g. port=10 observed on
        #   src 757 in the 6-13 TK790 capture), producing no parseable
        #   SRP — so Patterns B/D/F/G/H (all SRP-dependent) do NOT fire and
        #   the packet escapes. This closes that gap with a spec-intrinsic,
        #   portable check. Conservative: only rejects ports outside the
        #   defined set, so SRP (0) and Concentration (1) are never hit.
        if pkt.mant is not None and pkt.mant.port not in (0, 1):
            return True

        # Pattern J (per-report-type spec-conformance, 2026-06-21):
        #   iter=erasure AND any decoded report violates the structural
        #   rules the ALERT2 Application spec (v1.3) defines for its type.
        #   These are SPEC-INTRINSIC checks (no station/deployment
        #   knowledge), so portable across ALERT2 systems. Designed to ride
        #   alongside --max-erasures: the cap bounds how hard RS is pushed;
        #   this catches structurally-impossible content the cap lets
        #   through. Each sub-check rejects only spec-illegal structure,
        #   never merely-unusual values, so legitimate traffic is unaffected.
        if pkt.srp_pdu is not None and getattr(pkt.srp_pdu, 'reports', None):
            # Spec-defined format codes (Value Format/Length high nibble,
            # §2.2.3): 0x1 uint, 0x2 signed, 0x3 float, 0x4 UTF-8,
            # 0xD-0xF special (FP2 etc.). 0x0 and 0x5-0xC are Reserved.
            VALID_FMT = {0x1, 0x2, 0x3, 0x4, 0xD, 0xE, 0xF}
            for rpt in pkt.srp_pdu.reports:
                tname = type(rpt).__name__

                if tname == 'GeneralSensorReport':
                    # GSR (Type 1): each reading's format code must be a
                    # spec-defined value, and the raw value bytes must match
                    # the declared length. A reserved/0 format or a
                    # length/bytes mismatch is structurally illegal.
                    for rd in getattr(rpt, 'readings', []):
                        if getattr(rd, 'is_timestamp', False):
                            continue
                        fmt = getattr(rd, 'format_code', None)
                        ln = getattr(rd, 'length', None)
                        rb = getattr(rd, 'raw_bytes', b'') or b''
                        if fmt is not None and fmt not in VALID_FMT:
                            return True
                        if ln is not None and len(rb) != ln:
                            return True
                        # length nibble is 0-15 bytes (§2.2.3); 0 with a
                        # non-timestamp reading is malformed.
                        if ln == 0:
                            return True

                elif tname == 'MultiSensorReport':
                    # MSR (Type 3/4/5): fixed structure. data_flags is one
                    # byte (0-255); the number of decoded readings must not
                    # exceed the 8 defined sensor slots, and must match the
                    # set bits in data_flags (each flag = one included
                    # field, §2.4.4). A mismatch means the fixed layout did
                    # not parse as spec-defined.
                    df = getattr(rpt, 'data_flags', None)
                    rds = getattr(rpt, 'readings', [])
                    if df is None or not (0 <= df <= 255):
                        return True
                    if len(rds) > 8:
                        return True
                    if df is not None and bin(df).count('1') != len(rds):
                        return True

                elif tname == 'TippingBucketReport':
                    # TBRG (Type 2): accumulator format code must be a
                    # spec-defined Format/Length value, and each tip Time
                    # Offset is a 1-byte field = seconds before cycle end
                    # (§2.3.4), so 0-255. Accumulator must be non-negative
                    # (§2.3.3: "positively increasing").
                    fmt = getattr(rpt, 'format_code', None)
                    if fmt is not None and fmt not in VALID_FMT:
                        return True
                    if getattr(rpt, 'accumulator', 0) < 0:
                        return True
                    for off in getattr(rpt, 'tip_offsets', []):
                        if not (0 <= off <= 255):
                            return True

        return False

    def decode_bits(self, bits: List[int],
                    audio_43k: Optional['np.ndarray'] = None,
                    bit_phase: int = 0,
                    soft_samples: Optional[List[float]] = None,
                    amplitude_estimate: Optional[float] = None,
                    soft_inverted: bool = False,
                    presync_search: bool = True,
                    candidates: Optional[list] = None,
                    filtered_43k: Optional['np.ndarray'] = None,
                    timing_probe: Optional[bool] = None) -> List[Alert2Packet]:
        """Decode all packets found in a bit stream.

        Strategy:
          1. Find all framesync candidates
          2. For each, try to decode a packet starting after the framesync
          3. Filter out duplicates (same packet detected by multiple paths)

        If audio_43k is provided (the resampled audio at 43.2 kHz used for
        demodulation), SNR / signal level is measured for each packet.
        bit_phase is the sample offset where bit 0 begins in audio_43k.

        Soft-decision support:
          soft_samples: per-bit float soft values, index-aligned with
                        `bits` (same length, same order). Only used when
                        self.soft_viterbi is True.
          amplitude_estimate: nominal symbol amplitude |A| for quantization.
          soft_inverted: if True, the caller inverted bit polarity
                         (`[1-b for b in bits]`); the soft values must be
                         negated to match. (A strong "1" becomes a strong
                         "0".)

        If soft_viterbi is on but soft_samples is None, falls back to the
        hard path with a one-time debug log (defensive — should not happen
        in normal operation).
        """
        # Timing probe context (observation only). Held on the instance so
        # _try_decode_at can reach it without changing its signature.
        # The probe correlates against the un-inverted filtered stream, so
        # it must know which polarity pass produced these bits.
        self._soft_inverted_now = bool(soft_inverted)
        want = self.timing_probe if timing_probe is None else timing_probe
        self._filtered_43k = filtered_43k
        self._bit_phase = bit_phase
        self._timing_probe = bool(want and filtered_43k is not None)

        # --- Framesync candidate selection ---
        # soft_framesync uses the same soft_samples array as soft_viterbi
        # (index-aligned with bits), negated when soft_inverted so the
        # inverted-polarity pass works identically to the hard path.
        # Falls back to hard framesync if soft samples are unavailable.
        soft_fs_ok = (
            self.soft_framesync
            and soft_samples is not None
            and len(soft_samples) == len(bits)
            and amplitude_estimate is not None)

        # Per-burst phase selection supplies its own candidate list, found
        # and ranked across ALL phases beforehand. Re-scanning here would
        # rediscover the same bursts at this one phase and undo the saving,
        # so the scan is skipped entirely when candidates are injected.
        _injected = candidates is not None
        if _injected:
            pass
        elif soft_fs_ok:
            fs_soft_src = soft_samples
            if soft_inverted:
                fs_soft_src = [-x for x in soft_samples]
            if self.require_bitsync:
                soft_cands = find_framesync_soft_with_bitsync_gate(
                    bits, fs_soft_src, amplitude_estimate,
                    min_norm_score=self.soft_framesync_threshold,
                    max_bs_hd=8, modes=self.fec_modes)
            else:
                soft_cands = find_framesync_soft(
                    fs_soft_src, amplitude_estimate,
                    min_norm_score=self.soft_framesync_threshold,
                    modes=self.fec_modes)
            # Map norm_score -> synthetic integer HD so the downstream
            # loop, dedup, stats, diag and JSON framesync_hd field all
            # stay unchanged. The richer score is preserved separately
            # for diag text via _soft_fs_scores.
            self._soft_fs_scores = {}
            candidates = []
            for fs_pos, mode, score in soft_cands:
                hd = soft_score_to_synthetic_hd(score)
                candidates.append((fs_pos, mode, hd))
                self._soft_fs_scores[(fs_pos, mode)] = score
        else:
            self._soft_fs_scores = None
            if self.soft_framesync and not soft_fs_ok:
                logger.debug(
                    "soft_framesync enabled but soft_samples missing or "
                    "length-mismatched; falling back to hard framesync")
            if self.require_bitsync:
                candidates = find_framesync_with_bitsync_gate(
                    bits, max_fs_hd=self.max_fs_hd, max_bs_hd=8)
            else:
                candidates = find_framesync(
                    bits, max_hd=self.max_fs_hd, modes=self.fec_modes)

        # Sort by position
        candidates.sort(key=lambda x: x[0])

        # --- Presync-eq (rc16): detection-only pre-filter ---
        # Once self-calibrated (running average of per-burst preamble-
        # trained FIR weights from this decoder's own successful
        # decodes), run the framesync search a second time on the
        # equalized bit stream and merge in any candidates the raw
        # stream missed. Detection only: decode always proceeds from
        # the ORIGINAL samples at the found offsets, so the validated
        # decode chain is untouched. Rationale (validated on QTH TK790
        # captures): the chain's distortion inflates real packets'
        # framesync HD; pre-equalization moves them down 2-3 HD points
        # (1->0, 3->0, 4->1), so packets that needed max_fs_hd=6-7 (and
        # its noise-candidate CPU explosion) are found at hd<=4-5.
        presync_only = set()
        presync_raw_hd = {}
        # presync_search=False on multiphase re-decodes (rc19): the
        # presync convolution + second framesync pass must run ONCE per
        # chunk (primary pass), not once per phase candidate — running
        # it inside every multiphase decode_bits call multiplied the
        # search ~10-18x per chunk and pegged the Pi 4 the moment
        # calibration completed. Weight collection is gated the same
        # way: multiphase passes hand decode_bits pre-negated soft with
        # soft_inverted=True (double-negation in the soft paths), so
        # weights trained there are wrong-sign AND duplicate — they
        # polluted the average and tripped the warmup threshold after
        # only 2-3 real bursts.
        # Polarity gate (rc20): once any polarity has produced packets,
        # only run presync on productive polarities. Halves presync cost
        # on single-polarity chains (e.g. TK790 always-inverted).
        _pol_active = (sum(self._pol_pkts.values()) == 0
                       or self._pol_pkts.get(soft_inverted, 0) > 0)
        if (presync_search
                and _pol_active
                and self.presync_eq
                and self._presync_w_n >= self.presync_min_bursts
                and soft_samples is not None
                and len(soft_samples) == len(bits)):
            pw = self._presync_w_sum / self._presync_w_n
            psrc = soft_samples if not soft_inverted \
                else [-x for x in soft_samples]
            ph = len(pw) // 2
            peqs = np.convolve(
                np.pad(np.asarray(psrc, dtype=float), (ph, ph),
                       mode='edge'),
                pw[::-1], mode='valid')
            peq_bits = [1 if x > 0 else 0 for x in peqs]
            # Lower HD threshold for the eq-stream search (rc20): real
            # packets sync 2-3 HD points lower post-equalization
            # (measured 1->0, 2->0, 3->0/1, 4->1), so the second search
            # doesn't need the raw threshold — default max_fs_hd-1
            # catches everything measured while admitting ~6x fewer
            # noise candidates (the candidate-decode cost is what pegged
            # the Pi at the raw threshold).
            _ps_hd = self.presync_max_fs_hd
            if _ps_hd is None:
                _ps_hd = max(1, self.max_fs_hd - 1)
            if self.require_bitsync:
                pcands = find_framesync_with_bitsync_gate(
                    peq_bits, max_fs_hd=_ps_hd, max_bs_hd=8)
            else:
                pcands = find_framesync(
                    peq_bits, max_hd=_ps_hd,
                    modes=self.fec_modes)
            have = [(fe, m) for fe, m, _ in candidates]
            added = 0
            presync_raw_hd = {}
            for fe, m, phd in pcands:
                if m not in self.fec_modes:
                    continue
                if any(mm == m and abs(fe - fe0) <= 2 for fe0, mm in have):
                    continue
                candidates.append((fe, m, phd))
                presync_only.add((fe, m))
                # rc21: record the RAW-stream HD at this offset — how far
                # above the raw threshold the packet actually sat.
                if fe >= FRAMESYNC_BITS:
                    _w = 0
                    for _i, _b in enumerate(bits[fe - FRAMESYNC_BITS:fe]):
                        _w = (_w << 1) | _b
                    presync_raw_hd[(fe, m)] = bin(
                        _w ^ FRAMESYNCS[m]).count('1')
                added += 1
            if added:
                candidates.sort(key=lambda x: x[0])
                logger.debug(
                    f"presync-eq revealed {added} additional framesync "
                    f"candidate(s) (weights from "
                    f"{self._presync_w_n} bursts)")

        # Prepare quantized soft values once (if using soft Viterbi).
        # Quantization is global — it only depends on the per-burst
        # amplitude estimate, not on framesync position — so we do it
        # once here rather than per-candidate.
        quantized_soft = None
        use_soft = False
        if self.soft_viterbi:
            if soft_samples is None or len(soft_samples) != len(bits):
                logger.debug(
                    "soft_viterbi enabled but soft_samples missing or "
                    "length-mismatched (have %s, bits %s); falling back "
                    "to hard Viterbi for this stream",
                    None if soft_samples is None else len(soft_samples),
                    len(bits))
            else:
                src = soft_samples
                if soft_inverted:
                    src = [-x for x in soft_samples]
                quantized_soft = quantize_soft(src, amplitude_estimate)
                use_soft = True

        packets = []
        seen_payloads = set()  # For deduplication

        for fs_end, fec_mode, fs_hd in candidates:
            if fec_mode not in self.fec_modes:
                continue
            # Cheap rejection BEFORE the Viterbi. Framesync HD cannot
            # separate real bursts from noise (real packets occur at raw
            # HD 7-11), but the 80-bit preamble can: measured across four
            # captures, real bursts scored >= 0.610 and spurious
            # candidates <= 0.559.
            if self.candidate_gate > 0.0 and use_soft:
                self._gate_stats['seen'] += 1
                if not _gate_admit(src, fs_end, fec_mode,
                                   self.candidate_gate,
                                   inverted=False):
                    self._gate_stats['rejected'] += 1
                    continue
            decoded_pkts = self._try_decode_at(
                bits, fs_end, fec_mode, fs_hd,
                quantized_soft=quantized_soft if use_soft else None,
                raw_soft=src if use_soft else None)
            # Notify stats collector of every framesync attempt
            if self.output is not None:
                try:
                    self.output.note_attempt(fec_mode, fs_hd,
                                              bool(decoded_pkts))
                except Exception:
                    pass
                # Notify of the framesync event for live display / failure
                # logging. note_attempt() above is for stats counting; this
                # passes the actual decoded packets (post-filter) so display
                # outputs can show source IDs.
                try:
                    self.output.note_framesync_event(
                        fec_mode, fs_hd, decoded_pkts, fs_end)
                except Exception:
                    pass
            if not decoded_pkts:
                continue

            for pkt in decoded_pkts:
                # Measure signal quality if audio is available
                if audio_43k is not None:
                    try:
                        from .quality import measure_packet_snr
                        snr = measure_packet_snr(audio_43k, bit_phase, fs_end)
                        if snr is not None:
                            pkt.snr_db = snr['snr_db']
                            pkt.noise_rms = snr['noise_rms']
                            pkt.signal_rms = snr['signal_rms']
                    except Exception as e:
                        logger.debug(f"SNR measurement failed: {e}")

                # Deduplicate by payload bytes
                key = (pkt.fec_mode, bytes(pkt.raw_payload))
                if key in seen_payloads:
                    continue
                seen_payloads.add(key)
                if (fs_end, fec_mode) in presync_only:
                    pkt.presync_found = True
                    pkt.presync_raw_hd = presync_raw_hd.get(
                        (fs_end, fec_mode))
                packets.append(pkt)
                # Presync-eq self-calibration: every successful decode
                # contributes its preamble-trained FIR weights to the
                # running average. Converges within ~presync_min_bursts
                # packets of stream start, per site, automatically.
                if presync_search:
                    self._pol_pkts[soft_inverted] = \
                        self._pol_pkts.get(soft_inverted, 0) + 1
                if self.presync_eq and use_soft and presync_search:
                    _pw = _train_preamble_eq(src, fs_end, fec_mode)
                    if _pw is not None:
                        if self._presync_w_sum is None:
                            self._presync_w_sum = np.asarray(_pw, dtype=float)
                        else:
                            self._presync_w_sum = self._presync_w_sum + _pw
                        self._presync_w_n += 1
                logger.debug(f"Decoded packet at bit {fs_end}: {pkt}")

        return packets

    def _attach_mer(self, pkt) -> None:
        """Copy this frame's MER onto a packet. Never raises, never gates."""
        t0 = getattr(self, '_timing', None)
        if t0:
            pkt.timing_offset = t0.get('offset')
            pkt.timing_gain = t0.get('gain')
        m = getattr(self, '_mer', None)
        if not m:
            return
        pkt.mer_db = m.get('mer_db')
        pkt.mer_evm_pct = m.get('evm_pct')
        pkt.mer_symbols = m.get('n_symbols')
        pkt.mer_coverage = m.get('coverage')
        pkt.mer_alpha = m.get('alpha')
        t = getattr(self, '_timing', None)
        if t:
            pkt.timing_offset = t.get('offset')
            pkt.timing_gain = t.get('gain')
        pkt.mer_eq_db = m.get('mer_eq_db')
        pkt.mer_eq_gain_db = m.get('eq_gain_db')
        pkt.mer_act_db = m.get('mer_act_db')

    def _try_decode_at(self, bits: List[int], fs_end: int,
                       fec_mode: int, fs_hd: int,
                       quantized_soft: Optional[List[int]] = None,
                       raw_soft: Optional[List[float]] = None,
                       force_no_sova_followon: bool = False
                       ) -> List[Alert2Packet]:
        """Try to decode a packet starting just after framesync.

        Returns a list of Alert2Packet objects: usually one, but may be
        multiple when an AirLink frame contains stacked MANT PDUs.

        If quantized_soft is provided, the convolutional stage uses
        soft-decision Viterbi. quantized_soft is index-aligned with
        `bits` (same length), so it is sliced at the identical offsets.
        FEC mode 0 only; for other modes we fall back to hard even if
        soft values are present (soft Viterbi for modes 1/2 is not
        implemented — see docs/SOFT_VITERBI.md).
        """
        soft_ok = quantized_soft is not None and fec_mode == 0
        viterbi_mode = 'soft' if soft_ok else 'hard'
        # First-block convolutional bit count for FEC mode 0:
        # 24 data + 16 parity = 40 RS bytes = 320 bits
        # 320 input bits * 2 (rate 1/2) + 12 tail bits = 652 conv bits
        # In practice the spec says "encode with 2-byte tail" = 16 tail bits,
        # giving 640 + 16 = 656 conv bits = 82 bytes — but actual encoded
        # tail is 12 bits, with 4 padding bits to byte-align.
        first_block_data = FIRST_BLOCK_DATA[fec_mode]
        first_block_total = first_block_data + RS_PARITY[fec_mode]
        # CC encoded with 2-byte tail field (per spec §3.5.2):
        # 16 tail bits where last 4 are padding zeros
        first_cc_bits = first_block_cc_bits_padded(fec_mode)

        if fs_end + first_cc_bits > len(bits):
            return []

        first_cc = bits[fs_end : fs_end + first_cc_bits]

        # Try Viterbi decode of first block
        # Need to decode 320 input bits + 6 tail bits = 326 input bits
        # That's 652 cc bits + 4 padding bits in the spec's 656 total
        # We pass 656 to Viterbi but expect_tail handles the 6-bit tail
        iter_first_stats = None
        # Preamble-equalizer state (--eq-retry): trained lazily, once per
        # candidate, reused for first block and follow-on. eq_used marks
        # any packet whose bytes came from an equalized (unforced-RS)
        # retry, for diagnostics.
        eq_w = None
        eq_used = False
        eq_stage = None
        followon_block_corr = None
        if soft_ok:
            soft_first = quantized_soft[
                fs_end : fs_end + first_block_cc_bits(fec_mode)]
            if self.iterative == 'demod':
                first_data, first_corr, iter_first_stats = \
                    decode_first_block_iterative(soft_first, fec_mode)
            elif self.iterative == 'sova':
                # OFF-FIRST semantics (2026-07-01): always attempt the
                # exact standard soft decode first — identical to
                # iterative='off' — so gated SOVA can never lose a packet
                # that off would have decoded. (Field finding on the 6-14
                # capture: decode_first_block_sova's internal Viterbi can
                # produce slightly different bit decisions than
                # decode_first_block_soft, making its standard-RS attempt
                # fail where off-mode succeeded, and needlessly tagging a
                # cleanly-decodable block as an erasure product — which
                # Pattern A then rejects when the packet is truncated.)
                # Bonus: canonical SOVA no longer runs at all on clean
                # packets, which is most of them (CPU saving).
                iter_first_stats = None
                first_data, first_corr = decode_first_block_soft(
                    soft_first, fec_mode)
                # EQ retry (rc15): more trustworthy than erasure recovery
                # (plain unforced RS), so it sits BETWEEN the standard
                # decode and SOVA in the ladder.
                if first_data is None and self.eq_retry \
                        and raw_soft is not None:
                    if eq_w is None:
                        eq_w = _train_preamble_eq(raw_soft, fs_end, fec_mode)
                    if eq_w is not None:
                        _ln = first_block_cc_bits(fec_mode)
                        _eqq = quantize_soft(
                            _apply_preamble_eq(raw_soft, eq_w, fs_end, _ln),
                            1.0)
                        first_data, first_corr = decode_first_block_soft(
                            _eqq, fec_mode)
                        if first_data is not None:
                            eq_used = True
                            eq_stage = 'preamble'
                            logger.debug(
                                "  EQ retry recovered first block "
                                f"(RS_corr={first_corr})")
                if first_data is None:
                    # Standard failed — SOVA erasure recovery, gated:
                    # skip entirely on high-HD (noise) framesyncs
                    # (validated 06-21/06-22: zero real erasure
                    # recoveries above hd=5), which also removes the
                    # erasure-search CPU burn on noise candidates.
                    if not (self.sova_max_fs_hd is not None
                            and fs_hd is not None
                            and fs_hd > self.sova_max_fs_hd):
                        first_data, first_corr, iter_first_stats = \
                            decode_first_block_sova(
                                soft_first, fec_mode,
                                max_erasures=self.max_erasures)
                        # RS gate with SOVA-off equivalence: an
                        # erasure-recovered first block over the gate has
                        # no honest fallback (standard already failed), so
                        # discard — exactly what SOVA-off produces.
                        if (first_data is not None and iter_first_stats
                                and iter_first_stats.get('method')
                                    == 'erasure'
                                and self.sova_max_rs_corr is not None
                                and first_corr is not None
                                and first_corr > self.sova_max_rs_corr):
                            logger.debug(
                                f"  First-block erasure RS={first_corr} "
                                f"exceeds gate ({self.sova_max_rs_corr}); "
                                "discarding (SOVA-off equivalence)")
                            first_data, first_corr, iter_first_stats = \
                                None, 0, None
            else:  # 'off'
                first_data, first_corr = decode_first_block_soft(
                    soft_first, fec_mode)
                if first_data is None and self.eq_retry \
                        and raw_soft is not None:
                    if eq_w is None:
                        eq_w = _train_preamble_eq(raw_soft, fs_end, fec_mode)
                    if eq_w is not None:
                        _ln = first_block_cc_bits(fec_mode)
                        _eqq = quantize_soft(
                            _apply_preamble_eq(raw_soft, eq_w, fs_end, _ln),
                            1.0)
                        first_data, first_corr = decode_first_block_soft(
                            _eqq, fec_mode)
                        if first_data is not None:
                            eq_used = True
                            eq_stage = 'preamble'
                            logger.debug(
                                "  EQ retry recovered first block "
                                f"(RS_corr={first_corr})")
        else:
            first_data, first_corr = decode_first_block(
                first_cc[:first_block_cc_bits(fec_mode)], fec_mode)
        if first_data is None:
            logger.debug(f"  First block decode failed at bit {fs_end}")
            return []

        # Parse AirLink header
        try:
            airlink = AirLinkHeader.parse(first_data[:AIRLINK_HEADER_BYTES])
        except ValueError:
            return []
        if airlink.version != 0:
            logger.debug(f"  Bad AirLink version {airlink.version}")
            return []
        # Spec (AirLink v1.2 §3.2): "The maximum AirLink Payload is 1023
        # bytes." The earlier 256 cap was a plausibility heuristic that
        # would have silently rejected legal large frames (e.g. repeater
        # concentration full-boat frames: ~5.6 s airtime at mode 0,
        # ~3 s at mode 2).
        if airlink.length < AIRLINK_HEADER_BYTES or airlink.length > 1023:
            logger.debug(f"  Implausible AirLink length {airlink.length}")
            return []

        # Per AirLink spec §3.2: airlink.length is the AirLink Payload length,
        # which EXCLUDES the 2-byte AirLink header. The first FEC block
        # carries 2 bytes of AirLink header + (FIRST_BLOCK_DATA - 2) bytes
        # of AirLink Payload. So:
        airlink_payload_bytes = airlink.length
        first_block_payload_bytes = first_block_data - AIRLINK_HEADER_BYTES
        bytes_after_first = airlink_payload_bytes - first_block_payload_bytes
        # MER is measured after the decode settles (see _finish_mer below);
        # reset here so a frame can never inherit the previous frame's value.
        self._mer = None
        if bytes_after_first < 0:
            bytes_after_first = 0

        followon_data = b''
        followon_corr = 0
        offset_in_bits = first_block_cc_bits_padded(fec_mode)  # skip first block + tail padding

        if bytes_after_first > 0:
            # Per spec §3.5.2: ALL follow-on blocks are CC-encoded as a single
            # stream, with one CC tail at the end. So we compute the total
            # RS bytes across all blocks, plus one tail.
            full_block_data = FOLLOWON_BLOCK_DATA[fec_mode]
            parity = RS_PARITY[fec_mode]
            n_full = bytes_after_first // full_block_data
            final_size = bytes_after_first % full_block_data
            if final_size == 0:
                total_rs_bytes = n_full * (full_block_data + parity)
            else:
                total_rs_bytes = (n_full * (full_block_data + parity)
                                   + (final_size + parity))

            # Total CC bits for the follow-on stream at this mode's rate
            #                 + 16 (one tail+padding for the whole stream)
            total_cc_bits = followon_cc_bits_padded(total_rs_bytes, fec_mode)

            if fs_end + offset_in_bits + total_cc_bits > len(bits):
                logger.debug(f"  Not enough bits for follow-on stream "
                             f"(need {total_cc_bits}, have {len(bits) - fs_end - offset_in_bits})")
            else:
                # Pass total_rs_bytes * 16 + 12 to Viterbi (drop 4 padding bits)
                iter_followon_stats = None
                if soft_ok:
                    soft_followon = quantized_soft[
                        fs_end + offset_in_bits :
                        fs_end + offset_in_bits + followon_cc_bits(total_rs_bytes, fec_mode)]
                    if self.iterative == 'demod':
                        followon_data, followon_corr_count, iter_followon_stats = \
                            decode_followon_stream_iterative(
                                soft_followon, fec_mode, bytes_after_first)
                    elif self.iterative == 'sova':
                        # OFF-FIRST semantics: exact standard soft decode
                        # first (identical to off); SOVA only on failure.
                        # See first-block note. Guarantees the honest
                        # partial path is always available: if SOVA's
                        # recovery is discarded by the gate below,
                        # followon_data=None routes into the existing
                        # failure path which emits the first block as a
                        # TRUNCATED honest partial — exactly what SOVA-off
                        # emits. This fixes the 6-14 defect where a
                        # rejected recovery silently destroyed the partial.
                        _fd, _fpb, _fok = \
                            decode_followon_stream_soft_blocks(
                                soft_followon, fec_mode, bytes_after_first)
                        _nfull, _nfin = divmod(
                            bytes_after_first,
                            FOLLOWON_BLOCK_DATA[fec_mode])
                        _nblocks = _nfull + (1 if _nfin else 0)
                        if _fd and _fok >= _nblocks:
                            followon_data = _fd
                            followon_corr_count = sum(_fpb)
                            followon_block_corr = list(_fpb)
                        else:
                            followon_data, followon_corr_count = None, -1
                        # EQ retry sits between standard and erasure —
                        # unforced RS, fully honest (see first-block note).
                        if (followon_data is None and self.eq_retry
                                and raw_soft is not None):
                            if eq_w is None:
                                eq_w = _train_preamble_eq(
                                    raw_soft, fs_end, fec_mode)
                            if eq_w is not None:
                                _ln = followon_cc_bits(total_rs_bytes, fec_mode)
                                _st = fs_end + offset_in_bits
                                _eqq = quantize_soft(
                                    _apply_preamble_eq(
                                        raw_soft, eq_w, _st, _ln), 1.0)
                                _ed, _epb, _eok = \
                                    decode_followon_stream_soft_blocks(
                                        _eqq, fec_mode, bytes_after_first)
                                if _ed and _eok >= _nblocks:
                                    followon_data = _ed
                                    followon_corr_count = sum(_epb)
                                    followon_block_corr = list(_epb)
                                else:
                                    followon_data = None
                                if followon_data is not None:
                                    eq_used = True
                                    eq_stage = 'preamble'
                                    logger.debug(
                                        "  EQ retry recovered follow-on "
                                        f"(RS_corr={followon_corr_count})")
                            # DD-eq (rc17): preamble filter failed the
                            # follow-on -> retrain on the re-encoded first
                            # block (fresher channel state) and retry.
                            # Still plain unforced RS.
                            if followon_data is None:
                                _ln2 = followon_cc_bits(total_rs_bytes, fec_mode)
                                _st2 = fs_end + offset_in_bits
                                _pd, _ppb, _pok, _pnb, _pit = \
                                    _dd_eq_progressive(
                                        raw_soft, fs_end, first_data,
                                        fec_mode, bytes_after_first,
                                        _st2, _ln2)
                                if _pd is not None and _pok >= _pnb:
                                    followon_data = _pd
                                    followon_corr_count = sum(_ppb)
                                    followon_block_corr = list(_ppb)
                                    eq_used = True
                                    eq_stage = ('dd' if _pit <= 1
                                                else 'dd-prog')
                                    logger.debug(
                                        "  DD-EQ progressive recovered "
                                        f"follow-on ({_pnb} blocks, "
                                        f"RS per block={_ppb})")
                        if (followon_data is None
                                and not force_no_sova_followon
                                and not (
                                self.sova_max_fs_hd is not None
                                and fs_hd is not None
                                and fs_hd > self.sova_max_fs_hd)):
                            followon_data, followon_corr_count, \
                                iter_followon_stats = \
                                decode_followon_stream_sova(
                                    soft_followon, fec_mode,
                                    bytes_after_first,
                                    max_erasures=self.max_erasures)
                            # RS gate (total = first + followon), with
                            # honest-partial fallback on discard.
                            if (followon_data is not None
                                    and iter_followon_stats
                                    and iter_followon_stats.get('method')
                                        == 'erasure'
                                    and self.sova_max_rs_corr is not None):
                                total_rs = ((first_corr or 0)
                                            + (followon_corr_count or 0))
                                if total_rs > self.sova_max_rs_corr:
                                    logger.debug(
                                        f"  Follow-on erasure total "
                                        f"RS={total_rs} exceeds gate "
                                        f"({self.sova_max_rs_corr}); "
                                        "falling back to honest "
                                        "truncated partial")
                                    followon_data = None
                                    iter_followon_stats = None
                            # Structural dry-run: an erasure "success"
                            # whose bytes yield an inconsistent stacked-
                            # MANT walk (a PDU claiming more than
                            # available, bad header, bad version) is
                            # recovered garbage — previously it was built
                            # into a truncated packet which Pattern A
                            # rejected at emit, silently destroying the
                            # honest partial (the 6-14 defect). Discard it
                            # here instead so the failure path emits the
                            # first block as an honest partial.
                            if (followon_data is not None
                                    and iter_followon_stats
                                    and iter_followon_stats.get('method')
                                        == 'erasure'):
                                cand = (first_data + followon_data)[
                                    AIRLINK_HEADER_BYTES :
                                    AIRLINK_HEADER_BYTES + airlink.length]
                                if not self._mant_walk_consistent(cand):
                                    logger.debug(
                                        "  Follow-on erasure recovery "
                                        "fails MANT structural walk; "
                                        "falling back to honest "
                                        "truncated partial")
                                    followon_data = None
                                    iter_followon_stats = None
                    else:  # 'off'
                        _fd, _fpb, _fok = \
                            decode_followon_stream_soft_blocks(
                                soft_followon, fec_mode, bytes_after_first)
                        _nfull, _nfin = divmod(
                            bytes_after_first,
                            FOLLOWON_BLOCK_DATA[fec_mode])
                        _nblocks = _nfull + (1 if _nfin else 0)
                        if _fd and _fok >= _nblocks:
                            followon_data = _fd
                            followon_corr_count = sum(_fpb)
                            followon_block_corr = list(_fpb)
                        else:
                            followon_data, followon_corr_count = None, -1
                        if (followon_data is None and self.eq_retry
                                and raw_soft is not None):
                            if eq_w is None:
                                eq_w = _train_preamble_eq(
                                    raw_soft, fs_end, fec_mode)
                            if eq_w is not None:
                                _ln = followon_cc_bits(total_rs_bytes, fec_mode)
                                _st = fs_end + offset_in_bits
                                _eqq = quantize_soft(
                                    _apply_preamble_eq(
                                        raw_soft, eq_w, _st, _ln), 1.0)
                                _ed, _epb, _eok = \
                                    decode_followon_stream_soft_blocks(
                                        _eqq, fec_mode, bytes_after_first)
                                if _ed and _eok >= _nblocks:
                                    followon_data = _ed
                                    followon_corr_count = sum(_epb)
                                    followon_block_corr = list(_epb)
                                else:
                                    followon_data = None
                                if followon_data is not None:
                                    eq_used = True
                                    eq_stage = 'preamble'
                                    logger.debug(
                                        "  EQ retry recovered follow-on "
                                        f"(RS_corr={followon_corr_count})")
                            # DD-eq (rc17): preamble filter failed the
                            # follow-on -> retrain on the re-encoded first
                            # block (fresher channel state) and retry.
                            # Still plain unforced RS.
                            if followon_data is None:
                                _ln2 = followon_cc_bits(total_rs_bytes, fec_mode)
                                _st2 = fs_end + offset_in_bits
                                _pd, _ppb, _pok, _pnb, _pit = \
                                    _dd_eq_progressive(
                                        raw_soft, fs_end, first_data,
                                        fec_mode, bytes_after_first,
                                        _st2, _ln2)
                                if _pd is not None and _pok >= _pnb:
                                    followon_data = _pd
                                    followon_corr_count = sum(_ppb)
                                    followon_block_corr = list(_ppb)
                                    eq_used = True
                                    eq_stage = ('dd' if _pit <= 1
                                                else 'dd-prog')
                                    logger.debug(
                                        "  DD-EQ progressive recovered "
                                        f"follow-on ({_pnb} blocks, "
                                        f"RS per block={_ppb})")
                else:
                    cc_bits_followon = bits[fs_end + offset_in_bits :
                                              fs_end + offset_in_bits + followon_cc_bits(total_rs_bytes, fec_mode)]
                    from .fec import decode_followon_stream
                    followon_data, followon_corr_count = decode_followon_stream(
                        cc_bits_followon, fec_mode, bytes_after_first)
                if followon_data is None:
                    logger.debug("  Follow-on stream RS failed; emitting first block only")
                    followon_data = b''
                    followon_corr = 0
                else:
                    followon_corr = followon_corr_count

        # ---- MER (SINAD analogue) -------------------------------------
        # Computed once per AirLink frame, now that first_data and
        # followon_data are final. Pure observation: it feeds no decode,
        # gating, or filtering decision, so it cannot affect which packets
        # are emitted. Every packet from this frame carries the frame's
        # value, matching how snr_db is already scoped.
        # Per-burst timing probe (observation only — never alters decoding).
        self._timing = None
        if (getattr(self, '_timing_probe', False)
                and getattr(self, '_filtered_43k', None) is not None):
            try:
                from .timing_probe import measure_burst_timing
                self._timing = measure_burst_timing(
                    self._filtered_43k, fs_end,
                    float(getattr(self, '_bit_phase', 0) or 0), fec_mode,
                    inverted=bool(getattr(self, '_soft_inverted_now', False)))
            except Exception as e:
                logger.debug(f"timing probe failed: {e}")

        self._mer = None
        _mer_due = False
        if self.mer_mode != 'off' and raw_soft is not None:
            self._mer_frame_counter += 1
            _mer_due = (self._mer_frame_counter % self.mer_sample) == 0
        if _mer_due:
            try:
                from .quality import measure_symbol_mer
                _segs, _cov = _mer_reference_segments(
                    first_data, followon_data, fec_mode, fs_end)
                if _segs:
                    _m = measure_symbol_mer(raw_soft, _segs)
                    if _m is not None:
                        _m['coverage'] = _cov
                        if self.mer_mode == 'full':
                            _eq = _mer_after_ideal_eq(raw_soft, _segs)
                            if _eq is not None:
                                _m['mer_eq_db'] = _eq['mer_db']
                                _m['eq_gain_db'] = _eq['mer_db'] - _m['mer_db']
                            # ACHIEVED MER: the stream the decode actually
                            # used. MER is the raw channel and MER_eq is an
                            # IDEAL least-squares FIR fitted with perfect
                            # knowledge of the transmitted symbols — neither
                            # is what the real equalizer produced, so how
                            # much of the ~2.6 dB gap the ladder actually
                            # captures was invisible. Measuring the real
                            # equalizer's output closes that:
                            #     MER  ->  MER_act  ->  MER_eq
                            #   channel   achieved    bound
                            # If MER_act sits near MER_eq the equalizer is
                            # already doing its job; if it sits near raw MER,
                            # more training symbols would pay.
                            if eq_w is not None:
                                try:
                                    _span = min(len(raw_soft) - fs_end,
                                                max(e[0] + len(e[1])
                                                    for e in _segs) - fs_end)
                                    if _span > 0:
                                        _act = list(raw_soft)
                                        _act[fs_end:fs_end + _span] = \
                                            _apply_preamble_eq(
                                                raw_soft, eq_w, fs_end, _span)
                                        _ma = measure_symbol_mer(_act, _segs)
                                        if _ma is not None:
                                            _m['mer_act_db'] = _ma['mer_db']
                                except Exception as e:
                                    logger.debug(f"achieved MER failed: {e}")
                        self._mer = _m
            except Exception as e:
                logger.debug(f"MER measurement failed: {e}")

        truncated = False
        if bytes_after_first > 0 and followon_data == b'':
            truncated = True

        # Also catch the case where bytes_after_first > 0 but we never even
        # entered the followon block (because we explicitly went to b''
        # via the failure path, OR because we didn't have enough bits)
        if bytes_after_first > 0 and len(followon_data) < bytes_after_first:
            truncated = True

        # Aggregate iterative-decode stats (only set if self.iterative
        # is not 'off'). Both iter_first_stats and iter_followon_stats
        # are dicts when set, or None if iterative wasn't used or the
        # followon branch wasn't entered.
        iter_method_agg = None
        iter_n_erasures_agg = 0
        iter_max_block_corr_agg = 0
        if self.iterative != 'off':
            iter_followon_stats_local = locals().get('iter_followon_stats', None)
            stats_list = [iter_first_stats, iter_followon_stats_local]
            methods = [s['method'] for s in stats_list if s]
            if 'erasure' in methods:
                iter_method_agg = 'erasure'
            elif 'standard' in methods:
                iter_method_agg = 'standard'
            iter_n_erasures_agg = sum(s.get('n_erasures', 0)
                                       for s in stats_list if s)
            iter_max_block_corr_agg = max(
                (s.get('max_block_corr', 0) for s in stats_list if s),
                default=0)

        # NOTE on Phase 1 false-positive filtering:
        #
        # Per investigation 2026-05-26, erasure-RS with full-or-near-
        # full erasure budget on heavily-corrupted blocks can produce
        # *some* RS-consistent codeword whose first bytes happen to
        # parse as a valid AirLink+MANT header but contain garbage
        # source IDs and measurement bytes. The distinguishing pattern:
        # the resulting packet ends up TRUNCATED (either follow-on RS
        # also failed, or the "MANT PDU claims more bytes than
        # available" path fired). All known false positives in the
        # paired A/B fell in (truncated AND iter_method=erasure); all
        # known legitimate gains fell in (NOT truncated AND erasure).
        #
        # The filter is applied at packet emission below (per-PDU),
        # because per-PDU truncation captures the stack_truncated case
        # too, which can fire even when the AirLink-level RS succeeded.

        # Assemble full PDU bytes
        full_pdu = first_data + followon_data

        # The AirLink Payload (after the 2-byte AirLink header) contains
        # ONE OR MORE concatenated MANT PDUs. Per AirLink spec §3.2,
        # `airlink.length` bytes follow the header and contain stacked
        # MANT PDUs. Iterate through them.
        airlink_payload = full_pdu[AIRLINK_HEADER_BYTES :
                                    AIRLINK_HEADER_BYTES + airlink.length]

        packets = []
        offset = 0
        pdu_index = 0
        rx_time_now = time.time()
        # Set when an erasure-recovered packet is rejected by the emit
        # filters; triggers the honest-partial retry at function end.
        erasure_pkt_rejected = False

        while offset < len(airlink_payload):
            remaining = airlink_payload[offset:]
            # Skip trailing 0x55 padding (AirLink may pad the unused tail
            # of the first block when the actual MANT PDUs don't fill it,
            # though airlink.length should already exclude this)
            if all(b == 0x55 for b in remaining):
                break
            try:
                mant = MantHeader.parse(remaining)
            except ValueError as e:
                logger.debug(f"  MANT parse failed at offset {offset} "
                             f"(PDU #{pdu_index}): {e}")
                break
            if mant.version > 2:
                logger.debug(f"  Bad MANT version {mant.version} "
                             f"at offset {offset}")
                break

            # Per MANT spec: mant.payload_length is the application PAYLOAD
            # length following the MANT header.
            mant_pdu_size = mant.header_bytes + mant.payload_length
            if mant_pdu_size > len(remaining):
                # PDU claims to be larger than what we have left — truncated
                # or follow-on RS may have failed for the bytes containing
                # the second MANT PDU. Bail out — what we have is what we have.
                logger.debug(f"  MANT PDU #{pdu_index} claims {mant_pdu_size}B "
                             f"but only {len(remaining)}B available; stopping")
                # Still emit a packet with whatever we got
                application_payload = remaining[mant.header_bytes :
                                                  mant.header_bytes + mant.payload_length]
                # Mark as truncated
                stack_truncated = True
                # Construct partial packet (best effort)
                srp_pdu = None
                if mant.port == 0 and len(application_payload) > 0:
                    try:
                        from .srp import parse_srp_pdu
                        srp_pdu = parse_srp_pdu(application_payload)
                    except Exception as e:
                        logger.debug(f"  SRP parse failed: {e}")
                pkt = Alert2Packet(
                    fec_mode=fec_mode,
                    rx_time=rx_time_now,
                    rs_corrections=first_corr + followon_corr,
                    rs_block_corr=(
                        [first_corr] + followon_block_corr
                        if followon_block_corr is not None else None),
                    airlink=airlink,
                    mant=mant,
                    raw_payload=remaining,
                    application_payload=application_payload,
                    srp_pdu=srp_pdu,
                    framesync_hd=fs_hd,
                    sample_offset=fs_end,
                    decode_path='matched_filter',
                    truncated=stack_truncated,
                    viterbi_mode=viterbi_mode,
                    iter_method=iter_method_agg,
                    iter_n_erasures=iter_n_erasures_agg,
                    iter_max_block_corr=iter_max_block_corr_agg,
                    eq_recovered=eq_used,
                    eq_stage=eq_stage,
                )
                self._attach_mer(pkt)
                # False-positive filter (see Alert2Decoder._is_likely_false_positive
                # for the documented patterns).
                if not self._is_likely_false_positive(pkt) \
                        and not self._exceeds_correction_gates(pkt):
                    packets.append(pkt)
                else:
                    if pkt.iter_method == 'erasure':
                        erasure_pkt_rejected = True
                    logger.debug(
                        "  Dropping suspect erasure-RS decode "
                        f"(truncated={pkt.truncated}, "
                        f"srp_parsed={pkt.srp_pdu is not None})")
                break

            application_payload = remaining[mant.header_bytes :
                                              mant.header_bytes + mant.payload_length]

            # Try to parse SRP PDU if Port = 0 (Self-Reporting Protocol)
            srp_pdu = None
            if mant.port == 0 and len(application_payload) > 0:
                try:
                    from .srp import parse_srp_pdu
                    srp_pdu = parse_srp_pdu(application_payload)
                except Exception as e:
                    logger.debug(f"  SRP parse failed: {e}")

            # A PDU is truncated only if its OWN bytes are incomplete.
            # The AirLink-level "truncated" flag (set when follow-on RS failed)
            # doesn't automatically mean each PDU is truncated — only the PDUs
            # whose bytes fell into the failed follow-on block. If THIS PDU's
            # full payload was recovered (regardless of whether other PDUs in
            # the same AirLink frame were lost), it's complete.
            pdu_truncated = len(application_payload) < mant.payload_length

            pkt = Alert2Packet(
                fec_mode=fec_mode,
                rx_time=rx_time_now,
                rs_corrections=first_corr + followon_corr,
                rs_block_corr=(
                    [first_corr] + followon_block_corr
                    if followon_block_corr is not None else None),
                airlink=airlink,
                mant=mant,
                raw_payload=remaining[:mant_pdu_size],
                application_payload=application_payload,
                srp_pdu=srp_pdu,
                framesync_hd=fs_hd,
                sample_offset=fs_end,
                decode_path='matched_filter',
                truncated=pdu_truncated,
                viterbi_mode=viterbi_mode,
                iter_method=iter_method_agg,
                iter_max_block_corr=iter_max_block_corr_agg,
                iter_n_erasures=iter_n_erasures_agg,
                eq_recovered=eq_used,
                eq_stage=eq_stage,
            )
            self._attach_mer(pkt)
            # False-positive filter (see _is_likely_false_positive).
            if not self._is_likely_false_positive(pkt) \
                    and not self._exceeds_correction_gates(pkt):
                packets.append(pkt)
            else:
                if pkt.iter_method == 'erasure':
                    erasure_pkt_rejected = True
                logger.debug(
                    "  Dropping suspect erasure-RS decode "
                    f"(truncated={pkt.truncated}, "
                    f"srp_parsed={pkt.srp_pdu is not None})")
            offset += mant_pdu_size
            pdu_index += 1

        # Stamp frame-bundling info: all PDUs decoded from this single
        # AirLink frame share the same transmission. This lets the UI show
        # "bundled with src X, src Y" so a large airlink.length next to a
        # small per-PDU payload is understood as concentration, not loss.
        if len(packets) > 1:
            frame_sources = [
                (p.mant.source_addr if p.mant else None) for p in packets
            ]
            for idx, p in enumerate(packets):
                p.frame_pdu_count = len(packets)
                p.frame_index = idx
                p.frame_sources = frame_sources

        # HONEST-PARTIAL RETRY (2026-07-01): if an erasure-recovered
        # packet was rejected by the emit filters AND the first block was
        # NOT itself an erasure product, re-decode this candidate with
        # the SOVA follow-on disabled. That reproduces exactly what
        # iterative='off' emits (the standard-decode truncated partial),
        # so a rejected recovery can never silently destroy the honest
        # partial — gated SOVA becomes a strict superset of off.
        # (If the first block WAS erasure-recovered, off-mode would have
        # emitted nothing for this candidate, so dropping is already
        # SOVA-off-equivalent and no retry is needed.)
        if (erasure_pkt_rejected and not force_no_sova_followon
                and (iter_first_stats is None
                     or iter_first_stats.get('method') != 'erasure')):
            logger.debug(
                "  Erasure recovery rejected at emit; retrying candidate "
                "without SOVA follow-on (honest-partial fallback)")
            return self._try_decode_at(
                bits, fs_end, fec_mode, fs_hd,
                quantized_soft=quantized_soft,
                raw_soft=raw_soft,
                force_no_sova_followon=True)

        return packets


def decode_wav(path: str,
               max_framesync_hd: int = 5,
               fec_modes: Optional[List[int]] = None,
               try_simple_demod_too: bool = True,
               try_inverted_polarity: bool = True,
               soft_viterbi: bool = False,
               soft_framesync: bool = False,
               soft_framesync_threshold: float = 0.62,
               iterative: bool = False,
               max_erasures: Optional[int] = None,
               max_rs_corr: Optional[int] = None,
               sova_max_fs_hd: Optional[int] = None,
               sova_max_rs_corr: Optional[int] = None,
               sova_max_rs_corr_per_block: Optional[int] = None,
               eq_retry: bool = False,
               presync_eq: bool = False,
               presync_max_fs_hd: Optional[int] = None,
               mer_mode: str = 'full',
               mer_sample: int = 1,
               timing_probe: bool = False,
               hum_filter: bool = False,
               hum_cutoff: float = 300.0,
               multiphase: bool = False,
               phase_candidates: int = 3,
               rate_track: bool = False,
               rate_track_min_slope: float = 0.002) -> List[Alert2Packet]:
    """Decode all packets from a WAV file.

    Tries multiple demod paths and bit polarities for robustness.
    Returns deduplicated list of packets.

    If soft_viterbi is True, the matched-filter path uses soft-decision
    Viterbi (FEC mode 0). If soft_framesync is True, framesync is found
    by soft correlation. The two are independent and compose. The
    simple-demod path stays hard (no calibrated soft samples).
    If iterative is True, failed RS decodes are retried with erasure
    positions derived from demodulator confidence (Phase 1 iterative
    decode). Requires soft_viterbi (no effect without it). Default off.
    """
    fs, data = wav.read(path)
    if data.ndim > 1:
        data = data[:, 0]
    audio = data.astype(np.float32) / 32768.0
    logger.info(f"Loaded {path}: {len(audio)} samples at {fs} Hz")

    decoder = Alert2Decoder(max_framesync_hd=max_framesync_hd,
                            fec_modes=fec_modes,
                            soft_viterbi=soft_viterbi,
                            soft_framesync=soft_framesync,
                            soft_framesync_threshold=soft_framesync_threshold,
                            iterative=iterative,
                            max_erasures=max_erasures,
                            max_rs_corr=max_rs_corr,
                            sova_max_fs_hd=sova_max_fs_hd,
                            sova_max_rs_corr=sova_max_rs_corr,
                            sova_max_rs_corr_per_block=sova_max_rs_corr_per_block,
                            eq_retry=eq_retry,
                            presync_eq=presync_eq,
                            presync_max_fs_hd=presync_max_fs_hd,
                            mer_mode=mer_mode, mer_sample=mer_sample,
                            timing_probe=timing_probe)
    all_packets = []
    seen_keys = set()

    # Try matched-filter demod (preferred path)
    bits, info = demodulate(audio, fs, hum_filter=hum_filter,
                            hum_cutoff=hum_cutoff)
    logger.info(f"Matched-filter demod produced {len(bits)} bits "
                f"(filtered_std={info['filtered_std']:.4f})")
    audio_43k_mf = info.get('audio_43k')
    phase_mf = info.get('best_phase', 0)
    soft_mf = info.get('soft_samples')
    amp_mf = info.get('amplitude_estimate')

    # EXPERIMENTAL: multi-phase matched-filter pass. The default demod
    # picks ONE sampling phase for the whole stream; phase wanders within
    # bursts (measured), so packets can be lost to a suboptimal fixed
    # phase. When multiphase=True, additionally decode at every other
    # phase and union the results. Measured on a clean SDR capture: +47%
    # packets, +31% complete decodes, all standard-RS (no erasure), zero
    # flagged by the false-positive filters. On a low-SNR/under-driven
    # capture it recovered nothing (no good phase exists when the eye is
    # closed by noise) — so it helps healthy-SNR sites, not weak ones.
    # Cost ~4.4x decode time (resample/MF work is shared). Off by default.
    _extra_phase_infos = []
    if multiphase:
        from .demod import (matched_filter, resample_to_rx_rate, slice_bits,
                            rank_phases)
        from .fec import estimate_amplitude
        _bb = audio - audio.mean()
        _bb43 = resample_to_rx_rate(_bb, fs)
        _filt = matched_filter(_bb43)
        for _ph in rank_phases(_filt, phase_candidates):
            if _ph == phase_mf:
                continue
            _soft = _filt[_ph::SAMPLES_PER_BIT]
            _bits = slice_bits(_soft)
            _soft_list = [float(x) for x in _soft]
            _amp = estimate_amplitude(_soft_list)
            _extra_phase_infos.append((_ph, _bits, _bb43, _soft_list, _amp))

    for label, b_use, inv in [
            ('mf-normal', bits, False),
            (('mf-inverted', [1 - b for b in bits], True)
             if try_inverted_polarity else (None, None, None))]:
        if label is None:
            continue
        for pkt in decoder.decode_bits(b_use,
                                         audio_43k=audio_43k_mf,
                                         bit_phase=phase_mf,
                                         soft_samples=soft_mf,
                                         amplitude_estimate=amp_mf,
                                         soft_inverted=inv):
            pkt.decode_path = label
            key = (pkt.fec_mode, pkt.raw_payload)
            if key not in seen_keys:
                seen_keys.add(key)
                all_packets.append(pkt)

    # Optionally also try the simpler demod
    if try_simple_demod_too:
        bits, info = demodulate_simple(audio, fs)
        logger.info(f"Simple demod produced {len(bits)} bits")
        for label, b_use in [('simple-normal', bits),
                              ('simple-inverted', [1 - b for b in bits]) if try_inverted_polarity else None]:
            if label is None:
                continue
            for pkt in decoder.decode_bits(b_use):
                pkt.decode_path = label
                key = (pkt.fec_mode, pkt.raw_payload)
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_packets.append(pkt)

    # EXPERIMENTAL multi-phase union (see flag above). Decode each extra
    # phase through the same decode_bits path and merge, upgrading a
    # previously-truncated packet if another phase yields it complete.
    if _extra_phase_infos:
        by_key = {}
        for p in all_packets:
            by_key[(p.fec_mode, p.raw_payload)] = p
        for _ph, _bits, _bb43, _soft_list, _amp in _extra_phase_infos:
            for label, b_use, inv in [
                    ('mf%d-normal' % _ph, _bits, False),
                    ('mf%d-inverted' % _ph, [1 - b for b in _bits], True)]:
                for pkt in decoder.decode_bits(
                        b_use, audio_43k=_bb43, bit_phase=_ph,
                        soft_samples=(_soft_list if not inv
                                      else [-x for x in _soft_list]),
                        amplitude_estimate=_amp, soft_inverted=inv):
                    pkt.decode_path = label
                    key = (pkt.fec_mode, pkt.raw_payload)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        all_packets.append(pkt)

    # Per-burst symbol-rate correction (--rate-track, needs multiphase for
    # the matched-filter stream). Adds packets from bursts whose clock
    # drift exceeds the threshold; never removes. See rate_track.py.
    if rate_track and multiphase:
        try:
            from .burst_detect import detect_bursts
            from .rate_track import decode_rate_corrected
            _bursts = detect_bursts(_filt, list(range(SAMPLES_PER_BIT)),
                                    decoder.max_fs_hd, decoder.fec_modes,
                                    min_corr=max(
                                        getattr(decoder, 'candidate_gate',
                                                0.0) or 0.0, 0.40))
            for pkt in decode_rate_corrected(decoder, _filt, _bursts,
                                             min_slope=rate_track_min_slope):
                key = (pkt.fec_mode, pkt.raw_payload)
                prev = next((p for p in all_packets
                             if (p.fec_mode, p.raw_payload) == key), None)
                if prev is None:
                    seen_keys.add(key)
                    all_packets.append(pkt)
                elif prev.truncated and not pkt.truncated:
                    all_packets[all_packets.index(prev)] = pkt
        except Exception as e:
            logger.warning(f"rate-track failed: {e}")

    return all_packets
