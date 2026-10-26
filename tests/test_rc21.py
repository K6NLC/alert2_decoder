# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for rc21: progressive DD-eq, per-block RS attribution,
shadow dedup, and presync raw-HD logging."""
import io
import numpy as np

from alert2.decoder import _dd_eq_progressive, decode_wav
from alert2.fec import decode_followon_stream_soft_blocks, quantize_soft
from alert2.demod import demodulate
from alert2.generator import make_sensor_frame, build_tx_audio, TxTiming
from alert2.stream import stream_decode
from alert2.constants import (FIRST_BLOCK_DATA, FOLLOWON_BLOCK_DATA,
                              RS_PARITY, AIRLINK_HEADER_BYTES)
from alert2.sync import find_framesync_with_bitsync_gate
from alert2.fec import decode_first_block_soft

FS = 43200
TOTAL0 = FIRST_BLOCK_DATA[0] + RS_PARITY[0]
SLICE0 = TOTAL0 * 8 * 2 + 12
OFF0 = TOTAL0 * 8 * 2 + 16


def _long_frame_audio(n_readings=30, seed=5):
    readings = [(10 + i, 1.0 + 0.37 * i) for i in range(n_readings)]
    bits = make_sensor_frame(798, readings, timestamp=30000 + seed)
    burst = build_tx_audio(bits, TxTiming())
    audio = np.random.RandomState(seed).normal(
        0, 0.003, len(burst) + 2 * FS).astype(np.float32)
    audio[FS:FS + len(burst)] += burst
    return audio


def _soft_drift(soft, fe, region_end, k=1.1, noise=0.6, seed=13):
    """Symbol-domain ISI that ramps over the burst: zero at the
    preamble, maximal at the frame tail — the 'filter trained early is
    stale by the tail' failure mode progressive DD-eq targets. Applied
    to the soft symbols directly (the layer the equalizer operates on);
    audio-domain echoes are neutralized by the constant-envelope
    modulation and matched filter, as the tuning sweep showed."""
    s = np.asarray(soft, dtype=float)
    n = len(s)
    idx = np.arange(n)
    span = max(1, region_end - fe)
    ramp = np.clip((idx - fe) / span, 0, 1)
    out = s.copy()
    out[1:] += (k * ramp[1:]) * s[:-1]
    out[:-1] += (-0.8 * k * ramp[:-1]) * s[1:]
    out += np.random.RandomState(seed).normal(
        0, noise * np.abs(s).mean(), n) * ramp
    return out.tolist()


def _first_block(audio):
    bits, info = demodulate(audio, FS)
    soft = info.get('soft_samples')
    amp = info.get('amplitude_estimate')
    q = quantize_soft(soft, amp)
    for fe, m, hd in find_framesync_with_bitsync_gate(
            bits, max_fs_hd=6, max_bs_hd=8):
        if m != 0:
            continue
        d, c = decode_first_block_soft(q[fe:fe + SLICE0], 0)
        if d is not None:
            return bits, soft, amp, fe, d
    return None, None, None, None, None


def test_progressive_recovers_drifting_channel():
    audio = _long_frame_audio()
    bits, soft, amp, fe, first0 = _first_block(audio)
    assert first0 is not None
    from alert2.types import AirLinkHeader
    from alert2.fec import decode_first_block_soft
    al = AirLinkHeader.parse(first0[:2])
    ba = al.length - (FIRST_BLOCK_DATA[0] - AIRLINK_HEADER_BYTES)
    assert ba > FOLLOWON_BLOCK_DATA[0], "need a multi-block follow-on"
    full = FOLLOWON_BLOCK_DATA[0]
    nf, fin = divmod(ba, full)
    trs = nf * (full + RS_PARITY[0]) + ((fin + RS_PARITY[0]) if fin else 0)
    ln = trs * 8 * 2 + 12
    st = fe + OFF0
    n_blocks = nf + (1 if fin else 0)
    ds = _soft_drift(soft, fe, st + ln)
    q = quantize_soft(ds, amp)
    first, c1 = decode_first_block_soft(q[fe:fe + SLICE0], 0)
    assert first is not None, "drift must leave the first block decodable"
    _, _, base_ok = decode_followon_stream_soft_blocks(q[st:st + ln], 0, ba)
    assert base_ok < n_blocks,         "channel must defeat the baseline for this test to discriminate"
    _, _, ok1, _, _ = _dd_eq_progressive(ds, fe, first, 0, ba, st, ln,
                                         max_iters=1)
    d, pb, ok, nb, iters = _dd_eq_progressive(ds, fe, first, 0, ba, st, ln)
    assert nb == n_blocks
    assert ok >= max(base_ok, ok1),         "progressive must never do worse than baseline or single-DD"
    assert ok == nb and d is not None, (
        f"progressive should complete the drifting frame "
        f"(baseline {base_ok}/{nb}, single-DD {ok1}/{nb}, "
        f"progressive {ok}/{nb})")
    assert len(pb) == nb and iters >= 2,         "recovery should require at least one mid-frame retrain"


def test_per_block_corr_sums_to_total():
    audio = _long_frame_audio(seed=9)
    pkts = decode_wav.__wrapped__ if hasattr(decode_wav, '__wrapped__') \
        else None
    # decode through the normal API from a wav on disk
    from scipy.io import wavfile as wavio
    wavio.write('/tmp/_rc21_pb.wav', FS, (audio * 32767).astype(np.int16))
    got = decode_wav('/tmp/_rc21_pb.wav', soft_viterbi=True,
                     iterative='off', eq_retry=True)
    assert got, "clean long frame must decode"
    p = got[0]
    assert p.rs_block_corr is not None and len(p.rs_block_corr) > 1
    assert sum(p.rs_block_corr) == p.rs_corrections


def test_shadow_dedup_suppresses_boundary_copy():
    """A burst straddling a chunk boundary must emit exactly once."""
    readings = [(10 + i, 2.0 + i) for i in range(30)]
    bits = make_sensor_frame(798, readings, timestamp=31000)
    burst = build_tx_audio(bits, TxTiming())
    total = int(14 * FS)
    audio = np.random.RandomState(3).normal(
        0, 0.003, total).astype(np.float32)
    # place the burst straddling the first chunk boundary (chunk=6s):
    # start ~5.5s so its head is in chunk 1, full copy in chunk 2's
    # overlap region
    i = int(5.5 * FS)
    audio[i:i + len(burst)] += burst[:total - i]
    pcm = (audio * 32767).astype(np.int16).tobytes()
    got = list(stream_decode(io.BytesIO(pcm), sample_rate=FS,
                             chunk_seconds=6.0, overlap_seconds=3.0,
                             soft_viterbi=True, iterative='off',
                             eq_retry=True))
    trips = [(p.mant.source_addr,
              p.srp_pdu.timestamp if (p.srp_pdu
                                      and p.srp_pdu.has_timestamp)
              else None,
              tuple(p.mant.added_sas or []))
             for p in got if p.mant]
    assert len(trips) == len(set(trips)), (
        f"duplicate (src,ts,path) emissions survived dedup: {trips}")
    assert any(not p.truncated for p in got), \
        "the complete copy must be the survivor"


def test_dedup_preserves_distinct_timestamps():
    """Two different transmissions from the same src must BOTH emit."""
    frames = []
    for k in range(2):
        readings = [(10 + i, 2.0 + i + k) for i in range(10)]
        frames.append(make_sensor_frame(750, readings,
                                        timestamp=32000 + 60 * k))
    total = int(16 * FS)
    audio = np.random.RandomState(4).normal(
        0, 0.003, total).astype(np.float32)
    for k, fb in enumerate(frames):
        burst = build_tx_audio(fb, TxTiming())
        i = int((2 + 6 * k) * FS)
        audio[i:i + len(burst)] += burst[:total - i]
    pcm = (audio * 32767).astype(np.int16).tobytes()
    got = list(stream_decode(io.BytesIO(pcm), sample_rate=FS,
                             chunk_seconds=6.0, overlap_seconds=3.0,
                             soft_viterbi=True, iterative='off'))
    ts = sorted(set(p.srp_pdu.timestamp for p in got
                    if p.srp_pdu and p.srp_pdu.has_timestamp))
    assert len(ts) == 2, f"both transmissions must survive dedup: {ts}"
