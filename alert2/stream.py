# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Streaming decoder for live input (e.g., from rtl_fm).

Reads raw 16-bit signed mono PCM from a file-like object (typically stdin)
in chunks, processes each chunk through the decoder, and yields packets
as they're decoded.
"""
import sys
import time
import logging
import struct
from typing import Iterator, BinaryIO, Optional
import time as _time
from .constants import (FIRST_BLOCK_DATA as _FBD_CONST,
                        RS_PARITY as _RSP_CONST)
import numpy as np

from .types import Alert2Packet
from .decoder import Alert2Decoder
from .demod import demodulate
from .demod import (matched_filter, resample_to_rx_rate, slice_bits,
                    rank_phases)
from .fec import estimate_amplitude
from .constants import SAMPLES_PER_BIT

logger = logging.getLogger('alert2.stream')


from .constants import (FIRST_BLOCK_DATA as _FBD,
                        FOLLOWON_BLOCK_DATA as _FOD,
                        RS_PARITY as _RSP,
                        AIRLINK_HEADER_BYTES as _AHB,
                        SAMPLES_PER_BIT as _SPB,
                        BITSYNC_BITS as _BSB, FRAMESYNC_BITS as _FSB)




def _frame_airtime_s(airlink_len: int, fec_mode: int,
                     fs: int = 43200) -> float:
    """Approximate over-the-air duration of a frame from its AirLink
    length field (payload bytes) and FEC mode."""
    m = fec_mode if fec_mode in _FBD else 0
    ba = max(0, airlink_len - (_FBD[m] - _AHB))
    full = _FOD[m]
    nf, fin = divmod(ba, full)
    total_rs = (_FBD[m] + _RSP[m]) + nf * (full + _RSP[m]) \
        + ((fin + _RSP[m]) if fin else 0)
    bits = _BSB + _FSB + _frame_bit_length(airlink_len, m)
    return bits * _SPB / fs


from .burst import (decode_bursts as _burst_decode_bursts,
                    frame_bit_length as _frame_bit_length)
from .sync import (find_framesync as _find_fs,
                   find_framesync_with_bitsync_gate as _find_fs_gate)
from .fec import (decode_first_block_soft as _dec_first,
                  quantize_soft as _qsoft,
                  first_block_cc_bits as _fb_cc_bits)
from .types import AirLinkHeader as _AL



def _phase_quality(pkt):
    """Rank a decode of the SAME payload produced at a different phase.

    Higher is better. Used to choose which copy to keep when the primary
    phase, a multiphase pass and the burst pass all decode one burst.

    Measured on real captures (23 payloads decoded at 2+ phases, three
    files): choosing by preamble correlation lands 0.013 dB of MER short
    of an oracle that always picks the best phase, against 0.715 dB for
    RS_corr or FS_hd. The preamble term is therefore preferred whenever
    the timing probe supplied it; the RS/FS terms are the fallback.

    Rationale for the ordering: t_gain is how much preamble correlation
    the used sampling instant left on the table, so SMALL is good. A
    burst sampled at the wrong one of the nine phases shows a large
    t_gain and decodes at much lower MER (measured: |t_off| 0-0.5 -> MER
    7.05, |t_off| 3-4.6 -> MER 1.15, n=2130 at the QTH).
    """
    tg = getattr(pkt, 'timing_gain', None)
    have_probe = 0 if tg is None else 1
    return (
        have_probe,
        -(tg if tg is not None else 0.0),
        -(pkt.rs_corrections or 0),
        -(pkt.framesync_hd if pkt.framesync_hd is not None else 99),
    )


def _select_best_copies(packets, mode='best'):
    """Collapse duplicate decodes of one payload to a single copy.

    mode='first' reproduces the historical behaviour exactly: the first
    decode of a payload wins and later ones are discarded, which meant
    the primary phase always claimed the payload even when a later phase
    had decoded the same burst far better (measured mean 3.55 dB of MER
    discarded, max 7.48 dB).

    mode='best' keeps the highest-quality copy instead. The PACKET SET is
    unchanged either way — same payloads, same count — only which copy is
    retained differs, so this cannot add or remove a decode.

    Order is preserved by first appearance so downstream shadow-dedup and
    emission ordering behave as before.
    """
    if mode == 'first':
        return list(packets)
    best = {}
    order = []
    for pkt in packets:
        key = (pkt.fec_mode, bytes(pkt.raw_payload), pkt.truncated)
        cur = best.get(key)
        if cur is None:
            best[key] = pkt
            order.append(key)
        elif _phase_quality(pkt) > _phase_quality(cur):
            best[key] = pkt
    return [best[k] for k in order]


def _collect_burst_candidates(decoder, bits, soft_samples, amp_est):
    """Detection-stage framesync candidates for the burst pass.

    Also reads each candidate's AirLink length from the chunk-demod
    soft values when it can, so the burst extract is sized correctly
    (one cheap RS decode; falls back to an unknown-length window).
    """
    out = []
    for inverted in (False, True):
        b_use = [1 - b for b in bits] if inverted else bits
        if decoder.require_bitsync:
            cands = _find_fs_gate(b_use, max_fs_hd=decoder.max_fs_hd,
                                  max_bs_hd=8)
        else:
            cands = _find_fs(b_use, max_hd=decoder.max_fs_hd,
                             modes=decoder.fec_modes)
        if not cands:
            continue
        q = None
        if soft_samples is not None and amp_est:
            s = [-x for x in soft_samples] if inverted else soft_samples
            try:
                q = _qsoft(s, amp_est)
            except Exception:
                q = None
        for fe, m, hd in cands:
            if m not in decoder.fec_modes:
                continue
            al_len = None
            if q is not None:
                total = _FBD_CONST[m] + _RSP_CONST[m]
                sl = _fb_cc_bits(m)
                if fe + sl <= len(q):
                    try:
                        d, _ = _dec_first(q[fe:fe + sl], m)
                        if d is not None:
                            cand_len = _AL.parse(d[:2]).length
                            if 2 <= cand_len <= 1023:
                                al_len = cand_len
                    except Exception:
                        pass
            out.append((fe, m, inverted, al_len))
    return out


def collect_candidates_all_phases(decoder, filt, phases, soft_by_phase=None,
                                  amp_by_phase=None):
    """Framesync candidates from EVERY listed phase, merged per burst.

    WHY (stage 2). Measured on 1269 multiphase winners at the QTH on
    2026-08-10, the phase that decodes a burst best is UNIFORMLY RANDOM
    over all nine (10.3%-12.2% each) and carries no memory: consecutive
    winners agree 9.9% of the time against 11.1% by chance. There is
    nothing to predict, so partial phase coverage is structurally lossy —
    with 3 of 9 candidates the best phase is simply absent two thirds of
    the time.

    Full coverage used to be unaffordable because every phase meant a full
    decode. Profiling rc29 (3 phases, 30 s audio) shows that is no longer
    where the cost is:

        Viterbi + RS      1.39 s   53%
        quantize_soft     0.59 s   23%
        slice_bits        0.26 s   10%
        find_framesync    0.03 s    1.3%   <-- after vectorisation

    Detection is 1.3% of the budget. Scanning nine phases costs ~4%, which
    buys complete coverage; the expensive part is then done ONCE per burst
    rather than once per phase.

    Candidates are keyed by ABSOLUTE sample position, not by bit index:
    `fe` is relative to its own phase, so the same burst seen from two
    phases has two different fe values. Merging on
    (phase + fe*SAMPLES_PER_BIT) // SAMPLES_PER_BIT collapses them, and the
    lowest framesync Hamming distance wins the merge.

    Returns a list of (fe, fec_mode, inverted, airlink_len, phase) — the
    burst pass needs the phase to convert bits back to sample offsets.
    """
    merged = {}
    for ph in phases:
        soft = (soft_by_phase or {}).get(ph)
        amp = (amp_by_phase or {}).get(ph)
        if soft is None:
            soft = [float(v) for v in filt[ph::SAMPLES_PER_BIT]]
        if amp is None:
            try:
                amp = estimate_amplitude(soft)
            except Exception:
                amp = None
        bits = slice_bits(filt[ph::SAMPLES_PER_BIT])
        for (fe, m, inverted, al_len) in _collect_burst_candidates(
                decoder, bits, soft, amp):
            abs_sym = (ph + fe * SAMPLES_PER_BIT) // SAMPLES_PER_BIT
            key = (abs_sym, m, inverted)
            prev = merged.get(key)
            if prev is None:
                merged[key] = (fe, m, inverted, al_len, ph)
            elif al_len is not None and prev[3] is None:
                # Prefer a candidate whose AirLink length parsed, so the
                # burst extract is sized correctly rather than guessed.
                merged[key] = (fe, m, inverted, al_len, ph)
    return list(merged.values())


def stream_decode(stream: BinaryIO,
                  sample_rate: int = 48000,
                  chunk_seconds: float = 1.5,
                  overlap_seconds: float = 0.6,
                  max_framesync_hd: int = 5,
                  fec_modes=None,
                  soft_viterbi: bool = False,
                  soft_framesync: bool = False,
                  soft_framesync_threshold: float = 0.62,
                  iterative='off',
                  max_erasures=None,
                  max_rs_corr=None,
                  sova_max_fs_hd=None,
                  sova_max_rs_corr=None,
                  sova_max_rs_corr_per_block=None,
                  eq_retry=False,
                  presync_eq=False,
                  presync_max_fs_hd=None,
                  mer_mode: str = 'full',
                  mer_sample: int = 1,
                  timing_probe: bool = False,
                  candidate_gate: float = 0.0,
                  phase_select: bool = False,
                  phase_attempts: int = 1,
                  rate_track: bool = False,
                  rate_track_min_slope: float = 0.002,
                  burst_mode=False,
                  phase_block_scan=False,
                  health_callback=None,
                  health_interval_s: float = 300.0,
                  multiphase: bool = False,
                  phase_dedup: str = 'best',
                  burst_all_phases: bool = False,
                  burst_phase_count: int = 9,
                  phase_candidates: int = 3) -> Iterator[Alert2Packet]:
    """Decode packets from a streaming PCM source.

    Args:
        stream: file-like object yielding raw int16 mono PCM
        sample_rate: input sample rate in Hz
        chunk_seconds: how much audio to buffer before each decode pass
        overlap_seconds: overlap between chunks (so packets that span
                         chunk boundaries are still caught). MUST exceed
                         the longest realistic burst or boundary-
                         straddling packets get decoded twice-truncated
                         instead of once-clean. See note below.
        max_framesync_hd: max Hamming distance for framesync match
        soft_viterbi: use soft-decision Viterbi (FEC mode 0 only)

    Yields Alert2Packet instances as they're decoded.

    Overlap sizing (changed in v0.9.0a2):
        Previous default overlap was 0.30 s. Measured against 151 real
        packets from QTH/shop captures, the maximum ALERT2 burst is
        airlink_len=130 B ≈ 452 ms (bitsync+framesync+rate-1/2 CC).
        A 300 ms overlap is SHORTER than that, so a long frame landing
        on a chunk boundary was truncated in BOTH the first chunk and
        the carried-over tail — two truncated decodes, no clean copy,
        defeating the chunk-overlap re-decode safety net the user
        relies on. Default raised to 0.60 s (covers the 452 ms
        observed max + ~30% margin) with chunk raised to 1.5 s to keep
        the decode-overhead ratio similar. This is a pure framing
        change — no algorithm change. Tunable via CLI for sites with
        unusually long frames.
    """
    chunk_samples = int(chunk_seconds * sample_rate)
    overlap_samples = int(overlap_seconds * sample_rate)
    chunk_bytes = chunk_samples * 2  # int16 = 2 bytes

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
                            timing_probe=timing_probe,
                            candidate_gate=candidate_gate)
    buffer = np.zeros(0, dtype=np.float32)
    _airtime_warned = [False]
    # rc22 audio-clock accounting: the sound card produces samples at a
    # fixed rate whether or not the decoder keeps up. If anything
    # upstream sheds samples under load, the bursts they carried
    # disappear entirely — no framesync, no truncated remnant, nothing
    # in the diag. Comparing samples actually read against samples the
    # wall clock says were produced is the only way to see it.
    _t_first_read = None
    _samples_read = 0
    _t_decode = 0.0
    _next_health = health_interval_s
    # rc21 shadow dedup: chunk-boundary copies of one air burst that
    # decode DIFFERENTLY (one complete, one truncated head) escape the
    # exact-payload dedup and inflate counts (QTH: ~430/day). Key is
    # (src, srp_ts, repeater-path) — path included so genuine multi-path
    # receptions of the same report (~7-8%% of traffic) are preserved.
    # Truncated packets are held one chunk; if a better copy of the same
    # triple appears, the shadow is suppressed, else it emits late.
    _dd_recent = {}     # triple -> (rank, chunk_idx, id)
    _dd_held = []       # [(pkt, triple, rank)]
    _dd_stats = {'suppressed': 0}
    _dd_chunk = 0

    def _dd_triple(pkt):
        try:
            if (pkt.srp_pdu is not None and pkt.srp_pdu.has_timestamp
                    and pkt.mant is not None):
                return (pkt.mant.source_addr, pkt.srp_pdu.timestamp,
                        tuple(pkt.mant.added_sas or ()))
        except Exception:
            pass
        return None

    def _dd_rank(pkt):
        return (1 if pkt.truncated else 0, pkt.framesync_hd or 0)

    seen_payloads = set()
    samples_processed = 0

    while True:
        # Read a chunk
        raw = stream.read(chunk_bytes)
        if len(raw) == 0:
            # End of stream
            break
        # Convert to float
        n_samples = len(raw) // 2
        if n_samples == 0:
            continue
        if _t_first_read is None:
            _t_first_read = _time.monotonic()
        _samples_read += n_samples
        new_samples = np.frombuffer(raw[:n_samples * 2], dtype=np.int16)
        new_samples = new_samples.astype(np.float32) / 32768.0

        # Append to buffer
        buffer = np.concatenate([buffer, new_samples])

        # Only process if we have at least one full chunk
        if len(buffer) < chunk_samples:
            continue

        # Demodulate the buffer
        try:
            bits, info = demodulate(buffer, sample_rate,
                                    phase_block_scan=phase_block_scan)
            audio_43k = info.get('audio_43k')
            phase = info.get('best_phase', 0)
            soft_samples = info.get('soft_samples')
            amp_est = info.get('amplitude_estimate')
            # Pre-decimation matched-filter output, needed by the per-burst
            # timing probe (it must see the sub-symbol detail the decimated
            # soft samples have discarded). Only referenced when the probe
            # is on; it is a view into memory the demod already allocated.
            filtered_43k = (info.get('filtered_43k')
                            if getattr(decoder, 'timing_probe', False)
                            else None)
        except Exception as e:
            logger.error(f"Demod failed: {e}")
            buffer = buffer[-overlap_samples:]
            continue

        chunk_emit = []
        _t_chunk0 = _time.monotonic()
        # Try both polarities
        for label, b_use, inv in [('normal', bits, False),
                                   ('inverted', [1 - b for b in bits], True)]:
            for pkt in decoder.decode_bits(b_use,
                                             audio_43k=audio_43k,
                                             bit_phase=phase,
                                             soft_samples=soft_samples,
                                             amplitude_estimate=amp_est,
                                             filtered_43k=filtered_43k,
                                             soft_inverted=inv):
                pkt.decode_path = label
                # NOTE (rc29): the seen_payloads gate used to sit HERE, which
                # meant a duplicate decode of the same payload from a later
                # phase was discarded before _select_best_copies could compare
                # it — making best-wins a no-op on the live path while it
                # worked on the file path. Collect every copy now; the gate is
                # applied after selection, below.
                key = (pkt.fec_mode, pkt.raw_payload)
                # Guardrail (rc18): a frame whose airtime exceeds the
                # chunk overlap is not guaranteed to fit inside any
                # single chunk — boundary-straddling copies arrive
                # truncated or not at all (field case: station 798's
                # long frames at chunk=1.5/overlap=0.6). Warn once per
                # session so the operator can raise --overlap-seconds
                # to at least the longest frame's airtime.
                try:
                    if pkt.airlink is not None:
                        _atime = _frame_airtime_s(pkt.airlink.length,
                                                  pkt.fec_mode)
                        if _atime > overlap_seconds and \
                                not _airtime_warned[0]:
                            _airtime_warned[0] = True
                            logger.warning(
                                f"Frame airtime ~{_atime:.1f}s (airlink "
                                f"{pkt.airlink.length}B, mode "
                                f"{pkt.fec_mode}) exceeds overlap "
                                f"{overlap_seconds}s — long frames can be "
                                "lost/truncated at chunk boundaries. "
                                "Increase --overlap-seconds to at least "
                                "the longest expected frame airtime "
                                "(spec max: ~5.6s at mode 0, ~3s at "
                                "mode 2).")
                except Exception:
                    pass
                # Time-bound dedup: forget after some time
                chunk_emit.append(pkt)

        # --- Burst-scoped pass (rc23) ---
        # The chunk demod above fitted ONE symbol phase (from its first
        # 0.185 s, usually noise) and ONE amplitude for every burst in
        # the window. Real bursts each want their own phase — measured
        # spread is 8 of 9 values, varying transmission to transmission
        # even for one station. Here we take the detection pass's
        # framesync candidates, cut each burst out of the chunk audio,
        # and demodulate the cut so phase and amplitude are fitted to
        # the burst itself. Additive: merged with the primary pass, so
        # nothing the chunk pass found can be lost.
        if burst_mode:
            try:
                if burst_all_phases and audio_43k is not None:
                    # Stage 2: give the burst pass candidates from EVERY
                    # phase, not just the detection phase. The winning
                    # phase is uniformly random over all nine, so a
                    # single-phase candidate list structurally misses
                    # most bursts' best sampling instant. Detection is
                    # 1.3% of CPU, so full coverage is affordable; the
                    # decode still happens once per burst.
                    _filt_all = matched_filter(audio_43k)
                    _phases = rank_phases(_filt_all, burst_phase_count)
                    cands = collect_candidates_all_phases(
                        decoder, _filt_all, _phases)
                else:
                    cands = _collect_burst_candidates(
                        decoder, bits, soft_samples, amp_est)
                bpkts = _burst_decode_bursts(
                    buffer, sample_rate, decoder, cands, phase, {},
                    lambda s, f: demodulate(s, f, phase_block_scan=True))
                for pkt in bpkts:
                    pkt.decode_path = (
                        'burst-inverted'
                        if getattr(pkt, '_burst_inverted', False)
                        else 'burst')
                    chunk_emit.append(pkt)
            except Exception as e:
                logger.error(f"Burst-scoped pass failed: {e}")

        # Multi-phase pass (live): instead of one fixed phase, decode the
        # top-`phase_candidates` phases ranked by eye-opening (variance).
        # The single-phase pass above already covered the demod's best
        # phase; here we add any ranked candidate not equal to it. The
        # seen_payloads dedup means a packet found at multiple phases is
        # yielded once. Recovers packets a single fixed phase misses on
        # healthy-SNR feeds. Top-3 captured all complete-packet gain of
        # the exhaustive all-phase search at ~1/3 the cost. Off by default.
        if multiphase and audio_43k is not None:
            try:
                filt = matched_filter(audio_43k)
                candidates = rank_phases(filt, phase_candidates)
                # Per-burst phase selection (rc33, --phase-select). Detect
                # bursts across ALL candidate phases first, then decode each
                # burst ONCE at the phase whose sampling instant best matched
                # its preamble, instead of decoding every burst at every
                # phase and discarding all but the best copy.
                #
                # Measured (40 s QTH capture, 9 phases, gate 0.40):
                #   decode-at-every-phase : 42 pkts, 2.9 s
                #   per-burst selection   : 42 pkts, 1.7 s  -> 1.68x
                #   packets lost 0, gained 0
                # The top-correlation phase decoded 41/41 in a separate
                # test, giving up a mean 0.031 dB of MER vs an oracle.
                _burst_jobs = None
                _bursts = None
                if phase_select or rate_track:
                    try:
                        from .burst_detect import detect_bursts
                        _bursts = detect_bursts(
                            filt, candidates, decoder.max_fs_hd,
                            decoder.fec_modes,
                            min_corr=max(candidate_gate, 0.40))
                    except Exception as e:
                        logger.warning(f"burst detection failed: {e}")
                        _bursts = None
                if phase_select and _bursts is not None:
                    try:
                        _burst_jobs = {}
                        for _bc in _bursts:
                            for _ph, _fe, _hd, _inv in _bc.ranked_attempts(
                                    phase_attempts):
                                _burst_jobs.setdefault((_ph, _inv), []).append(
                                    (_fe, _bc.fec_mode, _hd))
                    except Exception as e:
                        # Loud, not silent: falling back to decoding every
                        # phase is correct but costs the whole saving, and
                        # an attribute typo hid exactly that for a build.
                        logger.warning(
                            f"phase-select failed, decoding all phases: {e}")
                        _burst_jobs = None
                for ph in candidates:
                    if ph == phase:
                        continue  # already decoded above
                    if _burst_jobs is not None and not any(
                            (ph, _i) in _burst_jobs for _i in (False, True)):
                        continue  # no burst chose this phase
                    soft_ph = filt[ph::SAMPLES_PER_BIT]
                    bits_ph = slice_bits(soft_ph)
                    soft_list = [float(x) for x in soft_ph]
                    amp_ph = estimate_amplitude(soft_list)
                    for label, b_use, inv in [
                            ('mf-phase%d' % ph, bits_ph, False),
                            ('mf-phase%d-inverted' % ph,
                             [1 - b for b in bits_ph], True)]:
                        _cands = None
                        if _burst_jobs is not None:
                            _cands = sorted(set(_burst_jobs.get((ph, inv), [])))
                            if not _cands:
                                continue
                        for pkt in decoder.decode_bits(
                                b_use, audio_43k=audio_43k, bit_phase=ph,
                                soft_samples=(soft_list if not inv
                                              else [-x for x in soft_list]),
                                amplitude_estimate=amp_ph,
                                filtered_43k=(filt if getattr(
                                    decoder, 'timing_probe', False) else None),
                                soft_inverted=inv,
                                presync_search=False,
                                candidates=_cands):
                            pkt.decode_path = label
                            key = (pkt.fec_mode, pkt.raw_payload)
                            chunk_emit.append(pkt)
                # Per-burst symbol-rate correction (--rate-track). Runs
                # only for bursts whose measured clock drift exceeds the
                # threshold; adds copies, never removes (best-wins dedup
                # below picks one). See rate_track.py.
                if rate_track and _bursts:
                    try:
                        from .rate_track import decode_rate_corrected
                        for pkt in decode_rate_corrected(
                                decoder, filt, _bursts,
                                min_slope=rate_track_min_slope):
                            chunk_emit.append(pkt)
                    except Exception as e:
                        logger.warning(f"rate-track failed: {e}")
            except Exception as e:
                logger.error(f"Multi-phase pass failed: {e}")
        _t_decode += _time.monotonic() - _t_chunk0

        # --- rc22 audio-clock health report ---
        if _t_first_read is not None:
            _elapsed = _time.monotonic() - _t_first_read
            if _elapsed >= _next_health:
                _next_health += health_interval_s
                _expected = int(_elapsed * sample_rate)
                _deficit = max(0, _expected - _samples_read)
                _info = dict(
                    elapsed_s=_elapsed,
                    samples_read=_samples_read,
                    samples_expected=_expected,
                    deficit_samples=_deficit,
                    deficit_s=_deficit / float(sample_rate),
                    deficit_pct=100.0 * _deficit / max(_expected, 1),
                    rt_factor=_t_decode / max(_elapsed, 1e-9),
                    # A live sound card delivers samples at exactly the
                    # nominal rate, so read≈expected and any shortfall
                    # is dropped audio. A replayed file delivers as
                    # fast as it can be read, making the wall-clock
                    # comparison meaningless — label it so the line is
                    # never misread as a healthy live result.
                    source=('live' if _samples_read
                            <= _expected * 1.02 else 'faster-than-rt'),
                )
                if (_deficit > sample_rate * 0.05
                        and _samples_read <= _expected * 1.02):
                    logger.warning(
                        f"audio clock deficit {_info['deficit_s']:.2f}s "
                        f"({_info['deficit_pct']:.2f}%) after "
                        f"{_elapsed:.0f}s — samples are being dropped "
                        "upstream; decode is not keeping up")
                if health_callback is not None:
                    try:
                        health_callback(_info)
                    except Exception:
                        pass

        # --- rc21 shadow dedup processing ---
        _dd_chunk += 1
        # 1) register this batch's best complete per triple
        # Best-wins selection across the primary, multiphase and burst
        # passes for this chunk (see _select_best_copies), THEN the
        # cross-chunk payload gate. This order matters: applying the gate
        # first (as rc28 did on this path) threw away the alternative
        # copies before they could be compared, so the primary phase always
        # won regardless of how badly it was timed.
        chunk_emit = _select_best_copies(chunk_emit, phase_dedup)

        _kept = []
        for pkt in chunk_emit:
            key = (pkt.fec_mode, pkt.raw_payload)
            if key in seen_payloads:
                continue
            seen_payloads.add(key)
            if len(seen_payloads) > 1000:
                seen_payloads.clear()
                seen_payloads.add(key)
            _kept.append(pkt)
        chunk_emit = _kept

        for pkt in chunk_emit:
            t = _dd_triple(pkt)
            if t is not None and not pkt.truncated:
                r = _dd_rank(pkt)
                cur = _dd_recent.get(t)
                if cur is None or r < cur[0]:
                    _dd_recent[t] = (r, _dd_chunk, id(pkt))
        # 2) release packets held from the previous chunk
        for pkt, t, r in _dd_held:
            cur = _dd_recent.get(t)
            if (cur is not None and cur[0] <= r
                    and cur[1] >= _dd_chunk - 2):
                _dd_stats['suppressed'] += 1
                if _dd_stats['suppressed'] % 100 == 1:
                    logger.info(
                        f"shadow dedup: {_dd_stats['suppressed']} "
                        "boundary copies suppressed so far")
            else:
                _dd_recent[t] = (r, _dd_chunk, id(pkt))
                yield pkt
        _dd_held = []
        # 3) emit current batch (hold truncated one chunk)
        for pkt in chunk_emit:
            t = _dd_triple(pkt)
            if t is None:
                yield pkt
                continue
            r = _dd_rank(pkt)
            cur = _dd_recent.get(t)
            if (cur is not None and cur[1] >= _dd_chunk - 2
                    and cur[0] <= r and cur[2] != id(pkt)):
                _dd_stats['suppressed'] += 1
                continue
            if pkt.truncated:
                _dd_held.append((pkt, t, r))
            else:
                yield pkt
        if len(_dd_recent) > 4000:
            _dd_recent = {k: v for k, v in _dd_recent.items()
                          if v[1] >= _dd_chunk - 2}
        samples_processed += len(buffer) - overlap_samples
        buffer = buffer[-overlap_samples:]
        logger.debug(f"Processed {samples_processed/sample_rate:.1f}s")

    # end of stream: flush any held truncated packets (their better
    # sibling never arrived, so they are honest partials, not shadows)
    for pkt, t, r in _dd_held:
        cur = _dd_recent.get(t)
        if (cur is not None and cur[0] <= r and cur[2] != id(pkt)):
            _dd_stats['suppressed'] += 1
        else:
            yield pkt
    if _dd_stats['suppressed']:
        logger.info(f"shadow dedup: {_dd_stats['suppressed']} boundary "
                    "copies suppressed this session")


def stream_decode_stdin(sample_rate: int = 48000,
                         **kwargs) -> Iterator[Alert2Packet]:
    """Convenience: stream from stdin (raw int16 mono PCM)."""
    return stream_decode(sys.stdin.buffer, sample_rate=sample_rate, **kwargs)
